"""SQLite engine/session factory with WAL mode (Streamlit and scheduler share the DB file)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, pool, text
from sqlalchemy.orm import Session, sessionmaker

from core.schema import Base

_engines: dict[str, Engine] = {}


def get_engine(db_path: Path | str) -> Engine:
    key = str(Path(db_path).resolve()) if str(db_path) != ":memory:" else ":memory:"
    if key in _engines:
        return _engines[key]
    if key == ":memory:":
        url = "sqlite+pysqlite:///:memory:"
        engine = create_engine(
            url, future=True, connect_args={"check_same_thread": False}, poolclass=pool.StaticPool
        )
    else:
        Path(key).parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite+pysqlite:///{key}"
        # NullPool: Streamlit reruns must not reuse a connection left inside a transaction.
        engine = create_engine(
            url,
            future=True,
            poolclass=pool.NullPool,
            connect_args={"check_same_thread": False, "timeout": 30},
        )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):  # noqa: ANN001
        # isolation_level None = autocommit, so journal_mode actually persists.
        previous = dbapi_conn.isolation_level
        dbapi_conn.isolation_level = None
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        if key != ":memory:":
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()
        dbapi_conn.isolation_level = previous

    _engines[key] = engine
    return engine


def init_db(db_path: Path | str) -> Engine:
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return engine


def session_factory(db_path: Path | str) -> sessionmaker[Session]:
    return sessionmaker(bind=init_db(db_path), expire_on_commit=False, future=True)


@contextmanager
def session_scope(db_path: Path | str) -> Iterator[Session]:
    factory = session_factory(db_path)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
