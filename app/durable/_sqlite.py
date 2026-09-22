"""Internal SQLite plumbing shared by the durable stores.

Every store takes either a plain file path or a DSN plus an optional
``connection_factory``. The factory hook is the PostgreSQL replacement seam:
SQL in this package is kept to a portable subset (no ``INSERT OR REPLACE``,
no ``RETURNING``), so swapping in a psycopg connection factory plus matching
DDL is enough to move the stores off SQLite. A ``postgresql://`` DSN today
fails loudly instead of silently ignoring the scheme.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Callable

ConnectionFactory = Callable[[], sqlite3.Connection]


def resolve_sqlite_path(dsn_or_path: str) -> str:
    if "://" not in dsn_or_path:
        return dsn_or_path
    scheme, _, rest = dsn_or_path.partition("://")
    if scheme == "sqlite":
        return rest
    raise NotImplementedError(
        f"DSN scheme {scheme!r} is not supported by the SQLite backend; "
        "pass connection_factory to plug in another database (e.g. PostgreSQL)"
    )


def open_sqlite(db_path: str) -> sqlite3.Connection:
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


class StoreBase:
    """One connection + one re-entrant lock; safe to share across threads."""

    def __init__(
        self,
        db_path: str,
        *,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        self._db_path = resolve_sqlite_path(db_path)
        self._conn = connection_factory() if connection_factory else open_sqlite(self._db_path)
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def close(self) -> None:
        with self._lock:
            self._conn.close()
