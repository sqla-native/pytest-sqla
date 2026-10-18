"""Per-test isolation through the async fixtures, on aiosqlite.

The same behaviours as ``tests/sync/test_isolation_sync.py``, under
pytest-asyncio and under anyio. The real-database suite repeats them on
asyncpg, psycopg, aiomysql and asyncmy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import Project, sqlite_rows, sqlite_script

TABLES = (
    "CREATE TABLE user_account (id INTEGER PRIMARY KEY, email VARCHAR(100) UNIQUE)",
    "CREATE TABLE note (id INTEGER PRIMARY KEY, user_id INTEGER, body VARCHAR(200))",
)
ANYIO_BACKEND = """
import pytest

@pytest.fixture
def anyio_backend():
    return "asyncio"
"""


@pytest.fixture
def database(project: Project) -> Path:
    project.models()
    path = project.path / "test.db"
    project.ini(sqla_url=f"sqlite+aiosqlite:///{path}", sqla_schema="none")
    sqlite_script(path, TABLES)
    return path


def users(path: Path) -> list[str]:
    return [str(row[0]) for row in sqlite_rows(path, "SELECT email FROM user_account ORDER BY id")]


@pytest.mark.parametrize("marker", ["asyncio", "anyio"])
def test_a_commit_is_invisible_to_the_next_test(
    project: Project, database: Path, marker: str
) -> None:
    project.conftest(ANYIO_BACKEND)
    project.tests(
        f"""
        import pytest
        from models import User, acount

        pytestmark = pytest.mark.{marker}

        async def test_commits(sqla_async_session):
            sqla_async_session.add(User(email="ada@example.com"))
            await sqla_async_session.commit()
            assert await acount(sqla_async_session) == 1

        async def test_sees_nothing(sqla_async_session):
            assert await acount(sqla_async_session) == 0

        async def test_commits_through_the_sessionmaker(sqla_async_sessionmaker):
            async with sqla_async_sessionmaker() as session:
                session.add(User(email="ada@example.com"))
                await session.commit()
                assert await acount(session) == 1

        async def test_still_nothing(sqla_async_connection):
            from sqlalchemy import text
            assert await sqla_async_connection.scalar(
                text("SELECT count(*) FROM user_account")
            ) == 0
        """
    )
    project.run().assert_outcomes(passed=4)
    assert users(database) == []


def test_an_app_rollback_after_an_integrity_error_keeps_the_seeded_rows(
    project: Project, database: Path
) -> None:
    project.tests(
        """
        import pytest
        from sqlalchemy.exc import IntegrityError
        from models import User, acount

        @pytest.mark.asyncio
        async def test_duplicate(sqla_async_session, sqla_async_sessionmaker):
            sqla_async_session.add(User(email="seed@example.com"))
            await sqla_async_session.commit()

            async with sqla_async_sessionmaker() as app:
                app.add(User(email="seed@example.com"))
                with pytest.raises(IntegrityError):
                    await app.commit()
                await app.rollback()
                assert await acount(app) == 1

            assert await acount(sqla_async_session) == 1
        """
    )
    project.run().assert_outcomes(passed=1)
    assert users(database) == []


def test_sessions_left_open_are_not_a_breach(project: Project, database: Path) -> None:
    project.tests(
        """
        import pytest
        from models import User, acount

        @pytest.mark.asyncio
        async def test_untidy(sqla_async_session, sqla_async_sessionmaker):
            assert await acount(sqla_async_session) == 0
            first = sqla_async_sessionmaker()
            first.add(User(email="first@example.com"))
            await first.flush()
            second = sqla_async_sessionmaker()
            assert await acount(second) == 1
            await first.close()
        """
    )
    project.run().assert_outcomes(passed=1)
    assert users(database) == []


@pytest.mark.parametrize("marker", ["asyncio", "anyio"])
def test_committing_the_connection_is_reported(
    project: Project, database: Path, marker: str
) -> None:
    project.conftest(ANYIO_BACKEND)
    project.tests(
        f"""
        import pytest
        from models import User

        @pytest.mark.{marker}
        async def test_escapes(sqla_async_session):
            sqla_async_session.add(User(email="leak@example.com"))
            await sqla_async_session.flush()
            connection = await sqla_async_session.connection()
            await connection.commit()
        """
    )
    result = project.run()
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        [
            "E *pytest_sqla.IsolationError: pytest-sqla: test_inner.py::test_escapes* committed "
            "the test transaction, so its writes are now permanent and visible to later tests. "
            "Something called commit() on the connection behind sqla_async_session*"
        ]
    )
    assert users(database) == ["leak@example.com"]


def test_the_database_ending_the_transaction_is_reported(project: Project, database: Path) -> None:
    project.tests(
        """
        import pytest
        from sqlalchemy import text
        from models import User

        @pytest.mark.asyncio
        async def test_ends_it(sqla_async_session):
            sqla_async_session.add(User(email="leak@example.com"))
            await sqla_async_session.flush()
            await sqla_async_session.execute(text("COMMIT"))
        """
    )
    result = project.run()
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        [
            "E *pytest_sqla.IsolationError: pytest-sqla: the database ended "
            "test_inner.py::test_ends_it's transaction on its own*The driver reported: no such "
            "savepoint: sa_savepoint_1"
        ]
    )
    assert users(database) == ["leak@example.com"]


def test_rolling_the_connection_back_is_not_a_breach(project: Project, database: Path) -> None:
    project.tests(
        """
        import pytest
        from models import User

        @pytest.mark.asyncio
        async def test_rollback(sqla_async_session, sqla_async_connection):
            sqla_async_session.add(User(email="gone@example.com"))
            await sqla_async_session.flush()
            await sqla_async_connection.rollback()
        """
    )
    project.run().assert_outcomes(passed=1)
    assert users(database) == []


def test_a_failing_test_and_its_breach_are_both_reported(project: Project, database: Path) -> None:
    project.tests(
        """
        import pytest
        from models import User

        @pytest.mark.asyncio
        async def test_both(sqla_async_connection):
            from sqlalchemy import text
            await sqla_async_connection.execute(
                text("INSERT INTO user_account (email) VALUES ('leak@example.com')")
            )
            await sqla_async_connection.commit()
            assert False, "the test's own failure"
        """
    )
    result = project.run()
    result.assert_outcomes(failed=1, errors=1)
    result.stdout.fnmatch_lines(["*the test's own failure*"])
    result.stdout.fnmatch_lines(["*connection behind sqla_async_connection*"])


def test_the_async_family_cannot_be_mixed_with_the_sync_one(
    project: Project, database: Path
) -> None:
    project.tests(
        """
        import pytest

        @pytest.mark.asyncio
        async def test_mixed(sqla_async_session, sqla_connection):
            pass
        """
    )
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*test_inner.py::test_mixed uses both sync and async sqla*"])


@pytest.mark.parametrize(
    ("url", "setting", "driver", "example"),
    [
        ("sqlite:///{path}", "sqla_url", "pysqlite", "sqlite+aiosqlite"),
        ("mysql+pymysql://ada@localhost/app", "sqla_url", "pymysql", "mysql+aiomysql"),
        ("mssql+pyodbc://ada@localhost/app", "sqla_url", "pyodbc", "mssql+<async driver>"),
    ],
)
def test_an_async_fixture_needs_an_async_driver(
    project: Project, url: str, setting: str, driver: str, example: str
) -> None:
    project.models()
    project.ini(sqla_url=url.format(path=project.path / "test.db"), sqla_schema="none")
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
            f"*pytest-sqla: sqla_async_session needs an async driver, but {setting} uses "
            f"{driver}. Set sqla_async_url to the same database with an async driver (for "
            f"example {example}://...)."
        ]
    )


def test_the_message_names_sqla_async_url_when_that_is_the_one_set(project: Project) -> None:
    project.models()
    path = project.path / "test.db"
    project.ini(sqla_url=f"sqlite:///{path}", sqla_async_url=f"sqlite+pysqlite:///{path}")
    project.tests(
        """
        import pytest

        @pytest.mark.asyncio
        async def test_db(sqla_async_connection):
            pass
        """
    )
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*needs an async driver, but sqla_async_url uses pysqlite.*"])


def test_without_a_url_an_async_fixture_fails_like_a_sync_one(project: Project) -> None:
    project.models()
    project.tests(
        """
        import pytest

        @pytest.mark.asyncio
        async def test_db(sqla_async_sessionmaker):
            pass
        """
    )
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*pytest-sqla: sqla_async_sessionmaker needs a database, and*"])


def test_postgresql_psycopg_serves_both_families() -> None:
    """create_async_engine picks psycopg_async for a postgresql+psycopg URL."""
    from sqlalchemy.engine import make_url

    url = make_url("postgresql+psycopg://ada@localhost/app")
    assert not url.get_dialect().is_async
    assert url.get_dialect().get_async_dialect_cls(url).is_async


def test_a_commit_on_a_second_connection_of_the_engine_is_reported(project: Project) -> None:
    project.models()
    project.ini(sqla_url="sqlite+aiosqlite://")
    project.tests(
        """
        import pytest
        from sqlalchemy import text
        from models import acount

        @pytest.mark.asyncio
        async def test_escapes(sqla_async_connection):
            async with sqla_async_connection.engine.begin() as other:
                await other.execute(text("INSERT INTO user_account (email) VALUES ('audit')"))

        @pytest.mark.asyncio
        async def test_later(sqla_async_session):
            assert await acount(sqla_async_session) == 1, "the leak is real"
        """
    )
    result = project.run()
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(
        [
            "E *pytest_sqla.IsolationError: pytest-sqla: test_inner.py::test_escapes committed on "
            "a second connection of the engine behind sqla_async_connection*"
        ]
    )


# ------------------------------------------------- tasks the test leaves running

LEFT_RUNNING = (
    "E *pytest_sqla.IsolationError: pytest-sqla: test_inner.py::test_fire_and_forget left 1 "
    "task(s) running that use its connection: test_fire_and_forget.<locals>.audit. A "
    "connection serves one operation at a time, so instead of rolling the test transaction "
    "back next to them, pytest-sqla closed the connection, and the database rolled the test's "
    "work back. Await or cancel such tasks before the test ends."
)


@pytest.mark.parametrize("marker", ["asyncio", "anyio"])
def test_a_task_left_running_is_reported_and_its_work_rolled_back(
    project: Project, database: Path, marker: str
) -> None:
    """Measured on servers: asyncmy hung the run forever, the others failed oddly."""
    project.conftest(ANYIO_BACKEND)
    project.tests(
        f"""
        import asyncio
        import pytest
        from models import User, acount

        pytestmark = pytest.mark.{marker}

        async def test_fire_and_forget(sqla_async_sessionmaker):
            async def audit():
                async with sqla_async_sessionmaker() as session:
                    session.add(User(email="audit@example.com"))
                    await session.commit()

            asyncio.create_task(audit())  # the code under test never awaits it

        async def test_next(sqla_async_session):
            assert await acount(sqla_async_session) == 0
        """
    )
    result = project.run()
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines([LEFT_RUNNING])
    assert users(database) == []


def test_a_teardown_that_does_not_finish_is_cut_short(project: Project, database: Path) -> None:
    project.ini(sqla_lock_timeout="0.2")
    project.conftest(
        """
        import asyncio
        import pytest
        from sqlalchemy.ext.asyncio import AsyncSession

        class HangingClose(AsyncSession):
            async def close(self):
                await asyncio.sleep(30)

        @pytest.fixture(scope="session")
        def sqla_session_options():
            return {"class_": HangingClose}
        """
    )
    project.tests(
        """
        import pytest
        from sqlalchemy import text
        from models import User

        @pytest.mark.asyncio
        async def test_hangs(sqla_async_session):
            sqla_async_session.add(User(email="ada@example.com"))
            await sqla_async_session.flush()

        @pytest.mark.asyncio
        async def test_next(sqla_async_connection):
            count = text("SELECT count(*) FROM user_account")
            assert await sqla_async_connection.scalar(count) == 0
        """
    )
    result = project.run()
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(
        [
            "E *pytest_sqla.IsolationError: pytest-sqla: rolling back test_inner.py::test_hangs's "
            "transaction took longer than sqla_lock_timeout (0.2 s), so pytest-sqla closed the "
            "connection instead, and the database rolled the test's work back. Usually a task "
            "the test left running uses the connection at the same time; await or cancel such "
            "tasks before the test ends."
        ]
    )
    assert users(database) == []


def test_a_query_cancelled_in_a_test_does_not_lock_the_next_one(project: Project) -> None:
    """Measured: the next test failed at setup with "database is locked" after the 5 s
    busy timeout.

    A subprocess: whether the cancelled statement is collected in time depends on the
    garbage collector, which the outer test session keeps busy.
    """
    project.models()
    project.ini(sqla_url=f"sqlite+aiosqlite:///{project.path / 'test.db'}")
    project.tests(
        """
        import asyncio
        import pytest
        from sqlalchemy import text
        from models import User, acount

        SLOW = (
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 30000000) "
            "SELECT count(*) FROM c"
        )

        @pytest.mark.asyncio
        async def test_times_out(sqla_async_session):
            sqla_async_session.add(User(email="ada@example.com"))
            await sqla_async_session.commit()
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(sqla_async_session.execute(text(SLOW)), 0.2)

        @pytest.mark.asyncio
        async def test_next(sqla_async_session):
            assert await acount(sqla_async_session) == 0
        """
    )
    project.run_subprocess(timeout=60).assert_outcomes(passed=2)
