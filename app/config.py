import os


class Settings:
    site_name: str = os.environ.get("SITE_NAME", "Darkn0tch Arena")
    database_url: str = os.environ.get("DATABASE_URL", "sqlite:///./dev.db")
    # Cookies are Secure by default; set COOKIE_SECURE=0 only for local http dev.
    cookie_secure: bool = os.environ.get("COOKIE_SECURE", "1") != "0"
    session_ttl_s: int = int(os.environ.get("SESSION_TTL_S", str(7 * 24 * 3600)))
    max_team_size_cap: int = 10
    # HMAC key for stored flags. Changing it invalidates every stored flag.
    flag_pepper: str = os.environ.get("FLAG_PEPPER", "")
    # Wrong-flag throttles per user and per team (per minute).
    flag_fail_user: int = 10
    flag_fail_team: int = 30
    upload_dir: str = os.environ.get("UPLOAD_DIR", "./uploads")
    max_upload_bytes: int = int(os.environ.get("MAX_UPLOAD_MB", "50")) * 1024 * 1024
    # Per-IP and per-account login throttles (window seconds, max failures).
    # The per-IP limit is generous because a whole college lab often shares one
    # public IP; the per-account limit is what stops password guessing.
    login_window_s: int = 300
    login_fail_account: int = 8
    login_fail_ip: int = int(os.environ.get("LOGIN_FAIL_IP", "100"))
    # A session ends after this long without any request (the app polls while open).
    session_idle_s: int = int(os.environ.get("SESSION_IDLE_S", str(12 * 3600)))
    # Encrypts 2FA secrets at rest and keys recovery-code hashes. Required in production.
    secret_key: str = os.environ.get("SECRET_KEY", "")
    # "required": organisers must turn on 2FA before creating or managing events.
    organiser_mfa: str = os.environ.get("ORGANISER_MFA", "required" if cookie_secure else "optional")
    # Host names the app answers to (comma separated); empty = any (dev).
    allowed_hosts: list[str] = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
    # Largest request body outside file uploads.
    max_body_bytes: int = 1024 * 1024
    # Seconds a scoreboard answer is shared between viewers (0 turns caching off).
    # Anything that changes scores clears it at once, so this mostly bounds how
    # stale the wrong-flag activity strip can get.
    board_cache_s: float = float(os.environ.get("BOARD_CACHE_S", "3"))
    # Outgoing mail for sign-up and password-reset codes. Required in production;
    # without SMTP_HOST in local development, mail goes to the server log instead.
    smtp_host: str = os.environ.get("SMTP_HOST", "")
    smtp_port: int = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user: str = os.environ.get("SMTP_USER", "")
    smtp_password: str = os.environ.get("SMTP_PASSWORD", "")
    smtp_from: str = os.environ.get("SMTP_FROM", "")
    smtp_security: str = os.environ.get("SMTP_SECURITY", "starttls")  # starttls | ssl | none
    # Check that an address's domain can actually receive mail (DNS) before sending a code.
    email_check_dns: bool = os.environ.get("EMAIL_CHECK_DNS", "1" if cookie_secure else "0") != "0"
    email_code_ttl_s: int = 600
    # Code emails one IP may trigger per hour (a whole lab can share one IP).
    mail_per_ip_hour: int = int(os.environ.get("MAIL_PER_IP_HOUR", "120"))
    # Shared state for running several API containers: rate limits and the
    # scoreboard cache live in Redis so every container sees the same counts.
    # Empty = in-process (one API process only; fine for development).
    redis_url: str = os.environ.get("REDIS_URL", "")
    # The address players type, used for sign-in redirects.
    public_url: str = os.environ.get("PUBLIC_URL", "").rstrip("/") or (
        f"https://{os.environ['DOMAIN']}" if os.environ.get("DOMAIN") else "http://localhost:8000")
    # "Sign in with ..." apps you register at each provider (leave empty to hide that button).
    google_client_id: str = os.environ.get("GOOGLE_CLIENT_ID", "")
    google_client_secret: str = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    # Postgres connections the app may hold (Postgres allows 100 by default).
    db_pool_size: int = int(os.environ.get("DB_POOL_SIZE", "20"))
    db_pool_overflow: int = int(os.environ.get("DB_POOL_OVERFLOW", "20"))


settings = Settings()
