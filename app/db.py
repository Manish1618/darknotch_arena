from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

if settings.database_url.startswith("sqlite"):
    _kw = {"connect_args": {"check_same_thread": False}}
else:
    # Every request opens a session, and the app serves up to 40 at once. The
    # default pool (5 + 10) would make busy moments queue and time out.
    # pre_ping drops connections that died with a Postgres restart.
    _kw = {"pool_size": settings.db_pool_size, "max_overflow": settings.db_pool_overflow,
           "pool_timeout": 10, "pool_pre_ping": True}
engine = create_engine(settings.database_url, **_kw)

if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):  # enforce foreign keys on SQLite like Postgres does
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
