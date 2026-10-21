"""Async fixtures that open every connection on the test's own event loop.

**The problem.** pytest-asyncio 1.4.0 picks an async fixture's loop from the
fixture's ``loop_scope``, then ``asyncio_default_fixture_loop_scope``, then
its caching scope, and a test's loop from its ``asyncio`` marker or
``asyncio_default_test_loop_scope``. Nothing compares the two, so a fixture
and the test that uses it can run on different loops. Measured on SQLAlchemy
2.1.5 with a connection opened on one loop and used on another: asyncpg
0.32.0, aiomysql 0.3.2 and asyncmy 0.2.16 raise "attached to a different
loop", and asyncpg's connection is unusable afterwards; psycopg 3.3 async and
aiosqlite 0.22 happen to survive (tests/integration pins all of it).

**The design.** The public async fixtures are synchronous dispatchers. Each
reads the test item and requests an internal fixture that runs on the test's
loop:

* a pytest-asyncio test - ``is_async_test()`` - gets the variant declared
  with ``pytest_asyncio.fixture(loop_scope=S)`` for its loop scope ``S``, which
  pytest-asyncio runs on the same Runner as the test. pytest-asyncio runs such
  a test even with ``anyio_backend`` among its fixtures; only an ``anyio``
  marker makes it anyio's;
* an anyio test - ``anyio_backend`` among its fixtures, which is anyio's own
  criterion - gets a plain async fixture, which anyio runs in the test's
  runner.

With ``anyio_backend`` in a pytest-asyncio test's closure, which plugin runs
the async fixtures depends on the order the two registered: measured, with
anyio's fixture wrapper outermost it runs every async fixture on anyio's own
loop, pytest_asyncio fixtures included, while pytest-asyncio runs the test.
The loop guard then names that cause.

An engine that serves many loops - per-test loops, and anyio, whose runner's
lifetime is unknown - uses NullPool: "If the same engine must be shared
between different loop, it should be configured to disable pooling using
NullPool" (SQLAlchemy's asyncio docs). For a class, module, package or session
loop, the engine has a real pool, created, used and disposed on that one loop;
pytest-asyncio closes the loop only after the engine fixture's teardown.

A **loop guard** on every connection turns cross-loop use into
LoopMismatchError before the driver sees it: every statement, and the root
COMMIT and ROLLBACK, which reach the driver without one. Measured on all five
drivers, SQLAlchemy 2.0.55 and 2.1.5: the error surfaces unchanged, the
connection is not invalidated, and teardown on the owning loop rolls back
cleanly.

**Tasks the test leaves behind.** SQLAlchemy's asyncio connector serialises
statements but not a connection's commit and rollback, so a task the test
started and never awaited races the teardown on the same connection. Measured:
asyncpg failed with "another operation is in progress", aiomysql with
"readexactly() called while another coroutine is already waiting", psycopg
warned "nested transaction already deassociated", and asyncmy hung forever.
The guard records every task that used the connection; when one is still
running at teardown, pytest-sqla closes the connection instead of rolling back
next to it - the database then rolls the test back - and reports it. The whole
teardown is bounded by ``sqla_lock_timeout`` the same way.

``sqlalchemy.ext.asyncio`` is imported only inside function bodies: on
SQLAlchemy 2.1 without greenlet, the import itself raises.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import importlib
import inspect
import weakref
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from types import ModuleType
from typing import TYPE_CHECKING, Any, NoReturn

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from . import IsolationError, LoopMismatchError
from ._config import SETTINGS, Targets
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
# before_execute runs before SQLAlchemy creates the cursor, which aiosqlite,
# psycopg, aiomysql and asyncmy await: measured, a task's first await on
# aiosqlite came before before_cursor_execute. exec_driver_sql() skips
# before_execute, so before_cursor_execute still guards it. COMMIT and
# ROLLBACK reach the driver without a statement.
_GUARDED_EVENTS = ("before_execute", "before_cursor_execute", "commit", "rollback")


def transaction_fixture(scope: str) -> str:
    return f"_sqla_async_transaction_{scope}"


def engine_fixture(scope: str) -> str:
    return f"_sqla_async_engine_{scope}"


@dataclass(eq=False)
class LoopGuard:
    """A connection listener: the statement, commit or rollback must run on ``loop``.

    Hashed by identity, as SQLAlchemy's event registry needs of a listener.
    """

    loop: asyncio.AbstractEventLoop
    nodeid: str
    scope: str | None
    """The pytest-asyncio loop scope; None for anyio."""
    fixture: str | None = None
    """The first public fixture that handed this connection out."""
    anyio_backend: bool = False
    """pytest-asyncio runs the test, but anyio_backend is among its fixtures."""
    tasks: weakref.WeakSet[asyncio.Task[Any]] = field(default_factory=weakref.WeakSet)
    """Every task that has used the connection."""

    def __call__(self, *_: object) -> None:
        __tracebackhide__ = True
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            raise LoopMismatchError(self.explain("a thread with no running event loop")) from None
        if running is not self.loop:
            raise LoopMismatchError(
                self.explain_anyio_backend()
                if self.anyio_backend
                else self.explain("a different event loop")
            )
        task = asyncio.current_task()
        if task is not None:
            self.tasks.add(task)

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

    def explain_anyio_backend(self) -> str:
        return (
            f"pytest-sqla: {self.fixture} was opened on another event loop than the one "
            f"{self.nodeid} runs on. pytest-asyncio runs this test, but anyio_backend is among "
            "its fixtures, directly or through a fixture it uses, and anyio then runs async "
            "fixtures, pytest-sqla's included, on an event loop of its own. Keep anyio_backend "
            "out of this test's fixtures, or make it an anyio test with @pytest.mark.anyio."
        )

    def running_elsewhere(self) -> list[asyncio.Task[Any]]:
        """The tasks, other than this one, that used the connection and have not finished."""
        current = asyncio.current_task()
        return [task for task in self.tasks if task is not current and not task.done()]


@dataclass
class AsyncState:
    """What the internal fixture hands the three public async dispatchers."""

    connection: AsyncConnection
    isolation: Isolation
    sessionmaker: async_sessionmaker[AsyncSession]
    guard: LoopGuard
    session: AsyncSession | None = None

    def claim(self, fixture: str, *, anyio_backend: bool) -> None:
        if self.guard.fixture is None:
            self.guard.fixture = fixture
        self.guard.anyio_backend = anyio_backend


def _pytest_asyncio_runs(request: pytest.FixtureRequest) -> bool:
    plugins = request.config.pluginmanager
    return plugins.hasplugin("asyncio") and bool(
        importlib.import_module("pytest_asyncio").is_async_test(request.node)
    )


def _anyio_runs(request: pytest.FixtureRequest) -> bool:
    return "anyio_backend" in request.fixturenames and request.config.pluginmanager.hasplugin(
        "anyio"
    )


def route(request: pytest.FixtureRequest) -> str:
    """The internal fixture that runs on this test's event loop."""
    item = request.node
    anyio = _anyio_runs(request)
    if _pytest_asyncio_runs(request) and not (anyio and item.get_closest_marker("anyio")):
        return transaction_fixture(loop_scope(item))
    if anyio:
        backend = request.getfixturevalue("anyio_backend")
        name = backend if isinstance(backend, str) else backend[0]
        if name != "asyncio":
            pytest.fail(
                f"pytest-sqla: {item.nodeid} runs on the anyio backend {name!r}, but "
                "SQLAlchemy's asyncio extension only runs on asyncio. Override the "
                "anyio_backend fixture to return 'asyncio'.",
                pytrace=False,
            )
        plugins = request.config.pluginmanager
        if plugins.hasplugin("asyncio") and _asyncio_mode(request.config) == "auto":
            pytest.fail(
                f"pytest-sqla: {item.nodeid} is an anyio test, but pytest-asyncio runs in auto "
                "mode and would run this test's async fixtures on its own event loop. Set "
                "asyncio_mode = strict, or mark the test with @pytest.mark.asyncio instead of "
                "@pytest.mark.anyio.",
                pytrace=False,
            )
        return ANYIO_TRANSACTION
    _no_async_test(request)


def _no_async_test(request: pytest.FixtureRequest) -> NoReturn:
    item = request.node
    function = getattr(item, "obj", None)
    if not (inspect.iscoroutinefunction(function) or inspect.isasyncgenfunction(function)):
        pytest.fail(
            f"pytest-sqla: {request.fixturename} needs an async test, but {item.nodeid} is a "
            "plain def. Its connection is opened on the event loop the test runs on, and a "
            "synchronous test has none, even when an async fixture of it does. Make the test "
            "async def, marked with @pytest.mark.asyncio (pip install pytest-sqla[asyncio]) or "
            "@pytest.mark.anyio.",
            pytrace=False,
        )
    pytest.fail(
        f"pytest-sqla: {request.fixturename} needs an async test, but no async plugin runs "
        f"{item.nodeid}. Mark it with @pytest.mark.asyncio (pip install pytest-sqla[asyncio]) "
        "or @pytest.mark.anyio, or set asyncio_mode = auto.",
        pytrace=False,
    )


def anyio_backend_in_asyncio_test(request: pytest.FixtureRequest) -> bool:
    """pytest-asyncio runs the test, and anyio_backend is among its fixtures anyway."""
    return _pytest_asyncio_runs(request) and _anyio_runs(request)


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
    # Before open_isolation's own commit listener: a commit stopped on the
    # wrong loop never reached the database, so it must not count as one.
    for name in _GUARDED_EVENTS:
        event.listen(connection, name, guard)
    return open_isolation(connection)


@contextlib.asynccontextmanager
async def open_transaction(
    engine: AsyncEngine,
    session_options: dict[str, Any],
    *,
    nodeid: str,
    scope: str | None,
    timeout: float,
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
        try:
            state = AsyncState(
                connection=connection,
                isolation=isolation,
                sessionmaker=async_sessionmaker(
                    bind=connection, join_transaction_mode="create_savepoint", **session_options
                ),
                guard=guard,
            )
            yield state
            breach = await _end(state, timeout)
        finally:
            isolation.unwatch()
    if breach is not None:
        raise breach


async def _end(state: AsyncState, timeout: float) -> IsolationError | None:
    """End the test transaction, or close the connection when that is not safe."""
    __tracebackhide__ = True
    connection, guard = state.connection, state.guard
    try:
        left = guard.running_elsewhere()
        if left:
            await connection.invalidate()
            return IsolationError(_left_running(guard, left))
        try:
            return await asyncio.wait_for(_rollback(state), timeout)
        except TimeoutError:
            await connection.invalidate()
            return IsolationError(_too_slow(guard, timeout))
    finally:
        if connection.invalidated and connection.dialect.name == "sqlite":
            _release_sqlite_locks()


async def _rollback(state: AsyncState) -> IsolationError | None:
    __tracebackhide__ = True
    if state.session is not None:
        # A failure here is explained by end_isolation when it matters.
        with contextlib.suppress(DBAPIError):
            await state.session.close()
    guard = state.guard
    fixture = guard.fixture or "sqla_async_connection"
    return await state.connection.run_sync(
        lambda _connection: end_isolation(state.isolation, nodeid=guard.nodeid, fixture=fixture)
    )


def _release_sqlite_locks() -> None:
    """Let an invalidated aiosqlite connection really close.

    Measured: asyncio.wait_for cancelling a slow query invalidates the
    connection, and aiosqlite closes it, but sqlite3 keeps a connection whose
    statement is still referenced - here from the cancellation's traceback,
    in a reference cycle - open until garbage collection finalizes it, with
    its transaction's write lock. The next test failed with "database is
    locked" after the 5 s busy timeout; collecting garbage here released it.
    """
    gc.collect()


def _left_running(guard: LoopGuard, tasks: list[asyncio.Task[Any]]) -> str:
    names = ", ".join(sorted(_task_name(task) for task in tasks))
    return (
        f"pytest-sqla: {guard.nodeid} left {len(tasks)} task(s) running that use its "
        f"connection: {names}. A connection serves one operation at a time, so instead of "
        "rolling the test transaction back next to them, pytest-sqla closed the connection, and "
        "the database rolled the test's work back. Await or cancel such tasks before the test "
        "ends."
    )


def _too_slow(guard: LoopGuard, timeout: float) -> str:
    return (
        f"pytest-sqla: rolling back {guard.nodeid}'s transaction took longer than "
        f"sqla_lock_timeout ({timeout:g} s), so pytest-sqla closed the connection instead, and "
        "the database rolled the test's work back. Usually a task the test left running uses "
        "the connection at the same time; await or cancel such tasks before the test ends."
    )


def _task_name(task: asyncio.Task[Any]) -> str:
    return str(getattr(task.get_coro(), "__qualname__", None) or task.get_name())


def _transaction_fixture(scope: str) -> Callable[..., AsyncIterator[AsyncState]]:
    engine_name = engine_fixture(scope)

    async def transaction(
        request: pytest.FixtureRequest, sqla_session_options: dict[str, Any], **engines: Any
    ) -> AsyncIterator[AsyncState]:
        __tracebackhide__ = True
        async with open_transaction(
            engines[engine_name],
            sqla_session_options,
            nodeid=request.node.nodeid,
            scope=scope,
            timeout=request.config.stash[SETTINGS].lock_timeout,
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
