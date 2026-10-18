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
