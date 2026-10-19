"""Site-wide rules that site admins change from the browser, and the checks
that keep sign-ups to real, permanent email addresses."""
import re

from email_validator import EmailNotValidError, caching_resolver, validate_email
from sqlalchemy.orm import Session

from .config import settings
from .models import SiteSetting

DEFAULTS = {
    "registration_open": True,   # new accounts may sign up
    "allowed_domains": [],       # empty = any domain; else only these (and their subdomains)
    "block_disposable": True,    # refuse throwaway inboxes
}
DOMAIN_RE = re.compile(r"^(?=.{3,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")

# Common throwaway inbox services. Not complete (new ones appear daily), which is
# why the DNS check and the per-address code still matter.
DISPOSABLE = frozenset("""
mailinator.com guerrillamail.com guerrillamail.net guerrillamail.org guerrillamailblock.com sharklasers.com grr.la
pokemail.net spam4.me 10minutemail.com 10minutemail.net 20minutemail.com tempmail.com temp-mail.org temp-mail.io
tempmail.net tempmailo.com tempmail.dev tempinbox.com tempail.com tempr.email yopmail.com yopmail.net yopmail.fr
trashmail.com trashmail.de trashmail.net getnada.com nada.email maildrop.cc dispostable.com throwawaymail.com
fakeinbox.com mintemail.com mohmal.com emailondeck.com discard.email spamgourmet.com mailnesia.com mytemp.email
burnermail.io inboxkitten.com mailcatch.com moakt.com tmail.ws emailfake.com crazymailing.com mailpoof.com
1secmail.com 1secmail.org 1secmail.net etempmail.com getairmail.com tempmailaddress.com fakemail.net
mail.tm mailsac.com harakirimail.com mailinator.net mailinator.org spambox.us anonbox.net 33mail.com
emailtemporanea.net linshiyouxiang.net mailtemp.net minuteinbox.com dropmail.me tmpmail.org tmpmail.net
""".split())

_resolver = caching_resolver(timeout=5)


def get_site(db: Session) -> dict:
    out = dict(DEFAULTS)
    for row in db.query(SiteSetting).filter(SiteSetting.key.in_(DEFAULTS)):
        out[row.key] = row.value
    return out


def set_site(db: Session, data: dict) -> None:
    for k, v in data.items():
        if k not in DEFAULTS:
            continue
        row = db.get(SiteSetting, k)
        if row:
            row.value = v
        else:
            db.add(SiteSetting(key=k, value=v))


def _within(domain: str, parent: str) -> bool:
    return domain == parent or domain.endswith("." + parent)


def email_problem(email: str, site: dict, check_dns: bool = True) -> str | None:
    """Why this address can't be used to sign up, or None. The DNS check is
    skipped for addresses Google has already verified."""
    domain = email.rsplit("@", 1)[-1].lower()
    allowed = site.get("allowed_domains") or []
    if allowed and not any(_within(domain, d) for d in allowed):
        return "Sign-up is limited to addresses at " + ", ".join(allowed) + "."
    if site.get("block_disposable", True) and any(_within(domain, d) for d in DISPOSABLE):
        return "Use a permanent email address. Throwaway inboxes can't be used here."
    if check_dns and settings.email_check_dns:
        try:
            validate_email(email, check_deliverability=True, dns_resolver=_resolver)
        except EmailNotValidError:
            return "That address's domain can't receive email. Check it for typos."
    return None
