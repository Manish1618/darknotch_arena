"""Server-side tasks that must never be reachable from the web.

    docker compose exec app python -m app.cli make-admin you@example.org
    docker compose exec app python -m app.cli remove-admin you@example.org
    docker compose exec app python -m app.cli list-admins

The account has to exist first (sign up in the browser). Site admins then need
2FA on before the Admin page lets them in."""
import sys

from .audit import audit
from .config import settings
from .db import Base, SessionLocal, engine
from .models import AuthSession, User


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("make-admin", "remove-admin", "list-admins"):
        print(__doc__)
        return 2
    if not settings.cookie_secure:
        Base.metadata.create_all(engine)  # development; production tables come from the migrations
    db = SessionLocal()
    try:
        if argv[0] == "list-admins":
            for u in db.query(User).filter_by(is_admin=True).order_by(User.id):
                print(f"{u.username}\t{u.email}\t2FA {'on' if u.totp_enabled else 'OFF'}")
            return 0
        if len(argv) != 2:
            print(f"usage: python -m app.cli {argv[0]} EMAIL")
            return 2
        u = db.query(User).filter_by(email=argv[1].strip().lower()).first()
        if not u:
            print("No account with that email. Sign up in the browser first.")
            return 1
        u.is_admin = argv[0] == "make-admin"
        # sessions end so the change takes effect with a fresh login
        db.query(AuthSession).filter(AuthSession.user_id == u.id).delete()
        audit(db, "admin_granted" if u.is_admin else "admin_removed", u.id, detail="server command line")
        db.commit()
        print(f"{u.username} is {'now' if u.is_admin else 'no longer'} a site admin.")
        if u.is_admin and not u.totp_enabled:
            print("They must turn on 2FA (Account page) before the Admin page opens.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
