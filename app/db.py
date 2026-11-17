from contextlib import contextmanager

from sqlalchemy import event
from sqlalchemy.exc import OperationalError
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
    for table, column, ddl in (
        ("source", "keywords", "VARCHAR NOT NULL DEFAULT ''"),
        ("video", "progress", "FLOAT NOT NULL DEFAULT 0"),
        ("source", "auto", "BOOLEAN NOT NULL DEFAULT 0"),
        ("source", "last_seen", "DATETIME"),
    ):
        with engine.begin() as conn:
            cols = {r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if column in cols:
                continue
            try:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            except OperationalError as e:  # l'autre processus l'a ajoutée en même temps
                if "duplicate column" not in str(e):
                    raise


@contextmanager
def session():
    with Session(get_engine(), expire_on_commit=False) as s:
        yield s
