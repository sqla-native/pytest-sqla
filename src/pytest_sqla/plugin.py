"""The pytest plugin: options, the report header, and the fixtures.

pytest loads this module through the ``sqla`` entry point, and ``-p no:sqla``
disables it. It stays inert until a test requests a database fixture: a run
that never does imports no application code, opens no connection, never
imports ``sqlalchemy.ext.asyncio`` and registers no per-test hooks.

The public fixtures are thin dispatchers. Each checks what it can check
cheaply, names itself in the error when something is missing, and only then
requests the internal fixture that opens the connection. That keeps every
setup error on the test that hit it, worded for the fixture it asked for.
"""

from __future__ import annotations

import contextlib
import importlib
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, NoReturn

import pytest
from sqlalchemy.engine import URL, Connection, Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from . import SchemaConflictError, _asyncio, _config, _schema, _sqlite
from ._asyncio import AsyncState
from ._config import SETTINGS, Settings, Targets
from ._engines import async_engine, sync_engine
from ._isolation import Isolation, check_session_options, end_isolation, open_isolation

if TYPE_CHECKING:
    import sqlite3

    from sqlalchemy.ext.asyncio import (
        AsyncConnection,
        AsyncEngine,
        AsyncSession,
        async_sessionmaker,
    )

SYNC_FIXTURES = frozenset({"sqla_connection", "sqla_sessionmaker", "sqla_session"})
ASYNC_FIXTURES = frozenset(
    {"sqla_async_connection", "sqla_async_sessionmaker", "sqla_async_session"}
)
_SYNC_DRIVERS = {
    "postgresql": "postgresql+psycopg",
    "mysql": "mysql+pymysql",
    "mariadb": "mariadb+pymysql",
    "sqlite": "sqlite",
}
_ASYNC_DRIVERS = {
    "postgresql": "postgresql+asyncpg",
    "mysql": "mysql+aiomysql",
    "mariadb": "mariadb+aiomysql",
    "sqlite": "sqlite+aiosqlite",
}


# ---------------------------------------------------------------------- hooks


def pytest_addoption(parser: pytest.Parser) -> None:
    _config.add_options(parser)


def pytest_configure(config: pytest.Config) -> None:
    config.stash[SETTINGS] = _config.read_settings(config)


def pytest_report_header(config: pytest.Config) -> list[str]:
    """Say which database the run is configured for, so a green CI log shows what it proved.

    This is the configuration: an override of the sqla_url fixture, such as a
    container's URL, is not known before the first database test, and the
    connected URL is ``sqla_connection.engine.url``.
    """
    settings = config.stash[SETTINGS]
    parts = [
        f"{label}={_config.render(configured.url)} (from {configured.source})"
        for label, configured in (("url", settings.url), ("async_url", settings.async_url))
        if configured is not None
    ]
    if not parts:
        return []
    return [f"sqla: {', '.join(parts)}, schema={settings.schema_mode}"]


# ------------------------------------------------------- configuration fixtures


@pytest.fixture(scope="session")
def sqla_url(request: pytest.FixtureRequest) -> URL | None:
    """The database URL, or None when nothing is configured.

    From ``--sqla-url``, then ``PYTEST_SQLA_URL``, then the ``sqla_url`` ini key.
    Override it in a conftest to compute the URL, for example from a
    testcontainers or pytest-databases fixture; a str or a URL is accepted.
    pytest-sqla never changes this value: the URL it connects to, which may
    differ for in-memory SQLite and xdist, is ``sqla_connection.engine.url``.
    """
    configured = _settings(request).url
    return None if configured is None else configured.url


@pytest.fixture(scope="session")
def sqla_async_url(request: pytest.FixtureRequest, sqla_url: URL | str | None) -> URL | str | None:
    """The URL the async fixtures use; ``sqla_url`` unless set separately.

    From ``--sqla-async-url``, then ``PYTEST_SQLA_ASYNC_URL``, then the
    ``sqla_async_url`` ini key. Only needed when ``sqla_url`` uses a sync driver
    with no async twin, such as pymysql or pysqlite: ``postgresql+psycopg``
    serves both families. It must name the same database as ``sqla_url``.
    """
    configured = _settings(request).async_url
    return sqla_url if configured is None else configured.url


@pytest.fixture(scope="session")
def sqla_metadata(request: pytest.FixtureRequest) -> Any:
    """The metadata ``sqla_schema`` creates: a MetaData, a tuple of them, or None.

    Imported on first database use from the ``sqla_metadata`` ini key,
    ``"package.module:attribute"``. The attribute may be a MetaData, a class
    with one as ``.metadata`` (a DeclarativeBase, ``declarative_base()``,
    SQLModel), or a list or tuple of these. Override the fixture to supply
    the metadata in Python instead.
    """
    path = _settings(request).metadata_path
    if path is None:
        return None
    found = _config.metadatas(_config.load_metadata(path), f"sqla_metadata = {path!r}")
    return found[0] if len(found) == 1 else found


@pytest.fixture(scope="session")
def sqla_engine_options() -> dict[str, Any]:
    """Extra keyword arguments for every engine pytest-sqla creates. Default ``{}``.

    For example ``echo``, ``connect_args``, ``json_serializer`` or
    ``execution_options``. Refused: ``poolclass`` and ``pool`` for async
    engines; anything that commits each statement, such as
    ``isolation_level="AUTOCOMMIT"`` or ``autocommit=True`` in
    ``connect_args``; and ``isolation_level="READ UNCOMMITTED"``, which lets a
    test read other workers' rows. The sync engine is NullPool unless these
    choose a pool.
    """
    return {}


@pytest.fixture(scope="session")
def sqla_session_options() -> dict[str, Any]:
    """Extra keyword arguments for the sessionmakers. Default ``{}``.

    For example ``expire_on_commit``, ``autoflush``, ``info``, or ``class_``
    (sqlmodel's Session or AsyncSession), so the test sessions behave like the
    application's. ``bind``, ``binds`` and ``join_transaction_mode`` are
    refused: every session is bound to the test's connection.
    """
    return {}


@pytest.fixture(scope="session")
def sqla_schema(request: pytest.FixtureRequest) -> Iterator[None]:
    """Creates the metadata's tables before the first database test; drops them at the end.

    With ``sqla_schema = create`` (the default) the tables are created once per
    run under a database-level lock, joined by sibling xdist workers, and
    dropped by the last one out. Tables pytest-sqla did not create are refused,
    never dropped. With ``sqla_schema = none`` it does nothing. Override it to
    build the schema yourself, for example with Alembic.

    A SQLite database pytest-sqla made up - an in-memory one, or a per-worker
    file - is discarded at the end instead. On Python 3.11 and later an
    unread result a failing test left behind keeps a SQLite read lock until it
    is garbage-collected, and pytest keeps the last failure's traceback, so a
    DROP there would wait out the lock timeout.
    """
    __tracebackhide__ = True
    settings = _settings(request)
    if settings.schema_mode == "none":
        yield
        return
    targets: Targets = request.getfixturevalue("_sqla_urls")
    url = targets.ddl
    if url is None:
        _no_database("sqla_schema")
    plan = _schema_plan(request, settings, url)
    _reported(_schema.prepare, plan)
    yield
    if not targets.disposable:
        _reported(_schema.release, plan)
    elif targets.worker_file is not None:
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(targets.worker_file + suffix).unlink(missing_ok=True)


def _reported(step: Callable[[_schema.Plan], str], plan: _schema.Plan) -> None:
    """Run a schema step; a SchemaConflictError is re-raised here, without the DDL frames."""
    __tracebackhide__ = True
    try:
        step(plan)
    except SchemaConflictError as error:
        raise SchemaConflictError(*error.args) from None


def _schema_plan(request: pytest.FixtureRequest, settings: Settings, url: URL) -> _schema.Plan:
    value = request.getfixturevalue("sqla_metadata")
    described = "the value of the sqla_metadata fixture"
    metadatas = () if value is None else _config.metadatas(value, described)
    if not metadatas:
        pytest.fail(
            "pytest-sqla: sqla_schema = create needs your metadata. Set sqla_metadata = "
            "'app.models:Base' (a MetaData, a declarative base, or a list of them), or "
            "override the sqla_metadata fixture. If the database already has its schema, "
            "set sqla_schema = none.",
            pytrace=False,
        )
    _schema.check_metadatas(metadatas)
    request.getfixturevalue("_sqla_sqlite_keeper")
    return _schema.Plan(
        url=url,
        metadatas=metadatas,
        engine_options=request.getfixturevalue("sqla_engine_options"),
        lock_timeout=settings.lock_timeout,
        run_id=settings.run_id,
        worker=settings.worker,
        recreate=settings.recreate_schema,
    )


# ----------------------------------------------------------- internal fixtures


@pytest.fixture(scope="session")
def _sqla_urls(request: pytest.FixtureRequest, sqla_url: object, sqla_async_url: object) -> Targets:
    return _config.resolve_targets(sqla_url, sqla_async_url, _settings(request))


@pytest.fixture(scope="session")
def _sqla_sqlite_keeper(_sqla_urls: Targets) -> Iterator[sqlite3.Connection | None]:
    """Holds a shared in-memory SQLite database open for the whole session.

    SQLAlchemy's docs: dispose(), pool_recycle and NullPool all discard a
    shared memory database once its last connection closes. Measured, it
    survives dispose() while this connection is open and is empty after.
    """
    if _sqla_urls.keeper is None:
        yield None
        return
    keeper = _sqlite.open_keeper(_sqla_urls.keeper)
    yield keeper
    keeper.close()


@pytest.fixture(scope="session")
def _sqla_sync_engine(
    _sqla_urls: Targets, sqla_engine_options: dict[str, Any], _sqla_sqlite_keeper: object
) -> Iterator[Engine]:
    """NullPool, unless sqla_engine_options choose a pool: see ``_engines``."""
    assert _sqla_urls.sync is not None, "the dispatcher checked the URL"
    engine = sync_engine(_sqla_urls.sync, sqla_engine_options)
    yield engine
    engine.dispose()


@dataclass
class _SyncState:
    connection: Connection
    isolation: Isolation
    sessionmaker: sessionmaker[Session]
    fixture: str | None = None
    session: Session | None = None

    def claim(self, fixture: str) -> None:
        if self.fixture is None:
            self.fixture = fixture


@pytest.fixture
def _sqla_sync_transaction(
    request: pytest.FixtureRequest,
    sqla_schema: None,
    _sqla_sync_engine: Engine,
    sqla_session_options: dict[str, Any],
) -> Iterator[_SyncState]:
    __tracebackhide__ = True
    check_session_options(sqla_session_options)
    with _sqla_sync_engine.connect() as connection:
        isolation = open_isolation(connection)
        try:
            state = _SyncState(
                connection=connection,
                isolation=isolation,
                sessionmaker=sessionmaker(
                    bind=connection,
                    join_transaction_mode="create_savepoint",
                    **sqla_session_options,
                ),
            )
            yield state
            if state.session is not None:
                # A failure here is explained by end_isolation when it matters.
                with contextlib.suppress(DBAPIError):
                    state.session.close()
            breach = end_isolation(
                isolation, nodeid=request.node.nodeid, fixture=state.fixture or "sqla_connection"
            )
        finally:
            isolation.unwatch()
    if breach is not None:
        raise breach


@pytest.fixture(scope="session")
def _sqla_async_engine_function(
    _sqla_urls: Targets, sqla_engine_options: dict[str, Any], _sqla_sqlite_keeper: object
) -> Iterator[AsyncEngine]:
    """Serves per-test loops and anyio tests: NullPool, so no connection outlives its loop.

    Each test opens and closes its own DBAPI connection on its own loop. The
    extra cost per test against a pool, measured with scripts/benchmark.py on
    localhost Docker: 3.9 ms (psycopg) to 5.5 ms (asyncpg) on PostgreSQL, and
    1.2 ms on MySQL (aiomysql, asyncmy).
    """
    assert _sqla_urls.asynchronous is not None, "the dispatcher checked the URL"
    engine = async_engine(_sqla_urls.asynchronous, sqla_engine_options, pooled=False)
    yield engine
    engine.sync_engine.dispose()


@pytest.fixture(name=_asyncio.ANYIO_TRANSACTION)
async def _sqla_async_transaction_anyio(
    request: pytest.FixtureRequest,
    sqla_session_options: dict[str, Any],
    _sqla_async_engine_function: AsyncEngine,
) -> AsyncIterator[AsyncState]:
    """anyio runs this in the test's own runner, because anyio_backend is in the test's closure."""
    __tracebackhide__ = True
    async with _asyncio.open_transaction(
        _sqla_async_engine_function,
        sqla_session_options,
        nodeid=request.node.nodeid,
        scope=None,
        timeout=_settings(request).lock_timeout,
    ) as state:
        yield state


def _optional_import(name: str) -> ModuleType | None:
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


# The per-loop-scope variants exist only when pytest-asyncio is importable,
# and are requested only when its plugin is active.
globals().update(_asyncio.pytest_asyncio_fixtures(_optional_import("pytest_asyncio")))


# ------------------------------------------------------------- sync fixtures


@pytest.fixture
def sqla_connection(request: pytest.FixtureRequest) -> Connection:
    """A connection inside this test's transaction; everything on it is rolled back.

    One per test, a DBAPI connection of its own from a session-wide NullPool
    engine, after ``BEGIN`` and a guard SAVEPOINT. At teardown pytest-sqla
    rolls back and checks that nothing escaped: a commit of the connection
    itself or of a second connection from its engine, or the database ending
    the transaction on its own (MySQL DDL), fails this test with IsolationError.
    """
    return _sync_state(request, "sqla_connection").connection


@pytest.fixture
def sqla_sessionmaker(request: pytest.FixtureRequest) -> sessionmaker[Session]:
    """A sessionmaker bound to this test's connection, for dependency overrides.

    Every session it makes joins the test transaction with
    ``join_transaction_mode="create_savepoint"``: ``commit()`` releases a
    savepoint, ``rollback()`` rolls back to one, ``close()`` leaves the
    connection open, and all of it is rolled back at teardown.
    """
    return _sync_state(request, "sqla_sessionmaker").sessionmaker


@pytest.fixture
def sqla_session(request: pytest.FixtureRequest) -> Session:
    """A Session in this test's transaction; commit() freely, it is all rolled back.

    Made by ``sqla_sessionmaker`` and closed by pytest-sqla at teardown.
    """
    state = _sync_state(request, "sqla_session")
    state.session = state.sessionmaker()
    return state.session


# ------------------------------------------------------------ async fixtures


@pytest.fixture
def sqla_async_connection(request: pytest.FixtureRequest) -> AsyncConnection:
    """An AsyncConnection inside this test's transaction, opened on the test's event loop.

    Works under every pytest-asyncio loop scope, in strict and auto mode, and
    under anyio. Using it from another event loop raises LoopMismatchError
    before the driver sees the statement, COMMIT or ROLLBACK. Every task that
    uses it must be done before the test ends.
    """
    return _async_state(request, "sqla_async_connection").connection


@pytest.fixture
def sqla_async_sessionmaker(request: pytest.FixtureRequest) -> async_sessionmaker[AsyncSession]:
    """An async_sessionmaker bound to this test's connection, for dependency overrides.

    One AsyncSession per request, all inside the test transaction, as with
    ``sqla_sessionmaker``. Requests within one test must run one at a time: a
    connection serves one operation at a time.
    """
    return _async_state(request, "sqla_async_sessionmaker").sessionmaker


@pytest.fixture
def sqla_async_session(request: pytest.FixtureRequest) -> AsyncSession:
    """An AsyncSession in this test's transaction; commit() freely, it is all rolled back.

    Shares the connection with ``sqla_async_sessionmaker``'s sessions, so it
    sees what the application committed. Closed on the test's loop at teardown.
    """
    state = _async_state(request, "sqla_async_session")
    state.session = state.sessionmaker()
    return state.session


# ---------------------------------------------------------------- dispatching


def _settings(request: pytest.FixtureRequest) -> Settings:
    return request.config.stash[SETTINGS]


def _no_database(fixture: str) -> NoReturn:
    pytest.fail(
        f"pytest-sqla: {fixture} needs a database, and none is configured. Set --sqla-url, "
        "the PYTEST_SQLA_URL environment variable, or sqla_url in your pytest configuration "
        "(for example sqla_url = sqlite://).",
        pytrace=False,
    )


def _one_family(request: pytest.FixtureRequest) -> None:
    names = set(request.fixturenames)
    if names & SYNC_FIXTURES and names & ASYNC_FIXTURES:
        pytest.fail(
            f"pytest-sqla: {request.node.nodeid} uses both sync and async sqla fixtures. They "
            "hold separate connections and transactions, so neither would see the other's "
            "writes. Use one family per test.",
            pytrace=False,
        )


def _sync_state(request: pytest.FixtureRequest, fixture: str) -> _SyncState:
    _one_family(request)
    if request.getfixturevalue("sqla_url") is None:
        _no_database(fixture)
    targets: Targets = request.getfixturevalue("_sqla_urls")
    assert targets.sync is not None, "sqla_url is set"
    if targets.sync.get_dialect().is_async:
        backend = targets.sync.get_backend_name()
        example = _SYNC_DRIVERS.get(backend, f"{backend}+<driver>")
        pytest.fail(
            f"pytest-sqla: {fixture} is synchronous, but sqla_url uses the async driver "
            f"{targets.sync.get_driver_name()}. Use the sqla_async_* fixtures, or set sqla_url "
            f"to a synchronous driver for the same database (for example {example}://...) and "
            "sqla_async_url to the async one.",
            pytrace=False,
        )
    state: _SyncState = request.getfixturevalue("_sqla_sync_transaction")
    state.claim(fixture)
    return state


def _async_state(request: pytest.FixtureRequest, fixture: str) -> AsyncState:
    _one_family(request)
    internal = _asyncio.route(request)
    _asyncio.require_greenlet(fixture)
    if request.getfixturevalue("sqla_async_url") is None:
        _no_database(fixture)
    targets: Targets = request.getfixturevalue("_sqla_urls")
    url = targets.asynchronous
    assert url is not None, "sqla_async_url is set"
    if not url.get_dialect().get_async_dialect_cls(url).is_async:
        backend = url.get_backend_name()
        example = _ASYNC_DRIVERS.get(backend, f"{backend}+<async driver>")
        pytest.fail(
            f"pytest-sqla: {fixture} needs an async driver, but {targets.async_setting} uses "
            f"{url.get_driver_name()}. Set sqla_async_url to the same database with an async "
            f"driver (for example {example}://...).",
            pytrace=False,
        )
    request.getfixturevalue("sqla_schema")
    state: AsyncState = request.getfixturevalue(internal)
    state.claim(fixture, anyio_backend=_asyncio.anyio_backend_in_asyncio_test(request))
    return state
