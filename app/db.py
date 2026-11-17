from contextlib import contextmanager

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from .config import get_settings

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        s = get_settings()
        _engine = create_engine(
            f"sqlite:///{s.db_path}", connect_args={"check_same_thread": False, "timeout": 30}
        )

        @event.listens_for(_engine, "connect")
        def _pragmas(conn, _):
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        from . import models  # noqa: F401

        SQLModel.metadata.create_all(_engine)
        _migrate(_engine)
    return _engine


def _migrate(engine):
    """Ajoute les colonnes apparues après la création de la base."""
    with engine.begin() as conn:
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(source)")}
        if "keywords" not in cols:
            conn.exec_driver_sql("ALTER TABLE source ADD COLUMN keywords VARCHAR NOT NULL DEFAULT ''")


@contextmanager
def session():
    with Session(get_engine(), expire_on_commit=False) as s:
        yield s
