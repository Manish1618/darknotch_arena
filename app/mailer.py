"""Sends the platform's few emails: password-reset codes and "was this you?"
security notes.

Without SMTP_HOST (local development only; production refuses to start) mail
goes to OUTBOX and the server log instead, so the flow can be tried offline.
SMTP_HOST=log does the same on a real server, for trying the site before mail
is set up: only someone who can read the server's log gets the codes."""
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .config import settings

log = logging.getLogger("mailer")
OUTBOX: list[dict] = []  # development and tests only


class MailError(Exception):
    pass


def send(to: str, subject: str, text: str) -> None:
    if settings.smtp_host in ("", "log"):
        if not settings.smtp_host and settings.cookie_secure:
            raise MailError("Email isn't set up on this server yet. Tell the site admin.")
        OUTBOX.append({"to": to, "subject": subject, "text": text})
        del OUTBOX[:-50]
        log.warning("mail (%s, not sent) to %s: %s\n%s",
                    "SMTP_HOST=log" if settings.smtp_host else "development", to, subject, text)
        return
    msg = EmailMessage()
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid()
    msg.set_content(text)
    ctx = ssl.create_default_context()
    try:
        if settings.smtp_security == "ssl":
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=ctx, timeout=15) as s:
                _deliver(s, msg)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as s:
                if settings.smtp_security == "starttls":
                    s.starttls(context=ctx)
                _deliver(s, msg)
    except (smtplib.SMTPException, OSError) as e:
        log.error("mail to %s failed: %s", to, e)
        raise MailError("Couldn't send the email just now. Try again in a minute.") from e


def _deliver(s: smtplib.SMTP, msg: EmailMessage) -> None:
    if settings.smtp_user:
        s.login(settings.smtp_user, settings.smtp_password)
    s.send_message(msg)


def code_mail(to: str, purpose: str, code: str) -> None:
    site, minutes = settings.site_name, settings.email_code_ttl_s // 60
    if purpose == "register":
        send(to, f"{code} is your {site} sign-up code",
             f"Your {site} sign-up code is:\n\n    {code}\n\nIt works for {minutes} minutes, once.\n\n"
             "If you didn't ask for it, ignore this email. No account is made without the code.\n")
    else:
        send(to, f"{code} is your {site} password reset code",
             f"Your {site} password reset code is:\n\n    {code}\n\nIt works for {minutes} minutes, once.\n\n"
             "If you didn't ask for it, ignore this email. Your password stays the same, and\n"
             "nobody can change it without this code.\n")


def security_mail(to: str, what: str, when: str, ip: str, device: str) -> None:
    """'Was this you?' notes, like the big platforms send: a new sign-in place,
    a Google account linked."""
    site = settings.site_name
    send(to, f"{site}: {what}",
         f"{what} on your {site} account.\n\n    When: {when}\n    IP address: {ip or 'unknown'}\n    Browser: {device or 'unknown'}\n\n"
         "If this was you, there's nothing to do.\n"
         "If it wasn't, reset your password from the login page, then on your Account page log out\n"
         "everywhere else and turn on two-factor login.\n")


def quietly(fn, *args) -> None:
    """For mail sent after the response: a failure is logged, never shown."""
    try:
        fn(*args)
    except MailError:
        pass
