from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

_is_sqlite = settings.db_url.startswith("sqlite")
_is_postgres = settings.db_url.startswith(("postgresql", "postgres"))

_connect_args: dict = {}
if _is_sqlite:
    _connect_args = {"check_same_thread": False}
elif _is_postgres:
    # Neon/pgbouncer tidak suport prepared statement lintas koneksi.
    _connect_args = {"connect_timeout": 10, "prepare_threshold": None}

engine = create_engine(
    settings.db_url,
    echo=False,
    future=True,
    pool_pre_ping=True,
    pool_recycle=300,
    connect_args=_connect_args,
)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    from . import models  # noqa: F401  (registrasi tabel)

    models.Base.metadata.create_all(engine)


def get_session() -> Session:
    return SessionLocal()
