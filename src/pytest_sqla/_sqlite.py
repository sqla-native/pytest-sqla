"""SQLite: the savepoint fix, shared in-memory databases, and per-worker files.

**The leak.** In sqlite3's legacy transaction control - Python's default
through 3.15 - the driver skips ``BEGIN`` except before DML, so a SAVEPOINT
emitted first "fails to participate in the enclosing transaction" (the
SQLAlchemy 2.0 and 2.1 SQLite docs). Measured on pysqlite and aiosqlite,
Python 3.11 to 3.13, SQLAlchemy 2.0.0, 2.0.55 and 2.1.5: a row committed
through a ``create_savepoint`` session survives the outer rollback (1 row
left), and SQLAlchemy's documented connect/begin listeners fix it (0 rows).
pytest-sqla applies them to every SQLite engine it creates, unless the
connection runs in PEP 249 mode (``connect_args={"autocommit": False}``,
Python 3.12+), which needs no fix and must not get a second ``BEGIN``. When a
future Python makes PEP 249 mode the default, the check below comes out False
and the fix switches itself off.

**Files shared between processes.** On a SQLite file the test transaction
begins ``IMMEDIATE``, taking the write lock at once. A deferred transaction
that has read and then tries to write while another process holds the write
lock gets "database is locked" immediately, because SQLite refuses the lock
upgrade rather than deadlock: measured with two xdist workers sharing a file
in none mode. An immediate one waits out the busy timeout instead, so the
processes take turns. In-memory databases belong to one process and keep a
plain ``BEGIN``.

**In-memory databases.** A plain memory URL is private to one connection, so
it is rewritten to a named database on SQLite's memdb VFS, which "allows the
same in-memory database to be shared among multiple database connections in
the same process as long as the database name begins with '/'" (SQLite 3.36.0
release notes). One name per pytest session, shared by the sync and async
engines. Shared-cache mode is not used: sqlite.org calls it "an obsolete
feature" whose use "is discouraged", and measured, it fails at once with
"database table is locked" where memdb waits out the busy timeout like a file.
A keeper connection holds the database open for the session, because NullPool
and ``dispose()`` would otherwise discard it with its last connection.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import PurePath
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.engine import URL, Connection, Engine

MEMDB_MIN_VERSION = (3, 36, 0)

# sqlite3's own legacy-mode marker. Python 3.12 added the attribute; before
# that every connection runs in legacy mode, which the fallback expresses.
_LEGACY = getattr(sqlite3, "LEGACY_TRANSACTION_CONTROL", -1)

# The query parameters SQLAlchemy hands to sqlite3.connect() instead of putting
# them in the SQLite URI (SQLiteDialect_pysqlite.create_connect_args).
_DRIVER_PARAMETERS = frozenset(
    {"uri", "timeout", "isolation_level", "detect_types", "check_same_thread", "cached_statements"}
)
_TRUE = frozenset({"true", "1", "yes", "on", "y", "t"})


def is_sqlite(url: URL) -> bool:
    return url.get_backend_name() == "sqlite"


def _uses_uri(url: URL) -> bool:
    return str(url.query.get("uri", "")).lower() in _TRUE


def is_plain_memory(url: URL) -> bool:
    """``sqlite://``, ``sqlite:///:memory:`` and their aiosqlite twins."""
    return is_sqlite(url) and not _uses_uri(url) and url.database in (None, "", ":memory:")


def is_shared_memory(url: URL) -> bool:
    """An in-memory database that several connections can open by name."""
    if not (is_sqlite(url) and _uses_uri(url)):
        return False
    database = url.database or ""
    return (
        url.query.get("mode") == "memory"
        or url.query.get("vfs") == "memdb"
        or database.startswith("file::memory:")
    )


def is_plain_file(url: URL) -> bool:
    """A file database named by path, which is what a per-worker copy can derive from."""
    return is_sqlite(url) and not _uses_uri(url) and not is_plain_memory(url)


def shared_memory_url(url: URL, name: str) -> URL:
    """Rewrite a plain memory URL to the memdb database ``name``."""
    return url.set(database=f"file:/{name}", query={**url.query, "vfs": "memdb", "uri": "true"})


def require_shared_memory(version: tuple[int, ...] = sqlite3.sqlite_version_info) -> None:
    if version < MEMDB_MIN_VERSION:
        found = ".".join(str(part) for part in version)
        pytest.fail(
            "pytest-sqla: an in-memory SQLite URL needs SQLite 3.36 or newer so that every "
            f"connection sees the same database; this Python uses SQLite {found}. Use a file "
            "URL such as sqlite:///test.db.",
            pytrace=False,
        )


def per_worker_url(url: URL, worker: str) -> URL:
    """``test.db`` becomes ``test-gw0.db`` next to it, for one xdist worker.

    SQLite allows one writer per file and every test holds its write lock until
    teardown, so workers sharing one file would wait on each other's tests.
    """
    path = PurePath(str(url.database))
    return url.set(database=str(path.with_name(f"{path.stem}-{worker}{path.suffix}")))


def keeper_target(url: URL) -> str:
    """The filename sqlite3 opens for ``url``, built the way SQLAlchemy builds it."""
    parameters = {key: value for key, value in url.query.items() if key not in _DRIVER_PARAMETERS}
    target = url.database or ""
    if parameters:
        target += "?" + "&".join(f"{key}={parameters[key]}" for key in sorted(parameters))
    return target


def open_keeper(target: str) -> sqlite3.Connection:
    return sqlite3.connect(target, uri=True)


def default_autocommit() -> object:
    """What sqlite3 does when nobody passes ``autocommit``: legacy mode, until it is not."""
    probe = sqlite3.connect(":memory:")
    try:
        return getattr(probe, "autocommit", _LEGACY)
    finally:
        probe.close()


def runs_legacy_transaction_control(connect_args: Mapping[str, Any]) -> bool:
    return bool(connect_args.get("autocommit", default_autocommit()) == _LEGACY)


def disable_implicit_transactions(dbapi_connection: Any, _record: object) -> None:
    """The documented fix, part one: stop sqlite3 from managing transactions."""
    dbapi_connection.isolation_level = None


def begin(connection: Connection) -> None:
    """The documented fix, part two: emit the BEGIN sqlite3 no longer does."""
    connection.exec_driver_sql("BEGIN")


def begin_immediate(connection: Connection) -> None:
    """For files and the schema connection: take the write lock at BEGIN."""
    connection.exec_driver_sql("BEGIN IMMEDIATE")


def apply_savepoint_fix(
    sync_engine: Engine, connect_args: Mapping[str, Any], *, immediate: bool = False
) -> bool:
    """Install the connect and begin listeners when the driver runs in legacy mode.

    Works for aiosqlite too, on ``AsyncEngine.sync_engine``: SQLAlchemy's
    adapter forwards ``isolation_level`` to the sqlite3 connection.
    """
    if not runs_legacy_transaction_control(connect_args):
        return False
    event.listen(sync_engine, "connect", disable_implicit_transactions)
    event.listen(sync_engine, "begin", begin_immediate if immediate else begin)
    return True
