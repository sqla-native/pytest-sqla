"""Per-test isolation through the sync fixtures, and the breaches it reports.

Most cases use a file database in ``sqla_schema = none`` mode with tables
created beforehand, so the outer test can count what is left in the file after
the inner run: a breach that leaks must leave a row, and isolation must leave
none.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import Project, sqlite_rows, sqlite_script

TABLES = (
    "CREATE TABLE user_account (id INTEGER PRIMARY KEY, email VARCHAR(100) UNIQUE)",
    "CREATE TABLE note (id INTEGER PRIMARY KEY, user_id INTEGER, body VARCHAR(200))",
)


@pytest.fixture
def database(project: Project) -> Path:
    """A file database with the models' tables, which pytest-sqla does not manage."""
    project.models()
    project.sqlite()
    project.ini(sqla_schema="none")
    path = project.path / "test.db"
    sqlite_script(path, TABLES)
    return path


def users(path: Path) -> list[str]:
    return [str(row[0]) for row in sqlite_rows(path, "SELECT email FROM user_account ORDER BY id")]


def test_a_commit_is_invisible_to_the_next_test_in_either_order(
    project: Project, database: Path
) -> None:
    project.tests(
        """
        from models import User, count

        def test_commits(sqla_session):
            sqla_session.add(User(email="ada@example.com"))
            sqla_session.commit()
            assert count(sqla_session) == 1

        def test_sees_nothing(sqla_session):
            assert count(sqla_session) == 0
        """,
        name="test_one",
    )
    project.tests(
        """
        from models import User, count

        def test_sees_nothing(sqla_session):
            assert count(sqla_session) == 0

        def test_commits(sqla_sessionmaker):
            with sqla_sessionmaker() as session:
                session.add(User(email="ada@example.com"))
                session.commit()
                assert count(session) == 1
        """,
        name="test_two",
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
        from models import User, count

        def test_duplicate(sqla_session, sqla_sessionmaker):
            sqla_session.add(User(email="seed@example.com"))
            sqla_session.commit()

            with sqla_sessionmaker() as app:
                app.add(User(email="seed@example.com"))
                with pytest.raises(IntegrityError):
                    app.commit()
                app.rollback()
                assert count(app) == 1
                app.add(User(email="after@example.com"))
                app.commit()

            assert count(sqla_session) == 2
        """
    )
    project.run().assert_outcomes(passed=1)
    assert users(database) == []


def test_sessions_from_the_sessionmaker_share_the_transaction(
    project: Project, database: Path
) -> None:
    project.tests(
        """
        from sqlalchemy import text
        from models import User, count

        def test_shared(sqla_sessionmaker, sqla_session, sqla_connection):
            with sqla_sessionmaker() as first:
                first.add(User(email="first@example.com"))
                first.commit()
            with sqla_sessionmaker() as second:
                assert count(second) == 1
                second.add(User(email="second@example.com"))
                second.commit()
            assert count(sqla_session) == 2
            assert sqla_connection.scalar(text("SELECT count(*) FROM user_account")) == 2
        """
    )
    project.run().assert_outcomes(passed=1)
    assert users(database) == []


def test_close_leaves_the_connection_open(project: Project, database: Path) -> None:
    project.tests(
        """
        from sqlalchemy import text
        from models import User

        def test_close(sqla_session, sqla_connection):
            sqla_session.add(User(email="ada@example.com"))
            sqla_session.commit()
            sqla_session.close()
            assert not sqla_connection.closed
            assert sqla_connection.scalar(text("SELECT count(*) FROM user_account")) == 1
        """
    )
    project.run().assert_outcomes(passed=1)
    assert users(database) == []


def test_sessions_left_open_and_closed_out_of_order_are_not_a_breach(
    project: Project, database: Path
) -> None:
    project.tests(
        """
        from models import User, count

        def test_untidy(sqla_session, sqla_sessionmaker):
            assert count(sqla_session) == 0          # its savepoint opens first
            first = sqla_sessionmaker()
            first.add(User(email="first@example.com"))
            first.flush()                             # a savepoint above it
            second = sqla_sessionmaker()
            assert count(second) == 1                 # and one above that
            first.close()                             # discards the second's savepoint
            third = sqla_sessionmaker()
            third.add(User(email="third@example.com"))
            third.commit()
            third.add(User(email="left-open@example.com"))
            third.flush()                             # never closed
        """
    )
    project.run().assert_outcomes(passed=1)
    assert users(database) == []


def test_the_guard_also_protects_a_session_with_the_default_join_mode(
    project: Project, database: Path
) -> None:
    """Application code building ``Session(bind=connection)`` itself.

    The default mode, conditional_savepoint, creates a savepoint when the
    connection is already in a nested transaction, and the guard is one. So
    even this session's rollback() stays inside the test transaction.
    """
    project.tests(
        """
        from sqlalchemy.orm import Session
        from models import User, count

        def test_default_mode(sqla_session, sqla_connection):
            sqla_session.add(User(email="seed@example.com"))
            sqla_session.commit()
            app = Session(bind=sqla_connection)
            app.add(User(email="app@example.com"))
            app.rollback()
            assert count(sqla_session) == 1
            app.add(User(email="app@example.com"))
            app.commit()
            app.close()
            assert count(sqla_session) == 2
        """
    )
    project.run().assert_outcomes(passed=1)
    assert users(database) == []


def test_the_derived_url_is_on_the_connection(project: Project) -> None:
    project.models()
    project.ini(sqla_url="sqlite://")
    project.tests(
        """
        def test_url(sqla_url, sqla_connection):
            assert str(sqla_url) == "sqlite://"
            url = sqla_connection.engine.url
            assert url.database.startswith("file:/pytest-sqla-")
            assert dict(url.query) == {"vfs": "memdb", "uri": "true"}
        """
    )
    project.run().assert_outcomes(passed=1)


# ------------------------------------------------------------------- breaches


def test_committing_the_connection_is_reported_on_the_test_that_did_it(
    project: Project, database: Path
) -> None:
    project.tests(
        """
        from models import User

        def test_escapes(sqla_session):
            sqla_session.add(User(email="leak@example.com"))
            sqla_session.flush()
            sqla_session.connection().commit()

        def test_next():
            pass
        """
    )
    result = project.run()
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(
        [
            "*ERROR at teardown of test_escapes*",
            "E *pytest_sqla.IsolationError: pytest-sqla: test_inner.py::test_escapes committed "
            "the test transaction, so its writes are now permanent and visible to later tests. "
            "Something called commit() on the connection behind sqla_session (for example "
            "session.connection().commit() or connection.commit()); commit through a session "
            "instead, which pytest-sqla turns into a savepoint release.",
        ]
    )
    assert users(database) == ["leak@example.com"]


def test_the_message_names_the_first_fixture_that_was_asked_for(
    project: Project, database: Path
) -> None:
    project.tests(
        """
        from sqlalchemy import text

        def test_escapes(sqla_connection, sqla_session):
            sqla_connection.execute(text("INSERT INTO user_account (email) VALUES ('x')"))
            sqla_connection.commit()
        """
    )
    result = project.run()
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*on the connection behind sqla_connection*"])


def test_a_commit_after_rolling_the_connection_back_is_reported(
    project: Project, database: Path
) -> None:
    project.tests(
        """
        from models import User

        def test_escapes(sqla_session, sqla_connection):
            sqla_connection.rollback()
            sqla_session.add(User(email="leak@example.com"))
            sqla_session.commit()
        """
    )
    result = project.run()
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*IsolationError*committed the test transaction*"])
    assert users(database) == ["leak@example.com"]


def test_rolling_the_connection_back_is_not_a_breach(project: Project, database: Path) -> None:
    project.tests(
        """
        from models import User

        def test_rollback(sqla_session, sqla_connection):
            sqla_session.add(User(email="gone@example.com"))
            sqla_session.flush()
            sqla_connection.rollback()
        """
    )
    project.run().assert_outcomes(passed=1)
    assert users(database) == []


@pytest.mark.parametrize(
    "statement",
    [
        'sqla_session.execute(text("COMMIT"))',
        "sqla_connection.connection.dbapi_connection.executescript("
        "\"INSERT INTO note (user_id, body) VALUES (1, 'script');\")",
    ],
    ids=["raw COMMIT", "executescript"],
)
def test_the_database_ending_the_transaction_is_reported(
    project: Project, database: Path, statement: str
) -> None:
    project.tests(
        f"""
        from sqlalchemy import text
        from models import User

        def test_ends_it(sqla_session, sqla_connection):
            sqla_session.add(User(email="leak@example.com"))
            sqla_session.flush()
            {statement}
        """
    )
    result = project.run()
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        [
            "E *pytest_sqla.IsolationError: pytest-sqla: the database ended "
            "test_inner.py::test_ends_it's transaction on its own, so rows the test wrote before "
            "that point are now permanent and visible to later tests. On MySQL and MariaDB, DDL "
            "(CREATE, ALTER, DROP, TRUNCATE) and LOCK TABLES commit implicitly, and on SQLite so "
            "do sqlite3's executescript() and a raw COMMIT; run such statements outside the sqla "
            "fixtures. The driver reported: no such savepoint: sa_savepoint_1",
        ]
    )
    assert users(database) == ["leak@example.com"]


def test_a_failing_test_and_its_breach_are_both_reported(project: Project, database: Path) -> None:
    project.tests(
        """
        from models import User

        def test_both(sqla_session):
            sqla_session.add(User(email="leak@example.com"))
            sqla_session.flush()
            sqla_session.connection().commit()
            assert False, "the test's own failure"
        """
    )
    result = project.run()
    result.assert_outcomes(failed=1, errors=1)
    result.stdout.fnmatch_lines(["*the test's own failure*"])
    result.stdout.fnmatch_lines(["*IsolationError*committed the test transaction*"])


def test_a_lost_connection_is_reported_as_itself(project: Project, database: Path) -> None:
    """A disconnect rolls back on the server: it is an error, but not a breach."""
    project.tests(
        """
        def test_disconnects(sqla_connection):
            sqla_connection.connection.dbapi_connection.close()
        """
    )
    result = project.run()
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        ["E *sqlalchemy.exc.ProgrammingError: (sqlite3.ProgrammingError) Cannot operate on a*"]
    )
    result.stdout.no_fnmatch_line("*pytest_sqla.IsolationError*")


def test_mixing_the_sync_and_async_families_fails(project: Project, database: Path) -> None:
    project.tests(
        """
        def test_mixed(sqla_session, sqla_async_session):
            pass
        """
    )
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            "*pytest-sqla: test_inner.py::test_mixed uses both sync and async sqla fixtures. They "
            "hold separate connections and transactions, so neither would see the other's "
            "writes. Use one family per test."
        ]
    )


def test_a_sync_fixture_refuses_an_async_driver(project: Project) -> None:
    project.models()
    project.ini(sqla_url="postgresql+asyncpg://ada@localhost/app")
    project.tests("def test_db(sqla_session):\n    pass\n")
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            "*pytest-sqla: sqla_session is synchronous, but sqla_url uses the async driver "
            "asyncpg. Use the sqla_async_* fixtures, or set sqla_url to a synchronous driver for "
            "the same database (for example postgresql+psycopg://...) and sqla_async_url to the "
            "async one."
        ]
    )


@pytest.mark.parametrize(
    ("url", "driver", "example"),
    [
        ("sqlite+aiosqlite:///test.db", "aiosqlite", "sqlite"),
        ("mysql+aiomysql://ada@localhost/app", "aiomysql", "mysql+pymysql"),
        ("fakeasync+aiosqlite:///test.db", "aiosqlite", "fakeasync+<driver>"),
    ],
)
def test_a_sync_fixture_suggests_a_sync_driver(
    project: Project, url: str, driver: str, example: str
) -> None:
    project.models()
    project.conftest(
        """
        from sqlalchemy.dialects import registry
        from sqlalchemy.dialects.sqlite.aiosqlite import SQLiteDialect_aiosqlite

        class FakeAsync(SQLiteDialect_aiosqlite):
            name = "fakeasync"
            supports_statement_cache = True

        registry.register("fakeasync.aiosqlite", "conftest", "FakeAsync")
        """
    )
    project.ini(sqla_url=url)
    project.tests("def test_db(sqla_connection):\n    pass\n")
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [f"*uses the async driver {driver}.*(for example {example}://...)*"]
    )
