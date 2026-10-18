"""Every engine pytest-sqla creates, and the options it refuses to pass on.

Three kinds:

* the session-wide **sync engine**: NullPool, so every test gets a DBAPI
  connection of its own, unless ``sqla_engine_options`` choose a pool;
* **async engines**: NullPool when one engine serves many event loops (a
  per-test loop, or anyio), which is SQLAlchemy's documented answer for
  multiple loops, and a real pool when the engine lives exactly as long as
  one loop (see ``_asyncio``);
* short-lived NullPool **schema engines** for DDL (see ``_schema``).

**Why a fresh connection per test.** ROLLBACK undoes rows, not a session's
state, and a pooled connection hands that state to the next test. Measured
with the QueuePool the sync engine used to have: MySQL kept ``SET
FOREIGN_KEY_CHECKS = 0`` (the next test's foreign-key violation did not
raise), user variables, temporary tables and ``sql_mode``, and after ``SET
autocommit = 1`` every later test committed for real; PostgreSQL kept a
PREPAREd statement and a session advisory lock; SQLite kept ``PRAGMA
query_only = 1``, so every later test failed. NullPool costs a few
milliseconds per test against a server (``scripts/benchmark.py``). A pool the
user chooses, or the pool of a class, module, package or session loop, trades
that for speed.

On SQLite each engine gets the savepoint fix from ``_sqlite``. A pool class is
always passed explicitly for SQLite, because SQLAlchemy 2.1 deprecates choosing
one from a ``mode=memory`` URL and filterwarnings = error would fail the run.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool

from . import _sqlite
from ._config import render

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

_QUEUE_POOL_OPTIONS = frozenset({"pool_size", "max_overflow", "pool_timeout", "pool_use_lifo"})
"""Options create_engine() refuses for NullPool. Copied from an application's
own engine, they made every database test error with "Invalid argument(s)
'pool_size' sent to create_engine()"; a NullPool engine has nothing to size."""


def _level(value: object) -> object:
    """An isolation level the way SQLAlchemy reads it: 'read_uncommitted' is 'READ UNCOMMITTED'."""
    return value.replace("_", " ").upper() if isinstance(value, str) else value


def check_engine_options(url: URL, options: Mapping[str, Any], *, for_async: bool) -> None:
    """Refuse the options that would make a rollback impossible, or a pool cross loops."""
    if for_async and ("poolclass" in options or "pool" in options):
        pytest.fail(
            "pytest-sqla: sqla_engine_options cannot set poolclass or pool for async engines; "
            "pytest-sqla picks the pool per event loop (NullPool for a per-test loop, a pool "
            "that lives as long as the loop otherwise).",
            pytrace=False,
        )
    execution_options = options.get("execution_options") or {}
    levels = {
        _level(options.get("isolation_level")),
        _level(execution_options.get("isolation_level")),
    }
    if "AUTOCOMMIT" in levels:
        pytest.fail(
            "pytest-sqla: isolation_level='AUTOCOMMIT' commits every statement, so pytest-sqla "
            "could not roll a test back. Remove it from sqla_engine_options.",
            pytrace=False,
        )
    if "READ UNCOMMITTED" in levels:
        pytest.fail(
            "pytest-sqla: isolation_level='READ UNCOMMITTED' lets a test read rows other tests "
            "have not committed, such as another pytest-xdist worker's, so pytest-sqla could not "
            "keep tests apart. Remove it from sqla_engine_options.",
            pytrace=False,
        )
    _check_autocommit(url, options.get("connect_args") or {})


def _check_autocommit(url: URL, connect_args: Mapping[str, Any]) -> None:
    autocommit = connect_args.get("autocommit")
    # sqlite3 takes exactly True, False or LEGACY_TRANSACTION_CONTROL (-1, the
    # default); psycopg, pymysql, aiomysql and asyncmy commit on any true value.
    if (autocommit is True) if _sqlite.is_sqlite(url) else bool(autocommit):
        pytest.fail(
            f"pytest-sqla: connect_args autocommit={autocommit!r} makes the driver commit every "
            "statement, so pytest-sqla could not roll a test back. Leave it unset, or set it to "
            "False.",
            pytrace=False,
        )
    if not _sqlite.is_sqlite(url) and "autocommit" in url.query:
        # Measured: pymysql reads autocommit=false in a URL as true.
        pytest.fail(
            f"pytest-sqla: the autocommit query parameter of {render(url)} reaches the driver, "
            "which then commits every statement, so pytest-sqla could not roll a test back. "
            "Remove it from the URL.",
            pytrace=False,
        )


def _unpooled(options: Mapping[str, Any]) -> dict[str, Any]:
    kwargs = {
        key: value
        for key, value in options.items()
        if key not in _QUEUE_POOL_OPTIONS and key not in ("pool", "poolclass")
    }
    kwargs["poolclass"] = NullPool
    return kwargs


def _thread_safe(url: URL, kwargs: dict[str, Any]) -> dict[str, Any]:
    if _sqlite.is_shared_memory(url) and url.get_driver_name() == "pysqlite":
        # SQLAlchemy sets check_same_thread=False only for what it considers a
        # file database; a test can use its mode=memory connection from a
        # thread too, as Starlette's TestClient does.
        kwargs["connect_args"] = {"check_same_thread": False, **kwargs.get("connect_args", {})}
    return kwargs


def sync_engine(url: URL, options: Mapping[str, Any]) -> Engine:
    check_engine_options(url, options, for_async=False)
    chosen = "poolclass" in options or "pool" in options
    kwargs = dict(options) if chosen else _unpooled(options)
    if not _sqlite.is_sqlite(url):
        return create_engine(url, **kwargs)
    engine = create_engine(url, **_thread_safe(url, kwargs))
    _sqlite.apply_savepoint_fix(
        engine, kwargs.get("connect_args", {}), immediate=not _sqlite.is_shared_memory(url)
    )
    return engine


def async_engine(url: URL, options: Mapping[str, Any], *, pooled: bool) -> AsyncEngine:
    from sqlalchemy.ext.asyncio import create_async_engine

    check_engine_options(url, options, for_async=True)
    kwargs = {**options, "poolclass": AsyncAdaptedQueuePool} if pooled else _unpooled(options)
    engine = create_async_engine(url, **kwargs)
    if _sqlite.is_sqlite(url):
        _sqlite.apply_savepoint_fix(
            engine.sync_engine,
            kwargs.get("connect_args", {}),
            immediate=not _sqlite.is_shared_memory(url),
        )
    return engine


def _schema_options(url: URL, options: Mapping[str, Any], lock_timeout: float) -> dict[str, Any]:
    kwargs = _unpooled(options)
    if _sqlite.is_sqlite(url):
        # sqlite3's busy timeout is the lock wait: BEGIN IMMEDIATE blocks on it.
        kwargs["connect_args"] = {**kwargs.get("connect_args", {}), "timeout": lock_timeout}
    return kwargs


def schema_engine(url: URL, options: Mapping[str, Any], lock_timeout: float) -> Engine:
    check_engine_options(url, options, for_async=False)
    kwargs = _schema_options(url, options, lock_timeout)
    engine = create_engine(url, **kwargs)
    if _sqlite.is_sqlite(url):
        _sqlite.apply_savepoint_fix(engine, kwargs["connect_args"], immediate=True)
    return engine


def async_schema_engine(url: URL, options: Mapping[str, Any], lock_timeout: float) -> AsyncEngine:
    from sqlalchemy.ext.asyncio import create_async_engine

    check_engine_options(url, options, for_async=False)
    kwargs = _schema_options(url, options, lock_timeout)
    engine = create_async_engine(url, **kwargs)
    if _sqlite.is_sqlite(url):
        _sqlite.apply_savepoint_fix(engine.sync_engine, kwargs["connect_args"], immediate=True)
    return engine
