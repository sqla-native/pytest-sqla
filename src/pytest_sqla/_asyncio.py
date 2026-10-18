"""Async fixtures that open every connection on the test's own event loop.

**The problem.** pytest-asyncio 1.4.0 picks an async fixture's loop from the
fixture's ``loop_scope``, then ``asyncio_default_fixture_loop_scope``, then
its caching scope, and a test's loop from its ``asyncio`` marker or
``asyncio_default_test_loop_scope``. Nothing compares the two, so a fixture
and the test that uses it can run on different loops. Measured on SQLAlchemy
2.1.5 with a connection opened on one loop and used on another: asyncpg
0.32.0, aiomysql 0.3.2 and asyncmy 0.2.16 raise "attached to a different loop"
and leave the connection unusable; psycopg 3.3 async and aiosqlite 0.22 happen
to survive.

**The design.** The public async fixtures are synchronous dispatchers. Each
reads the test item and requests an internal fixture that runs on the test's
loop:

* an anyio test - ``anyio_backend`` among its fixtures, which is anyio's own
  criterion - gets a plain async fixture, which anyio runs in the test's
  runner;
* a pytest-asyncio test gets the variant declared with
  ``pytest_asyncio.fixture(loop_scope=S)`` for its loop scope ``S``, which
  pytest-asyncio runs on the same Runner as the test.

An engine that serves many loops - per-test loops, and anyio, whose runner's
lifetime is unknown - uses NullPool: "If the same engine must be shared
between different loop, it should be configured to disable pooling using
NullPool" (SQLAlchemy's asyncio docs). For a class, module, package or session
loop, the engine has a real pool, created, used and disposed on that one loop;
pytest-asyncio closes the loop only after the engine fixture's teardown.

A **loop guard** on every connection turns cross-loop use into
LoopMismatchError before the driver sees it. Measured on all five drivers,
SQLAlchemy 2.0.55 and 2.1.5: the error surfaces unchanged, the connection is
not invalidated, and teardown on the owning loop rolls back cleanly.

``sqlalchemy.ext.asyncio`` is imported only inside function bodies: on
SQLAlchemy 2.1 without greenlet, the import itself raises.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from . import LoopMismatchError
from ._config import Targets
from ._engines import async_engine
from ._isolation import Isolation, check_session_options, end_isolation, open_isolation

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import (
        AsyncConnection,
        AsyncEngine,
        AsyncSession,
        async_sessionmaker,
    )

LOOP_SCOPES = ("function", "class", "module", "package", "session")
ANYIO_TRANSACTION = "_sqla_async_transaction_anyio"


def transaction_fixture(scope: str) -> str:
    return f"_sqla_async_transaction_{scope}"


def engine_fixture(scope: str) -> str:
    return f"_sqla_async_engine_{scope}"


@dataclass
class LoopGuard:
    """A ``before_cursor_execute`` listener: the statement must run on ``loop``."""

    loop: asyncio.AbstractEventLoop
    nodeid: str
    scope: str | None
    """The pytest-asyncio loop scope; None for anyio."""
    fixture: str | None = None
    """The first public fixture that handed this connection out."""

    def __call__(self, *_: object) -> None:
        __tracebackhide__ = True
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            raise LoopMismatchError(self.explain("a thread with no running event loop")) from None
        if running is not self.loop:
            raise LoopMismatchError(self.explain("a different event loop"))

    def explain(self, used_from: str) -> str:
        if self.scope is None:
            runs_on = "anyio"
            advice = (
                "A fixture that uses it probably runs on another plugin's event loop; make it "
                "a plain async fixture, which anyio runs on the test's loop."
            )
        else:
            runs_on = f"pytest-asyncio loop scope {self.scope!r}"
            advice = (
                "A fixture that uses it probably runs on a different loop scope; give it the "
                "test's loop scope, for example asyncio_default_fixture_loop_scope = "
                f"{self.scope} or @pytest_asyncio.fixture(loop_scope={self.scope!r})."
            )
        return (
            f"pytest-sqla: {self.fixture} belongs to the event loop {self.nodeid} runs on "
            f"({runs_on}), but it was used from {used_from}. {advice} If the code under test "
            "runs in its own thread, as Starlette's TestClient does, call it through "
            "httpx.AsyncClient with ASGITransport instead."
        )


@dataclass
class AsyncState:
    """What the internal fixture hands the three public async dispatchers."""

    connection: AsyncConnection
    isolation: Isolation
    sessionmaker: async_sessionmaker[AsyncSession]
    guard: LoopGuard
    session: AsyncSession | None = None

    def claim(self, fixture: str) -> None:
        if self.guard.fixture is None:
            self.guard.fixture = fixture


def route(request: pytest.FixtureRequest) -> str:
    """The internal fixture that runs on this test's event loop."""
    item = request.node
    plugins = request.config.pluginmanager
    if "anyio_backend" in request.fixturenames and plugins.hasplugin("anyio"):
        backend = request.getfixturevalue("anyio_backend")
        name = backend if isinstance(backend, str) else backend[0]
        if name != "asyncio":
            pytest.fail(
                f"pytest-sqla: {item.nodeid} runs on the anyio backend {name!r}, but "
                "SQLAlchemy's asyncio extension only runs on asyncio. Override the "
                "anyio_backend fixture to return 'asyncio'.",
                pytrace=False,
            )
        if plugins.hasplugin("asyncio") and _asyncio_mode(request.config) == "auto":
            pytest.fail(
                f"pytest-sqla: {item.nodeid} is an anyio test, but pytest-asyncio runs in auto "
                "mode and would run this test's async fixtures on its own event loop. Set "
                "asyncio_mode = strict, or mark the test with @pytest.mark.asyncio instead of "
                "@pytest.mark.anyio.",
                pytrace=False,
            )
        return ANYIO_TRANSACTION
    if plugins.hasplugin("asyncio") and importlib.import_module("pytest_asyncio").is_async_test(
        item
    ):
        return transaction_fixture(loop_scope(item))
    pytest.fail(
        f"pytest-sqla: {request.fixturename} needs an async test, but no async plugin runs "
        f"{item.nodeid}. Mark it with @pytest.mark.asyncio (pip install pytest-sqla[asyncio]) "
        "or @pytest.mark.anyio, or set asyncio_mode = auto.",
        pytrace=False,
    )


def _asyncio_mode(config: pytest.Config) -> str:
    return str(config.getoption("asyncio_mode", None) or config.getini("asyncio_mode"))


def loop_scope(item: pytest.Item) -> str:
    """The loop a pytest-asyncio test runs on: its marker, else the ini default.

    The same rule pytest-asyncio applies, through its public marker and ini
    semantics: the closest ``asyncio`` marker's ``loop_scope`` (or the
    deprecated ``scope``), else ``asyncio_default_test_loop_scope``.
    """
    marker = item.get_closest_marker("asyncio")
    assert marker is not None, "pytest-asyncio marks every test it runs"
    scope = marker.kwargs.get("loop_scope") or marker.kwargs.get("scope")
    return str(scope or item.config.getini("asyncio_default_test_loop_scope"))


def require_greenlet(fixture: str) -> None:
    try:
        importlib.import_module("greenlet")
        importlib.import_module("sqlalchemy.ext.asyncio")
    except ImportError:
        pytest.fail(
            f"pytest-sqla: {fixture} needs greenlet, which SQLAlchemy 2.1 no longer installs "
            "by default. Install pytest-sqla[asyncio] (or sqlalchemy[asyncio]).",
            pytrace=False,
        )


def _begin(connection: Connection, guard: LoopGuard) -> Isolation:
    event.listen(connection, "before_cursor_execute", guard)
    return open_isolation(connection)


@contextlib.asynccontextmanager
async def open_transaction(
    engine: AsyncEngine,
    session_options: dict[str, Any],
    *,
    nodeid: str,
    scope: str | None,
) -> AsyncIterator[AsyncState]:
    """One connection on the running loop, in the test transaction, guarded.

    Leaving the block ends the transaction as ``_isolation`` does, awaited on
    the same loop, closes the connection, and then raises any breach.
    """
    __tracebackhide__ = True
    from sqlalchemy.ext.asyncio import async_sessionmaker

    check_session_options(session_options)
    async with engine.connect() as connection:
        guard = LoopGuard(asyncio.get_running_loop(), nodeid, scope)
        isolation = await connection.run_sync(_begin, guard)
        state = AsyncState(
            connection=connection,
            isolation=isolation,
            sessionmaker=async_sessionmaker(
                bind=connection, join_transaction_mode="create_savepoint", **session_options
            ),
            guard=guard,
        )
        yield state
        if state.session is not None:
            # A failure here is explained by end_isolation when it matters.
            with contextlib.suppress(DBAPIError):
                await state.session.close()
        breach = await connection.run_sync(
            lambda _connection: end_isolation(
                isolation, nodeid=nodeid, fixture=guard.fixture or "sqla_async_connection"
            )
        )
    if breach is not None:
        raise breach


def _transaction_fixture(scope: str) -> Callable[..., AsyncIterator[AsyncState]]:
    engine_name = engine_fixture(scope)

    async def transaction(
        request: pytest.FixtureRequest, sqla_session_options: dict[str, Any], **engines: Any
    ) -> AsyncIterator[AsyncState]:
        __tracebackhide__ = True
        async with open_transaction(
            engines[engine_name], sqla_session_options, nodeid=request.node.nodeid, scope=scope
        ) as state:
            yield state

    # The engine is a static dependency, so pytest-asyncio sets it up on this
    # scope's loop before the transaction; a running async fixture cannot call
    # request.getfixturevalue() ("Runner.run() cannot be called from a running
    # event loop").
    transaction.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [
            inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD),
            inspect.Parameter("sqla_session_options", inspect.Parameter.POSITIONAL_OR_KEYWORD),
            inspect.Parameter(engine_name, inspect.Parameter.KEYWORD_ONLY),
        ]
    )
    return transaction


def _engine_fixture() -> Callable[..., AsyncIterator[AsyncEngine]]:
    async def engine(
        _sqla_urls: Targets, sqla_engine_options: dict[str, Any], _sqla_sqlite_keeper: object
    ) -> AsyncIterator[AsyncEngine]:
        assert _sqla_urls.asynchronous is not None, "the dispatcher checked the URL"
        pooled = async_engine(_sqla_urls.asynchronous, sqla_engine_options, pooled=True)
        yield pooled
        await pooled.dispose()

    return engine


def pytest_asyncio_fixtures(pytest_asyncio: ModuleType | None) -> dict[str, Any]:
    """The per-loop-scope fixtures, keyed by name; none without pytest-asyncio.

    ``pytest_asyncio.fixture(loop_scope=S)`` is the only declaration strict
    mode honours: a plain async fixture requested by a strict-mode asyncio test
    draws pytest-asyncio's "will become an error" deprecation warning. A
    function-loop transaction uses the session-wide NullPool engine; every
    other loop scope gets an engine of its own scope and loop.
    """
    if pytest_asyncio is None:
        return {}
    fixtures: dict[str, Any] = {}
    for scope in LOOP_SCOPES:
        name = transaction_fixture(scope)
        fixtures[name] = pytest_asyncio.fixture(
            _transaction_fixture(scope), loop_scope=scope, name=name
        )
        if scope != "function":
            name = engine_fixture(scope)
            fixtures[name] = pytest_asyncio.fixture(
                _engine_fixture(), scope=scope, loop_scope=scope, name=name
            )
    return fixtures
