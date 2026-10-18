"""aiosqlite: one in-memory database for both families, DDL from an async-only URL,
and the async engine options."""

from __future__ import annotations

import pytest

from tests.helpers import Project, sqlite_script, table_names


def test_sync_and_async_engines_share_one_in_memory_database(project: Project) -> None:
    """The schema is created through the sync URL; the async engine must see it."""
    project.models()
    project.ini(sqla_url="sqlite://", sqla_async_url="sqlite+aiosqlite://")
    project.conftest(
        """
        import pytest

        SEEN = {}

        @pytest.fixture
        def anyio_backend():
            return "asyncio"
        """
    )
    project.tests(
        """
        import pytest
        from sqlalchemy import inspect
        from conftest import SEEN

        def test_sync(sqla_connection):
            SEEN["sync"] = sqla_connection.engine.url.database
            assert "user_account" in inspect(sqla_connection).get_table_names()

        @pytest.mark.asyncio
        async def test_async(sqla_async_connection):
            SEEN["async"] = sqla_async_connection.engine.url.database
            names = await sqla_async_connection.run_sync(
                lambda connection: inspect(connection).get_table_names()
            )
            assert "user_account" in names
            assert SEEN["async"] == SEEN["sync"]
            assert SEEN["sync"].startswith("file:/pytest-sqla-")
        """
    )
    project.run().assert_outcomes(passed=2)


def test_an_async_only_url_creates_and_drops_the_schema_from_a_thread(project: Project) -> None:
    project.models()
    path = project.path / "test.db"
    project.ini(sqla_url=f"sqlite+aiosqlite:///{path}")
    project.tests(
        """
        import pytest
        from sqlalchemy import inspect

        @pytest.mark.asyncio
        async def test_schema(sqla_async_connection):
            names = await sqla_async_connection.run_sync(
                lambda connection: inspect(connection).get_table_names()
            )
            assert sorted(names) == ["note", "pytest_sqla", "user_account"]
        """
    )
    project.run().assert_outcomes(passed=1)
    assert table_names(f"sqlite:///{path}") == []


def test_a_conflict_found_on_the_schema_thread_reaches_the_tests(project: Project) -> None:
    project.models()
    path = project.path / "test.db"
    sqlite_script(path, ["CREATE TABLE note (id INTEGER PRIMARY KEY)"])
    project.ini(sqla_url=f"sqlite+aiosqlite:///{path}")
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
        ["*SchemaConflictError: pytest-sqla: the database at*already has tables*: note.*"]
    )


def test_an_in_memory_async_only_url_works_too(project: Project) -> None:
    project.models()
    project.ini(sqla_url="sqlite+aiosqlite://")
    project.tests(
        """
        import pytest
        from models import User, acount

        @pytest.mark.asyncio
        async def test_commits(sqla_async_session):
            sqla_async_session.add(User(email="ada@example.com"))
            await sqla_async_session.commit()
            assert await acount(sqla_async_session) == 1

        @pytest.mark.asyncio
        async def test_rolled_back(sqla_async_session):
            assert await acount(sqla_async_session) == 0
        """
    )
    project.run().assert_outcomes(passed=2)


# ------------------------------------------------------------- engine options

ASYNC_TEST = """
import pytest

@pytest.mark.asyncio(loop_scope="{scope}")
async def test_db(sqla_async_session):
    pass
"""


@pytest.mark.parametrize("scope", ["function", "module"])
@pytest.mark.parametrize("key", ["poolclass", "pool"])
def test_an_async_engine_refuses_a_pool(project: Project, key: str, scope: str) -> None:
    project.models()
    project.ini(sqla_url="sqlite+aiosqlite://")
    project.conftest(
        f"""
        import pytest
        from sqlalchemy.pool import StaticPool

        @pytest.fixture(scope="session")
        def sqla_engine_options():
            return {{{key!r}: StaticPool}}
        """
    )
    project.tests(ASYNC_TEST.format(scope=scope))
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            "*pytest-sqla: sqla_engine_options cannot set poolclass for async engines; "
            "pytest-sqla picks the pool per event loop (NullPool for a per-test loop, a pool that "
            "lives as long as the loop otherwise)."
        ]
    )


def test_async_session_options_reach_the_sessions(project: Project) -> None:
    project.models()
    project.ini(sqla_url="sqlite+aiosqlite://")
    project.conftest(
        """
        import pytest
        from sqlalchemy.ext.asyncio import AsyncSession

        class AppSession(AsyncSession):
            pass

        @pytest.fixture(scope="session")
        def sqla_session_options():
            return {"class_": AppSession, "expire_on_commit": False}
        """
    )
    project.tests(
        """
        import pytest
        from conftest import AppSession
        from models import User

        @pytest.mark.asyncio
        async def test_session(sqla_async_session, sqla_async_sessionmaker):
            assert isinstance(sqla_async_session, AppSession)
            assert isinstance(sqla_async_sessionmaker(), AppSession)
            user = User(email="ada@example.com")
            sqla_async_session.add(user)
            await sqla_async_session.commit()
            assert user.email == "ada@example.com"  # no lazy refresh needed
        """
    )
    project.run().assert_outcomes(passed=1)


def test_async_session_options_cannot_rebind(project: Project) -> None:
    project.models()
    project.ini(sqla_url="sqlite+aiosqlite://")
    project.conftest(
        """
        import pytest

        @pytest.fixture(scope="session")
        def sqla_session_options():
            return {"binds": {}}
        """
    )
    project.tests(ASYNC_TEST.format(scope="function"))
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*pytest-sqla: sqla_session_options cannot set 'binds'*"])
