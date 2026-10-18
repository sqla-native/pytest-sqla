"""Every async object lives on the event loop its test runs on.

aiosqlite happens to survive a connection crossing loops, which is exactly why
these tests can trust a passing statement: the loop guard raises
LoopMismatchError before the driver is reached, so a statement that runs was
on the right loop. The real-database suite repeats the matrix on asyncpg,
aiomysql and asyncmy, where crossing loops breaks the connection.
"""

from __future__ import annotations

import sys

import pytest

from tests.helpers import Project

TWO_TESTS = """
import pytest
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool
from conftest import ENGINES
from models import User, acount

{marker}
async def test_commits(sqla_async_session, sqla_async_connection):
    ENGINES.setdefault("{key}", []).append(sqla_async_connection.engine.sync_engine)
    sqla_async_session.add(User(email="ada@example.com"))
    await sqla_async_session.commit()
    assert await acount(sqla_async_session) == 1

{marker}
async def test_rolled_back(sqla_async_session, sqla_async_connection):
    ENGINES["{key}"].append(sqla_async_connection.engine.sync_engine)
    assert await acount(sqla_async_session) == 0
    first, second = ENGINES["{key}"]
    assert first is second
    assert isinstance(first.pool, {pool})
"""
CONFTEST = """
import pytest

ENGINES = {}

@pytest.fixture
def anyio_backend():
    return "asyncio"
"""


def loop_project(project: Project, mode: str) -> Project:
    project.models()
    project.ini(sqla_url="sqlite+aiosqlite://", asyncio_mode=mode)
    project.conftest(CONFTEST)
    return project


def marker_for(scope: str, mode: str) -> str:
    if scope == "function":
        return "@pytest.mark.asyncio" if mode == "strict" else ""
    return f'@pytest.mark.asyncio(loop_scope="{scope}")'


@pytest.mark.parametrize("mode", ["strict", "auto"])
@pytest.mark.parametrize("scope", ["function", "module", "package", "session"])
def test_every_loop_scope_by_marker(project: Project, mode: str, scope: str) -> None:
    loop_project(project, mode)
    pool = "NullPool" if scope == "function" else "AsyncAdaptedQueuePool"
    source = TWO_TESTS.format(marker=marker_for(scope, mode), key=scope, pool=pool)
    project.pytester.mkpydir("pkg")
    project.tests(source, name="pkg/test_loop")
    project.run().assert_outcomes(passed=2)


@pytest.mark.parametrize("mode", ["strict", "auto"])
def test_the_class_loop_scope(project: Project, mode: str) -> None:
    loop_project(project, mode)
    project.tests(
        """
        import pytest
        from sqlalchemy.pool import AsyncAdaptedQueuePool
        from models import User, acount

        @pytest.mark.asyncio(loop_scope="class")
        class TestClassLoop:
            engines = []

            async def test_commits(self, sqla_async_session, sqla_async_connection):
                self.engines.append(sqla_async_connection.engine.sync_engine)
                sqla_async_session.add(User(email="ada@example.com"))
                await sqla_async_session.commit()
                assert await acount(sqla_async_session) == 1

            async def test_rolled_back(self, sqla_async_session, sqla_async_connection):
                self.engines.append(sqla_async_connection.engine.sync_engine)
                assert await acount(sqla_async_session) == 0
                first, second = self.engines
                assert first is second
                assert isinstance(first.pool, AsyncAdaptedQueuePool)
        """
    )
    project.run().assert_outcomes(passed=2)


@pytest.mark.parametrize("mode", ["strict", "auto"])
def test_the_ini_default_test_loop_scope(project: Project, mode: str) -> None:
    loop_project(project, mode)
    project.ini(asyncio_default_test_loop_scope="module")
    marker = "@pytest.mark.asyncio" if mode == "strict" else ""
    project.tests(TWO_TESTS.format(marker=marker, key="ini", pool="AsyncAdaptedQueuePool"))
    project.run().assert_outcomes(passed=2)


def test_both_defaults_on_the_session_loop(project: Project) -> None:
    """A user fixture on the session loop and tests on the session loop agree."""
    loop_project(project, "strict")
    project.ini(
        asyncio_default_fixture_loop_scope="session", asyncio_default_test_loop_scope="session"
    )
    project.tests(
        """
        import pytest
        import pytest_asyncio
        from models import User, acount

        @pytest_asyncio.fixture
        async def seeded(sqla_async_session):
            sqla_async_session.add(User(email="seed@example.com"))
            await sqla_async_session.commit()
            return sqla_async_session

        @pytest.mark.asyncio
        async def test_seeded(seeded):
            assert await acount(seeded) == 1

        @pytest.mark.asyncio
        async def test_again(seeded):
            assert await acount(seeded) == 1
        """
    )
    project.run().assert_outcomes(passed=2)


def test_the_deprecated_scope_keyword_is_honoured(project: Project) -> None:
    loop_project(project, "strict")
    project.ini(filterwarnings="error\n    ignore::pytest.PytestDeprecationWarning")
    project.tests(
        TWO_TESTS.format(
            marker='@pytest.mark.asyncio(scope="module")', key="old", pool="AsyncAdaptedQueuePool"
        )
    )
    project.run().assert_outcomes(passed=2)


def test_pools_live_as_long_as_their_loop(project: Project) -> None:
    """One pooled engine per module loop, disposed with it; the session loop shares one."""
    loop_project(project, "strict")
    body = """
import pytest
from conftest import ENGINES

@pytest.mark.asyncio(loop_scope="module")
async def test_module_loop(sqla_async_connection):
    ENGINES.setdefault("module", []).append(sqla_async_connection.engine.sync_engine)

@pytest.mark.asyncio(loop_scope="session")
async def test_session_loop(sqla_async_connection):
    ENGINES.setdefault("session", []).append(sqla_async_connection.engine.sync_engine)
"""
    project.tests(body, name="test_a")
    project.tests(body, name="test_b")
    project.tests(
        """
        from conftest import ENGINES

        def test_engines():
            first, second = ENGINES["module"]
            assert first is not second
            assert first.pool.checkedin() == 0, "disposed when its module's loop ended"
            first, second = ENGINES["session"]
            assert first is second
        """,
        name="test_z",
    )
    project.run().assert_outcomes(passed=5)


# ----------------------------------------------------------------------- anyio


@pytest.mark.parametrize("plugins", [(), ("-p", "no:asyncio")], ids=["with", "without"])
def test_anyio_tests_with_and_without_pytest_asyncio(
    project: Project, plugins: tuple[str, ...]
) -> None:
    loop_project(project, "strict")
    if plugins:
        project.ini(asyncio_mode=None, asyncio_default_fixture_loop_scope=None)
    project.tests(TWO_TESTS.format(marker="@pytest.mark.anyio", key="anyio", pool="NullPool"))
    project.run(*plugins).assert_outcomes(passed=2)


def test_anyio_backend_options_are_fine(project: Project) -> None:
    loop_project(project, "strict")
    project.conftest(
        CONFTEST.replace('return "asyncio"', 'return ("asyncio", {"use_uvloop": False})')
    )
    project.tests(TWO_TESTS.format(marker="@pytest.mark.anyio", key="anyio", pool="NullPool"))
    project.run().assert_outcomes(passed=2)


def test_anyio_under_auto_mode_is_refused(project: Project) -> None:
    loop_project(project, "auto")
    project.tests(
        """
        import pytest

        @pytest.mark.anyio
        async def test_anyio(sqla_async_session):
            pass
        """
    )
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            "*pytest-sqla: test_inner.py::test_anyio is an anyio test, but pytest-asyncio runs "
            "in auto mode and would run this test's async fixtures on its own event loop. Set "
            "asyncio_mode = strict, or mark the test with @pytest.mark.asyncio instead of "
            "@pytest.mark.anyio."
        ]
    )


def test_the_trio_backend_is_refused(project: Project) -> None:
    loop_project(project, "strict")
    project.conftest(CONFTEST.replace('return "asyncio"', 'return "trio"'))
    project.tests(
        """
        import pytest

        @pytest.mark.anyio
        async def test_trio(sqla_async_connection):
            pass
        """
    )
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            "*pytest-sqla: test_inner.py::test_trio runs on the anyio backend 'trio', but "
            "SQLAlchemy's asyncio extension only runs on asyncio. Override the anyio_backend "
            "fixture to return 'asyncio'."
        ]
    )


# ------------------------------------------------------------ the loop guard


def test_a_fixture_on_another_loop_scope_is_reported(project: Project) -> None:
    loop_project(project, "strict")
    project.tests(
        """
        import pytest
        import pytest_asyncio
        from sqlalchemy import text
        from models import acount

        @pytest_asyncio.fixture
        async def seeded(sqla_async_session):
            await sqla_async_session.execute(text("SELECT 1"))
            return sqla_async_session

        @pytest.mark.asyncio(loop_scope="module")
        async def test_module_loop(seeded):
            pass

        @pytest.mark.asyncio(loop_scope="module")
        async def test_the_next_test_is_fine(sqla_async_session):
            assert await acount(sqla_async_session) == 0
        """
    )
    result = project.run()
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        [
            "E *pytest_sqla.LoopMismatchError: pytest-sqla: sqla_async_session belongs to the "
            "event loop test_inner.py::test_module_loop runs on (pytest-asyncio loop scope "
            "'module'), but it was used from a different event loop. A fixture that uses it "
            "probably runs on a different loop scope; give it the test's loop scope, for "
            "example asyncio_default_fixture_loop_scope = module or "
            "@pytest_asyncio.fixture(loop_scope='module'). If the code under test runs in its "
            "own thread, as Starlette's TestClient does, call it through httpx.AsyncClient with "
            "ASGITransport instead."
        ]
    )
    assert "ERROR at teardown" not in result.stdout.str()


def test_the_guard_reports_a_thread_with_no_running_loop() -> None:
    """Called directly: aiosqlite needs an await to create its cursor, so SQLAlchemy
    raises MissingGreenlet in such a thread before the guard could. asyncpg and
    psycopg create cursors synchronously, and the real-database suite meets the
    guard there."""
    import asyncio
    import threading

    from pytest_sqla import LoopMismatchError
    from pytest_sqla._asyncio import LoopGuard

    loop = asyncio.new_event_loop()
    guard = LoopGuard(loop, "test_x.py::test_y", "module", "sqla_async_session")
    errors: list[str] = []

    def use() -> None:
        try:
            guard()
        except LoopMismatchError as error:
            errors.append(str(error))

    thread = threading.Thread(target=use)
    thread.start()
    thread.join()
    loop.close()
    assert errors == [
        "pytest-sqla: sqla_async_session belongs to the event loop test_x.py::test_y runs on "
        "(pytest-asyncio loop scope 'module'), but it was used from a thread with no running "
        "event loop. A fixture that uses it probably runs on a different loop scope; give it "
        "the test's loop scope, for example asyncio_default_fixture_loop_scope = module or "
        "@pytest_asyncio.fixture(loop_scope='module'). If the code under test runs in its own "
        "thread, as Starlette's TestClient does, call it through httpx.AsyncClient with "
        "ASGITransport instead."
    ]


def test_the_guard_lets_a_callback_on_the_right_loop_through() -> None:
    """Called directly: a callback has no task to record, and is still on the right loop."""
    import asyncio

    from pytest_sqla._asyncio import LoopGuard

    loop = asyncio.new_event_loop()
    guard = LoopGuard(loop, "test_x.py::test_y", "function", "sqla_async_connection")
    done = loop.create_future()
    loop.call_soon(lambda: done.set_result(guard()))
    loop.run_until_complete(done)
    loop.close()
    assert list(guard.tasks) == []


def test_an_app_on_its_own_loop_in_a_thread_is_reported(project: Project) -> None:
    """The shape of Starlette's TestClient: the app runs on a loop of its own."""
    loop_project(project, "strict")
    project.tests(
        """
        import asyncio
        import threading
        import pytest
        from sqlalchemy import text
        from pytest_sqla import LoopMismatchError
        from models import User, acount

        @pytest.mark.anyio
        async def test_threaded_app(sqla_async_sessionmaker, sqla_async_session):
            errors = []

            async def endpoint():
                async with sqla_async_sessionmaker() as session:
                    await session.execute(text("SELECT 1"))

            def portal():
                try:
                    asyncio.run(endpoint())
                except LoopMismatchError as error:
                    errors.append(str(error))

            thread = threading.Thread(target=portal)
            thread.start()
            thread.join()
            assert len(errors) == 1
            assert "(anyio), but it was used from a different event loop" in errors[0]
            assert "make it a plain async fixture, which anyio runs" in errors[0]
            assert await acount(sqla_async_session) == 0
        """
    )
    project.run().assert_outcomes(passed=1)


def test_a_missing_greenlet_is_reported(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    loop_project(project, "strict")
    monkeypatch.setitem(sys.modules, "greenlet", None)
    project.tests(
        """
        import pytest

        @pytest.mark.asyncio
        async def test_db(sqla_async_session):
            pass
        """
    )
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            "*pytest-sqla: sqla_async_session needs greenlet, which SQLAlchemy 2.1 no longer "
            "installs by default. Install pytest-sqla[[]asyncio] (or sqlalchemy[[]asyncio])."
        ]
    )


@pytest.mark.parametrize("call", ["rollback", "commit"])
def test_a_root_commit_or_rollback_from_another_loop_is_stopped(
    project: Project, call: str
) -> None:
    """They reach the driver without a statement. Measured before: a cross-loop root
    rollback raised a raw RuntimeError on asyncpg, aiomysql and asyncmy and left the
    pooled connection out of step for the next test; aiosqlite let both through, and the
    commit made the next test count a row."""
    loop_project(project, "strict")
    project.tests(
        f"""
        import pytest
        import pytest_asyncio
        from sqlalchemy import insert
        from models import User, acount

        @pytest_asyncio.fixture
        async def ends_it_on_the_function_loop(sqla_async_connection):
            yield
            await sqla_async_connection.{call}()

        @pytest.mark.asyncio(loop_scope="module")
        async def test_module_loop(ends_it_on_the_function_loop, sqla_async_connection):
            await sqla_async_connection.execute(insert(User).values(email="ada@example.com"))

        @pytest.mark.asyncio(loop_scope="module")
        async def test_next(sqla_async_session):
            assert await acount(sqla_async_session) == 0
        """
    )
    result = project.run()
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(
        [
            "*ERROR at teardown of test_module_loop*",
            "E *pytest_sqla.LoopMismatchError: pytest-sqla: sqla_async_connection belongs to the "
            "event loop test_inner.py::test_module_loop runs on (pytest-asyncio loop scope "
            "'module'), but it was used from a different event loop.*",
        ]
    )
    result.stdout.no_fnmatch_line("*IsolationError*")


def test_a_thread_is_stopped_before_sqlalchemy_makes_a_cursor(project: Project) -> None:
    """aiosqlite awaits to make a cursor, and SQLAlchemy raised MissingGreenlet there before
    before_cursor_execute could run; before_execute comes first. exec_driver_sql() has no
    before_execute event, so it still meets MissingGreenlet in a thread on aiosqlite."""
    loop_project(project, "strict")
    project.tests(
        """
        import asyncio
        import pytest
        from sqlalchemy import text
        from pytest_sqla import LoopMismatchError

        @pytest.mark.asyncio
        async def test_from_a_thread(sqla_async_connection):
            sync_connection = sqla_async_connection.sync_connection
            with pytest.raises(LoopMismatchError, match="a thread with no running event loop"):
                await asyncio.to_thread(sync_connection.execute, text("SELECT 1"))
            assert await sqla_async_connection.scalar(text("SELECT 1")) == 1
        """
    )
    project.run().assert_outcomes(passed=1)


# ---------------------------------------- anyio_backend in a pytest-asyncio test

ANYIO_HELPER = (
    CONFTEST
    + '''
@pytest.fixture
def needs_anyio_backend(anyio_backend):
    """A helper written for anyio tests, reused by a pytest-asyncio test."""
    return anyio_backend
'''
)
HELPER_TEST = """
import pytest
from models import User, acount

{marker}
async def test_helper(needs_anyio_backend, sqla_async_session):
    sqla_async_session.add(User(email="ada@example.com"))
    await sqla_async_session.commit()
    assert await acount(sqla_async_session) == 1
"""


@pytest.mark.parametrize("mode", ["strict", "auto"])
def test_pytest_asyncio_runs_its_test_even_with_anyio_backend_among_its_fixtures(
    project: Project, mode: str
) -> None:
    """Routed by is_async_test() first. Measured before: the strict test got a connection
    from anyio's loop, and the auto one was refused as "an anyio test" it was not.

    ``-p anyio`` registers anyio first, which puts pytest-asyncio's fixture wrapper
    outermost: then pytest-asyncio runs every async fixture of its tests.
    """
    loop_project(project, mode)
    project.conftest(ANYIO_HELPER)
    marker = "@pytest.mark.asyncio" if mode == "strict" else ""
    project.tests(HELPER_TEST.format(marker=marker))
    project.run("-p", "anyio").assert_outcomes(passed=1)


def test_with_anyio_wrapping_the_fixtures_the_guard_names_the_cause(project: Project) -> None:
    """``-p asyncio`` registers pytest-asyncio first: anyio's fixture wrapper is then
    outermost and runs every async fixture, pytest_asyncio ones included, on anyio's loop.

    In process, the stdlib asyncio is already imported, which pytest warns about when
    ``-p asyncio`` marks that name for assertion rewriting.
    """
    loop_project(project, "strict")
    project.conftest(ANYIO_HELPER)
    project.tests(HELPER_TEST.format(marker="@pytest.mark.asyncio"))
    result = project.run("-p", "asyncio", "-W", "ignore::pytest.PytestAssertRewriteWarning")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "E *pytest_sqla.LoopMismatchError: pytest-sqla: sqla_async_session was opened on "
            "another event loop than the one test_inner.py::test_helper runs on. pytest-asyncio "
            "runs this test, but anyio_backend is among its fixtures, directly or through a "
            "fixture it uses, and anyio then runs async fixtures, pytest-sqla's included, on an "
            "event loop of its own. Keep anyio_backend out of this test's fixtures, or make it "
            "an anyio test with @pytest.mark.anyio."
        ]
    )


def test_a_sync_test_with_an_async_fixture_is_told_it_is_synchronous(project: Project) -> None:
    """Measured before: "no async plugin runs" it, with pytest-asyncio running the fixture."""
    loop_project(project, "strict")
    project.tests(
        """
        import pytest_asyncio

        @pytest_asyncio.fixture
        async def seeded(sqla_async_session):
            return "seeded"

        def test_sync(seeded):
            assert seeded == "seeded"
        """
    )
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            "*pytest-sqla: sqla_async_session needs an async test, but "
            "test_inner.py::test_sync is a plain def.*"
        ]
    )
