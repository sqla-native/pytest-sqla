"""Create the metadata's tables once per run, own them, and drop only those.

**The race.** A naive session fixture calling ``create_all`` in every xdist
worker, with ``-n 4``, ended "2 passed, 6 errors" on 3 of 3 runs per backend:
PostgreSQL 17 raised UniqueViolation on ``pg_type_typname_nsp_index``, MySQL
8.4 raised "(1050, "Table 'race_t0' already exists")". So creation and drop
run in one transaction under a database-level lock:

* PostgreSQL: ``pg_advisory_xact_lock`` with a constant key, released at
  commit together with the transactional DDL. "Advisory locks are local to
  each database" (PostgreSQL 17 docs), so one key is safe across databases;
* MySQL and MariaDB: ``GET_LOCK`` on the schema connection. It is session-level,
  so it survives MySQL's implicit DDL commits and dies with the connection;
* SQLite: ``BEGIN IMMEDIATE``. In-memory databases are per process and create
  mode gives each xdist worker its own file, so this only orders separate runs.

**Ownership.** A test plugin must never destroy data it did not create. The
marker table ``pytest_sqla`` records which run created the schema and how many
xdist workers still use it; tables that exist without it are refused (S1), and
the worker that brings the count to 0 drops exactly the metadata's tables.

**Never hang.** DDL waits for locks a leaked transaction may hold forever.
``sqla_lock_timeout`` bounds every wait: measured, a drop behind an open
transaction failed after 2.0 s with PostgreSQL sqlstate 55P03 and MySQL 1205,
and without the timeout it blocked indefinitely.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import math
import os
import socket
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, TypeVar

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, func, inspect, select
from sqlalchemy.engine import URL, Connection
from sqlalchemy.exc import DBAPIError

from . import SchemaConflictError
from ._config import render
from ._engines import async_schema_engine, schema_engine

MARKER_NAME = "pytest_sqla"
_ADVISORY_KEY = 0x7079_7465_7374_5351  # "pytestSQ", which fits a signed bigint
_AUTOCOMMITTING_DDL = frozenset({"mysql", "mariadb"})
_LOCKING_DIALECTS = frozenset({"postgresql", "mysql", "mariadb", "sqlite"})

MARKER = Table(
    MARKER_NAME,
    MetaData(),
    Column("run_id", String(64), primary_key=True),
    Column("workers", Integer, nullable=False),
    Column("started_at", String(32), nullable=False),
    Column("owner", String(255), nullable=False),
)

# Shown when the marker table exists but holds no row: on MySQL, a run killed
# between creating the table and inserting its row leaves exactly that.
_UNKNOWN_RUN = SimpleNamespace(started_at="at an unknown time", owner="an unknown run", workers=0)

_T = TypeVar("_T")


@dataclass(frozen=True)
class Plan:
    """Everything the schema fixture decided before touching the database."""

    url: URL
    metadatas: tuple[MetaData, ...]
    engine_options: Mapping[str, Any]
    lock_timeout: float
    run_id: str
    worker: str | None
    recreate: bool

    @property
    def owner(self) -> str:
        return f"{socket.gethostname()}:{os.getpid()}/{self.worker or 'main'}"


def check_metadatas(metadatas: tuple[MetaData, ...]) -> None:
    if any(
        table.name == MARKER_NAME for metadata in metadatas for table in metadata.tables.values()
    ):
        raise SchemaConflictError(
            "pytest-sqla: your metadata defines a table named pytest_sqla, which pytest-sqla "
            "uses to track the schema it creates. Rename that table."
        )


def prepare(plan: Plan) -> str:
    """Create the schema, or join the sibling worker that did. Returns what happened."""
    dialect = plan.url.get_backend_name()
    if plan.worker is not None and dialect not in _LOCKING_DIALECTS:
        pytest.fail(
            "pytest-sqla: cannot serialise schema creation across pytest-xdist workers on "
            f"{dialect}; supported are PostgreSQL, MySQL, MariaDB and SQLite. Create the "
            "schema before the run and set sqla_schema = none, or run without -n.",
            pytrace=False,
        )
    return _in_schema_transaction(plan, _create_or_join, ending=False)


def release(plan: Plan) -> str:
    """Leave the schema; the last worker out drops it. Returns what happened."""
    return _in_schema_transaction(plan, _leave, ending=True)


# ------------------------------------------------------------------ the work


def _create_or_join(connection: Connection, plan: Plan) -> str:
    if not inspect(connection).has_table(MARKER_NAME):
        _refuse_foreign_tables(connection, plan)
        _build(connection, plan, create_marker=True)
        return "created"
    bumped = connection.execute(
        MARKER.update().where(MARKER.c.run_id == plan.run_id).values(workers=MARKER.c.workers + 1)
    )
    if bumped.rowcount:
        return "joined"
    if not plan.recreate:
        other = connection.execute(select(MARKER)).first() or _UNKNOWN_RUN
        raise SchemaConflictError(
            f"pytest-sqla: the database at {render(plan.url)} holds the schema of another "
            f"pytest-sqla run (started {other.started_at} by {other.owner}; {other.workers} "
            "worker(s) still running, or crashed). If no other test run uses this database, "
            "run again with --sqla-recreate-schema; otherwise give this run a database of its "
            "own."
        )
    for metadata in reversed(plan.metadatas):
        metadata.drop_all(connection)
    connection.execute(MARKER.delete())
    _build(connection, plan, create_marker=False)
    return "recreated"


def _refuse_foreign_tables(connection: Connection, plan: Plan) -> None:
    inspector = inspect(connection)
    existing = sorted(
        table.fullname
        for metadata in plan.metadatas
        for table in metadata.tables.values()
        if inspector.has_table(table.name, schema=table.schema)
    )
    if existing:
        raise SchemaConflictError(
            f"pytest-sqla: the database at {render(plan.url)} already has tables that "
            f"pytest-sqla did not create: {', '.join(existing)}. It will not drop or replace "
            "them. Point sqla_url at a database used only for tests, drop those tables "
            "yourself, or set sqla_schema = none to run against them as they are."
        )


def _build(connection: Connection, plan: Plan, *, create_marker: bool) -> None:
    """The marker row, then every table. On MySQL a failure undoes it by hand.

    PostgreSQL and SQLite roll the whole transaction back, DDL included. MySQL
    commits each DDL statement on its own, so the tables created before the
    failure would stay; they are dropped here (S1 proved none existed before).
    Other dialects are left to their own transaction.
    """
    try:
        if create_marker:
            MARKER.create(connection)
        connection.execute(
            MARKER.insert().values(
                run_id=plan.run_id,
                workers=1,
                started_at=datetime.now(UTC).isoformat(timespec="seconds"),
                owner=plan.owner,
            )
        )
        for metadata in plan.metadatas:
            metadata.create_all(connection)
    except Exception:
        if connection.dialect.name in _AUTOCOMMITTING_DDL:
            for metadata in reversed(plan.metadatas):
                metadata.drop_all(connection, checkfirst=True)
            MARKER.drop(connection, checkfirst=True)
        raise


def _leave(connection: Connection, plan: Plan) -> str:
    ours = MARKER.c.run_id == plan.run_id
    connection.execute(MARKER.update().where(ours).values(workers=MARKER.c.workers - 1))
    left = connection.scalar(select(MARKER.c.workers).where(ours))
    # None: our row is gone, because another run recreated the schema. Its
    # tables are not ours any more, so they stay.
    if left is None or left > 0:
        return "left"
    for metadata in reversed(plan.metadatas):
        metadata.drop_all(connection)
    MARKER.drop(connection)
    return "dropped"


# --------------------------------------------------------- locks, per dialect

_Work = Callable[[Connection, Plan], str]


def _lock_timed_out(connection: Connection, error: DBAPIError) -> bool:
    dialect = connection.dialect.name
    if dialect == "postgresql":
        return getattr(error.orig, "sqlstate", None) == "55P03"
    if dialect in ("mysql", "mariadb"):
        return getattr(error.orig, "args", (None,))[:1] == (1205,)
    return "database is locked" in str(error.orig)


def _waited_too_long(plan: Plan, error: DBAPIError | None = None) -> SchemaConflictError:
    detail = "" if error is None else f" The driver reported: {error.orig}"
    return SchemaConflictError(
        f"pytest-sqla: gave up after {plan.lock_timeout:g} s waiting for the schema lock on "
        f"{render(plan.url)}; another pytest-sqla process is creating or dropping the schema. "
        f"A crashed run releases the lock when its connection closes.{detail}"
    )


def _blocked_drop(plan: Plan, error: DBAPIError) -> SchemaConflictError:
    unread = (
        " On SQLite, so can a query result a failing test left unread: Python 3.11+ keeps "
        "its read lock until the result is garbage-collected."
        if plan.url.get_backend_name() == "sqlite"
        else ""
    )
    return SchemaConflictError(
        f"pytest-sqla: could not drop the test schema in {render(plan.url)} within "
        f"{plan.lock_timeout:g} s: another connection still holds a lock on one of its tables. "
        "Some code in this run opened its own connection (for example through the "
        f"application's own engine) and left a transaction open.{unread} The driver "
        f"reported: {error.orig}"
    )


def _blocked_creation(plan: Plan, error: DBAPIError) -> SchemaConflictError:
    return SchemaConflictError(
        f"pytest-sqla: gave up after {plan.lock_timeout:g} s waiting for a lock on the tables "
        f"of the test schema in {render(plan.url)}: another connection holds one, usually a "
        "test of the pytest-sqla run whose schema this is, still going. Give this run a "
        "database of its own, or wait for that run to finish. The driver reported: "
        f"{error.orig}"
    )


_Explanation = Callable[[Plan, DBAPIError], SchemaConflictError]


@contextlib.contextmanager
def _waiting(connection: Connection, plan: Plan, explain: _Explanation) -> Iterator[None]:
    """Turn a lock wait that timed out into the error that explains it.

    ``explain`` names the cause: the schema lock held by another process
    (S3), a drop held up by a leaked transaction (S4), or creation - in
    practice --sqla-recreate-schema dropping another run's tables - held up by
    a run still using them.
    """
    try:
        yield
    except DBAPIError as error:
        if not _lock_timed_out(connection, error):
            raise
        raise explain(plan, error) from error


def _do(connection: Connection, plan: Plan, work: _Work, ending: bool) -> str:
    with _waiting(connection, plan, _blocked_drop if ending else _blocked_creation):
        return work(connection, plan)


def _postgresql(connection: Connection, plan: Plan, work: _Work, ending: bool) -> str:
    milliseconds = max(1, round(plan.lock_timeout * 1000))
    with connection.begin():
        connection.exec_driver_sql(f"SET LOCAL lock_timeout = '{milliseconds}ms'")
        with _waiting(connection, plan, _waited_too_long):
            connection.execute(select(func.pg_advisory_xact_lock(_ADVISORY_KEY)))
        return _do(connection, plan, work, ending)


def _mysql(connection: Connection, plan: Plan, work: _Work, ending: bool) -> str:
    seconds = max(1, math.ceil(plan.lock_timeout))
    name = "pytest_sqla:" + hashlib.sha1(str(plan.url.database).encode()).hexdigest()
    connection.exec_driver_sql(f"SET SESSION lock_wait_timeout = {seconds}")
    acquired = connection.scalar(select(func.get_lock(name, seconds)))
    connection.commit()
    if acquired != 1:
        raise _waited_too_long(plan)
    try:
        with connection.begin():
            return _do(connection, plan, work, ending)
    finally:
        connection.scalar(select(func.release_lock(name)))
        connection.commit()


def _sqlite(connection: Connection, plan: Plan, work: _Work, ending: bool) -> str:
    # BEGIN IMMEDIATE (through the listener) waits for other writers, and
    # COMMIT for readers. At session end the likely holder is a test's leak.
    explain = _blocked_drop if ending else _waited_too_long
    with _waiting(connection, plan, explain), connection.begin():
        return _do(connection, plan, work, ending)


def _unlocked(connection: Connection, plan: Plan, work: _Work, ending: bool) -> str:
    """Any other dialect, in a single process: prepare() refused it under xdist."""
    with connection.begin():
        return _do(connection, plan, work, ending)


_LOCKS = {"postgresql": _postgresql, "mysql": _mysql, "mariadb": _mysql, "sqlite": _sqlite}


def _locked(connection: Connection, plan: Plan, work: _Work, ending: bool) -> str:
    lock = _LOCKS.get(connection.dialect.name, _unlocked)
    return lock(connection, plan, work, ending)


# ------------------------------------------------------- sync or async driver


def _in_schema_transaction(plan: Plan, work: _Work, *, ending: bool) -> str:
    if plan.url.get_dialect().is_async:
        return _in_thread(lambda: asyncio.run(_async_schema_transaction(plan, work, ending)))
    engine = schema_engine(plan.url, plan.engine_options, plan.lock_timeout)
    try:
        with engine.connect() as connection:
            return _locked(connection, plan, work, ending)
    finally:
        engine.dispose()


async def _async_schema_transaction(plan: Plan, work: _Work, ending: bool) -> str:
    engine = async_schema_engine(plan.url, plan.engine_options, plan.lock_timeout)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(_locked, plan, work, ending)
    finally:
        await engine.dispose()


def _in_thread(function: Callable[[], _T]) -> _T:
    """Run ``function`` on a thread of its own, re-raising whatever it raised.

    An async-only URL needs an event loop for its DDL. A thread of its own keeps
    ``asyncio.run()`` away from the loops pytest-asyncio and anyio manage.
    """
    outcome: dict[str, Any] = {}

    def target() -> None:
        try:
            outcome["value"] = function()
        except BaseException as error:  # handed to the calling thread below
            outcome["error"] = error

    thread = threading.Thread(target=target, name="pytest-sqla-schema")
    thread.start()
    thread.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]  # type: ignore[no-any-return]
