"""Every engine pytest-sqla creates, and the options it refuses to pass on.

Three kinds:

* the session-wide **sync engine**, a normal QueuePool, because sync
  connections are not bound to an event loop;
* **async engines**: NullPool when one engine serves many event loops (a
  per-test loop, or anyio), which is SQLAlchemy's documented answer for
  multiple loops, and a real pool when the engine lives exactly as long as
  one loop (see ``_asyncio``);
* short-lived NullPool **schema engines** for DDL (see ``_schema``).

On SQLite each one gets the savepoint fix from ``_sqlite``. A pool class is
always passed explicitly for SQLite, because SQLAlchemy 2.1 deprecates choosing
one from a ``mode=memory`` URL and filterwarnings = error would fail the run.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool, QueuePool

from . import _sqlite

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


def check_engine_options(url: URL, options: Mapping[str, Any], *, for_async: bool) -> None:
    """Refuse the options that would make a rollback impossible, or a pool cross loops."""
    if for_async and ("poolclass" in options or "pool" in options):
        pytest.fail(
            "pytest-sqla: sqla_engine_options cannot set poolclass for async engines; "
            "pytest-sqla picks the pool per event loop (NullPool for a per-test loop, a pool "
            "that lives as long as the loop otherwise).",
            pytrace=False,
        )
    execution_options = options.get("execution_options") or {}
    levels = (options.get("isolation_level"), execution_options.get("isolation_level"))
    if "AUTOCOMMIT" in levels:
        pytest.fail(
            "pytest-sqla: isolation_level='AUTOCOMMIT' commits every statement, so pytest-sqla "
            "could not roll a test back. Remove it from sqla_engine_options.",
            pytrace=False,
        )
    autocommit = (options.get("connect_args") or {}).get("autocommit")
    if _sqlite.is_sqlite(url) and autocommit is True:
        pytest.fail(
            "pytest-sqla: connect_args autocommit=True makes sqlite3 commit every statement, "
            "so pytest-sqla could not roll a test back. Leave it unset, or set it to False.",
            pytrace=False,
        )


def _sqlite_options(url: URL, options: dict[str, Any], poolclass: type[Any]) -> dict[str, Any]:
    if "poolclass" not in options and "pool" not in options:
        options["poolclass"] = poolclass
    if _sqlite.is_shared_memory(url) and url.get_driver_name() == "pysqlite":
        # SQLAlchemy sets check_same_thread=False only for what it considers a
        # file database; a mode=memory URL behind a QueuePool needs it too.
        options["connect_args"] = {"check_same_thread": False, **options.get("connect_args", {})}
    return options


def sync_engine(url: URL, options: Mapping[str, Any]) -> Engine:
    check_engine_options(url, options, for_async=False)
    kwargs = dict(options)
    if not _sqlite.is_sqlite(url):
        return create_engine(url, **kwargs)
    engine = create_engine(url, **_sqlite_options(url, kwargs, QueuePool))
    _sqlite.apply_savepoint_fix(
        engine, kwargs.get("connect_args", {}), immediate=not _sqlite.is_shared_memory(url)
    )
    return engine


def async_engine(url: URL, options: Mapping[str, Any], *, pooled: bool) -> AsyncEngine:
    from sqlalchemy.ext.asyncio import create_async_engine

    check_engine_options(url, options, for_async=True)
    kwargs = {**options, "poolclass": AsyncAdaptedQueuePool if pooled else NullPool}
    engine = create_async_engine(url, **kwargs)
    if _sqlite.is_sqlite(url):
        _sqlite.apply_savepoint_fix(
            engine.sync_engine,
            kwargs.get("connect_args", {}),
            immediate=not _sqlite.is_shared_memory(url),
        )
    return engine


def _schema_options(url: URL, options: Mapping[str, Any], lock_timeout: float) -> dict[str, Any]:
    kwargs = {key: value for key, value in options.items() if key != "pool"}
    kwargs["poolclass"] = NullPool
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
