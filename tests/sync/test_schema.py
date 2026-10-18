"""The schema: created once, owned, dropped only when pytest-sqla created it."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from tests.helpers import MODELS, Project, sqlite_rows, sqlite_script, table_names

MARKER = (
    "CREATE TABLE pytest_sqla (run_id VARCHAR(64) PRIMARY KEY, workers INTEGER NOT NULL, "
    "started_at VARCHAR(32) NOT NULL, owner VARCHAR(255) NOT NULL)"
)
TABLES = (
    "CREATE TABLE user_account (id INTEGER PRIMARY KEY, email VARCHAR(100) UNIQUE)",
    "CREATE TABLE note (id INTEGER PRIMARY KEY, user_id INTEGER, body VARCHAR(200))",
)
SEES_THE_SCHEMA = """
from sqlalchemy import inspect

def test_schema(sqla_connection):
    assert sorted(inspect(sqla_connection).get_table_names()) == [
        "note", "pytest_sqla", "user_account"
    ]
"""


@pytest.fixture
def database(project: Project) -> str:
    project.models()
    return project.sqlite()


def test_tables_are_created_before_the_first_test_and_dropped_after_the_last(
    project: Project, database: str
) -> None:
    project.tests(SEES_THE_SCHEMA)
    project.run().assert_outcomes(passed=1)
    assert table_names(database) == []


def test_none_mode_runs_no_ddl(project: Project, database: str) -> None:
    path = project.path / "test.db"
    sqlite_script(path, [*TABLES, "INSERT INTO user_account (email) VALUES ('kept')"])
    project.ini(sqla_schema="none")
    project.tests(
        """
        from sqlalchemy import inspect

        def test_schema(sqla_connection):
            assert sorted(inspect(sqla_connection).get_table_names()) == ["note", "user_account"]
        """
    )
    project.run().assert_outcomes(passed=1)
    assert table_names(database) == ["note", "user_account"]
    assert sqlite_rows(path, "SELECT email FROM user_account") == [("kept",)]


def test_tables_it_did_not_create_are_refused_and_left_alone(
    project: Project, database: str
) -> None:
    path = project.path / "test.db"
    sqlite_script(path, [*TABLES, "INSERT INTO user_account (email) VALUES ('precious')"])
    project.tests(
        """
        def test_one(sqla_session):
            pass

        def test_two(sqla_connection):
            pass

        def test_unrelated():
            pass
        """
    )
    result = project.run()
    result.assert_outcomes(passed=1, errors=2)
    result.stdout.fnmatch_lines(
        [
            f"E *pytest_sqla.SchemaConflictError: pytest-sqla: the database at {database} "
            "already has tables that pytest-sqla did not create: note, user_account. It will not "
            "drop or replace them. Point sqla_url at a database used only for tests, drop those "
            "tables yourself, or set sqla_schema = none to run against them as they are.",
        ]
    )
    assert table_names(database) == ["note", "user_account"]
    assert sqlite_rows(path, "SELECT email FROM user_account") == [("precious",)]


def leftover(path: Path, run_id: str = "an-old-run", workers: int = 2) -> None:
    sqlite_script(
        path,
        [
            MARKER,
            *TABLES,
            "INSERT INTO pytest_sqla VALUES "
            f"('{run_id}', {workers}, '2026-10-18T09:00:00+00:00', 'ci-host:4242/gw1')",
        ],
    )


def test_another_runs_schema_is_refused(project: Project, database: str) -> None:
    leftover(project.path / "test.db")
    project.tests(SEES_THE_SCHEMA)
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            f"E *pytest_sqla.SchemaConflictError: pytest-sqla: the database at {database} holds "
            "the schema of another pytest-sqla run (started 2026-10-18T09:00:00+00:00 by "
            "ci-host:4242/gw1; 2 worker(s) still running, or crashed). If no other test run "
            "uses this database, run again with --sqla-recreate-schema; otherwise give this run "
            "a database of its own.",
        ]
    )
    assert table_names(database) == ["note", "pytest_sqla", "user_account"]


def test_recreate_schema_replaces_a_crashed_runs_leftovers(project: Project, database: str) -> None:
    path = project.path / "test.db"
    leftover(path)
    sqlite_script(path, ["INSERT INTO user_account (email) VALUES ('stale')"])
    project.tests(
        """
        from models import count

        def test_fresh(sqla_session):
            assert count(sqla_session) == 0
        """
    )
    project.run("--sqla-recreate-schema").assert_outcomes(passed=1)
    assert table_names(database) == []


def test_recreate_schema_on_a_clean_database_just_creates_it(
    project: Project, database: str
) -> None:
    project.tests(SEES_THE_SCHEMA)
    project.run("--sqla-recreate-schema").assert_outcomes(passed=1)
    assert table_names(database) == []


def test_an_empty_marker_table_is_still_another_runs(project: Project, database: str) -> None:
    sqlite_script(project.path / "test.db", [MARKER])
    project.tests(SEES_THE_SCHEMA)
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        ["*(started at an unknown time by an unknown run; 0 worker(s) still running, or crashed)*"]
    )


def test_a_sibling_worker_joins_and_the_last_one_out_drops(
    project: Project, database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without xdist: the marker row a sibling worker of this run would have written."""
    path = project.path / "test.db"
    leftover(path, run_id="this-run", workers=1)
    monkeypatch.setenv("PYTEST_XDIST_TESTRUNUID", "this-run")
    project.tests(
        """
        from sqlalchemy import text

        def test_joined(sqla_connection):
            assert sqla_connection.scalar(text("SELECT workers FROM pytest_sqla")) == 2
        """
    )
    project.run().assert_outcomes(passed=1)
    # This process left; the sibling still counts, so nothing was dropped.
    assert sqlite_rows(path, "SELECT run_id, workers FROM pytest_sqla") == [("this-run", 1)]
    assert table_names(database) == ["note", "pytest_sqla", "user_account"]


def test_tables_are_kept_when_another_run_took_the_schema_over(
    project: Project, database: str
) -> None:
    path = project.path / "test.db"
    project.tests(
        f"""
        import sqlite3

        def test_taken_over(sqla_schema):
            other = sqlite3.connect({str(path)!r})
            other.execute("DELETE FROM pytest_sqla")
            other.execute(
                "INSERT INTO pytest_sqla VALUES ('newer-run', 1, '2026-10-18T10:00:00+00:00', "
                "'elsewhere:1/main')"
            )
            other.commit()
            other.close()
        """
    )
    project.run().assert_outcomes(passed=1)
    assert sqlite_rows(path, "SELECT run_id FROM pytest_sqla") == [("newer-run",)]
    assert table_names(database) == ["note", "pytest_sqla", "user_account"]


def test_a_dropped_marker_fails_the_teardown_and_drops_nothing(
    project: Project, database: str
) -> None:
    """Without its marker pytest-sqla cannot tell whose tables these are."""
    path = project.path / "test.db"
    project.tests(
        f"""
        import sqlite3

        def test_drops_the_marker(sqla_schema):
            other = sqlite3.connect({str(path)!r})
            other.execute("DROP TABLE pytest_sqla")
            other.commit()
            other.close()
        """
    )
    result = project.run()
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*OperationalError*no such table: pytest_sqla*"])
    assert table_names(database) == ["note", "user_account"]


def test_a_metadata_table_named_like_the_marker_is_refused(project: Project) -> None:
    project.models(
        """
        from sqlalchemy import Column, Integer, MetaData, Table

        metadata = MetaData()
        Table("pytest_sqla", metadata, Column("id", Integer, primary_key=True))
        """,
        name="clash",
    )
    project.ini(sqla_metadata="clash:metadata")
    project.sqlite()
    project.tests(SEES_THE_SCHEMA)
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            "E *pytest_sqla.SchemaConflictError: pytest-sqla: your metadata defines a table "
            "named pytest_sqla, which pytest-sqla uses to track the schema it creates. Rename "
            "that table."
        ]
    )


def test_a_failed_creation_leaves_nothing_behind(project: Project) -> None:
    project.models(
        MODELS
        + """
from sqlalchemy import CheckConstraint


class Broken(Base):
    __tablename__ = "zz_broken"
    __table_args__ = (CheckConstraint("this is not ( sql"),)
    id: Mapped[int] = mapped_column(primary_key=True)
"""
    )
    database = project.sqlite()
    project.tests(SEES_THE_SCHEMA)
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*OperationalError*"])
    assert table_names(database) == []


# ------------------------------------------------------------------- metadata

APP_MODELS = """
from types import SimpleNamespace

from sqlalchemy import Column, Integer, MetaData, Table
from sqlalchemy.orm import DeclarativeBase, declarative_base


class Base(DeclarativeBase):
    pass


class Thing(Base):
    __tablename__ = "thing"
    id = Column(Integer, primary_key=True)


Legacy = declarative_base()


class Old(Legacy):
    __tablename__ = "old"
    id = Column(Integer, primary_key=True)


metadata = MetaData()
Table("plain", metadata, Column("id", Integer, primary_key=True))

namespace = SimpleNamespace(models=SimpleNamespace(Base=Base))
BOTH = [Base, Legacy]
DUPLICATES = (Base, Base.metadata)
NUMBER = 42
MIXED = [Base, "Legacy"]
A_TABLE = metadata.tables["plain"]
EMPTY = []
"""

TABLES_SEEN = """
from sqlalchemy import inspect

def test_tables(sqla_connection):
    print()
    print("tables:", sorted(inspect(sqla_connection).get_table_names()))
"""


@pytest.mark.parametrize(
    ("attribute", "tables"),
    [
        ("Base", "['pytest_sqla', 'thing']"),
        ("metadata", "['plain', 'pytest_sqla']"),
        ("Legacy", "['old', 'pytest_sqla']"),
        ("BOTH", "['old', 'pytest_sqla', 'thing']"),
        ("DUPLICATES", "['pytest_sqla', 'thing']"),
        ("namespace.models.Base", "['pytest_sqla', 'thing']"),
    ],
)
def test_every_metadata_form_is_accepted(project: Project, attribute: str, tables: str) -> None:
    project.models(APP_MODELS, name="app_models")
    project.ini(sqla_metadata=f"app_models:{attribute}")
    project.sqlite()
    project.tests(TABLES_SEEN)
    result = project.run("-s")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines([f"tables: {tables}"])


def test_the_metadata_can_come_from_a_fixture(project: Project) -> None:
    project.pytester.makepyfile(app_models=APP_MODELS)
    project.sqlite()
    project.conftest(
        """
        import pytest
        from app_models import Base, metadata

        @pytest.fixture(scope="session")
        def sqla_metadata():
            return [Base, metadata]
        """
    )
    project.tests(TABLES_SEEN)
    result = project.run("-s")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["tables: ['plain', 'pytest_sqla', 'thing']"])


@pytest.mark.parametrize(
    ("path", "message"),
    [
        (
            "missing_module:Base",
            "*pytest-sqla: cannot import sqla_metadata = 'missing_module:Base': "
            "ModuleNotFoundError: No module named 'missing_module'",
        ),
        (
            "app_models:Nope",
            "*pytest-sqla: cannot import sqla_metadata = 'app_models:Nope': AttributeError: *",
        ),
        (
            "app_models:NUMBER",
            "*pytest-sqla: sqla_metadata = 'app_models:NUMBER' is a int, not a MetaData, a "
            "declarative base, or a list of them.",
        ),
        ("app_models:MIXED", "*sqla_metadata = 'app_models:MIXED' is a str, not a MetaData*"),
        ("app_models:A_TABLE", "*sqla_metadata = 'app_models:A_TABLE' is a Table, not a*"),
    ],
    ids=["no module", "no attribute", "an int", "a list with a str", "a Table"],
)
def test_metadata_that_cannot_be_used_fails_the_database_tests(
    project: Project, path: str, message: str
) -> None:
    project.pytester.makepyfile(app_models=APP_MODELS)
    project.ini(sqla_metadata=path)
    project.sqlite()
    project.tests("def test_db(sqla_session):\n    pass\n\ndef test_unrelated():\n    pass\n")
    result = project.run()
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines([message])


def test_a_fixture_value_that_is_not_metadata_names_the_fixture(project: Project) -> None:
    project.sqlite()
    project.conftest(
        """
        import pytest

        @pytest.fixture(scope="session")
        def sqla_metadata():
            return "app.models:Base"
        """
    )
    project.tests("def test_db(sqla_session):\n    pass\n")
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            "*pytest-sqla: the value of the sqla_metadata fixture is a str, not a MetaData, a "
            "declarative base, or a list of them."
        ]
    )


NEEDS_METADATA = (
    "*pytest-sqla: sqla_schema = create needs your metadata. Set sqla_metadata = "
    "'app.models:Base' (a MetaData, a declarative base, or a list of them), or override the "
    "sqla_metadata fixture. If the database already has its schema, set sqla_schema = none."
)


def test_create_mode_needs_metadata(project: Project) -> None:
    project.sqlite()
    project.tests("def test_db(sqla_session):\n    pass\n")
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines([NEEDS_METADATA])


def test_an_empty_list_is_no_metadata(project: Project) -> None:
    project.models(APP_MODELS, name="app_models")
    project.ini(sqla_metadata="app_models:EMPTY")
    project.sqlite()
    project.tests("def test_db(sqla_session):\n    pass\n")
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines([NEEDS_METADATA])


def test_an_overridden_schema_fixture_owns_the_schema(project: Project, database: str) -> None:
    project.conftest(
        f"""
        import pytest
        from sqlalchemy import create_engine
        from models import Base

        @pytest.fixture(scope="session")
        def sqla_schema():
            engine = create_engine({database!r})
            Base.metadata.create_all(engine)
            yield
            Base.metadata.drop_all(engine)
            engine.dispose()
        """
    )
    project.tests(
        """
        from sqlalchemy import inspect

        def test_schema(sqla_connection):
            assert sorted(inspect(sqla_connection).get_table_names()) == ["note", "user_account"]
        """
    )
    project.run().assert_outcomes(passed=1)
    assert table_names(database) == []


# ---------------------------------------------------------------------- locks


def test_waiting_too_long_for_the_schema_lock_fails(project: Project, database: str) -> None:
    holder = sqlite3.connect(project.path / "test.db", isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        project.ini(sqla_lock_timeout="0.2")
        project.tests(SEES_THE_SCHEMA)
        result = project.run()
    finally:
        holder.rollback()
        holder.close()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            f"E *pytest_sqla.SchemaConflictError: pytest-sqla: gave up after 0.2 s waiting for "
            f"the schema lock on {database}; another pytest-sqla process is creating or dropping "
            "the schema. A crashed run releases the lock when its connection closes. The "
            "driver reported: database is locked",
        ]
    )


def test_a_leaked_transaction_cannot_hang_the_drop(project: Project, database: str) -> None:
    path = project.path / "test.db"
    project.ini(sqla_lock_timeout="0.2")
    project.conftest(
        """
        LEAKED = []

        def pytest_sessionfinish(session):
            for connection in LEAKED:
                connection.close()
        """
    )
    project.tests(
        f"""
        import sqlite3
        from conftest import LEAKED

        def test_leaks(sqla_schema):
            leaked = sqlite3.connect({str(path)!r}, isolation_level=None)
            leaked.execute("BEGIN IMMEDIATE")
            LEAKED.append(leaked)
        """
    )
    result = project.run()
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        [
            "*ERROR at teardown of test_leaks*",
            f"E *pytest_sqla.SchemaConflictError: pytest-sqla: could not drop the test schema "
            f"in {database} within 0.2 s: another connection still holds a lock on one of its "
            "tables. Some code in this run opened its own connection (for example through the "
            "application's own engine) and left a transaction open. On SQLite, so can a query "
            "result a failing test left unread: Python 3.11+ keeps its read lock until the "
            "result is garbage-collected. The driver reported: database is locked",
        ]
    )


UNREAD_RESULT = """
from models import User

def test_fails_with_a_result_unread(sqla_session):
    sqla_session.add_all([User(email=f"{n}@example.com") for n in range(3)])
    sqla_session.flush()
    result = sqla_session.execute(User.__table__.select())
    assert result is None, "a failure while the rows are still unread"
"""


def test_an_unread_result_cannot_block_a_disposable_database(project: Project) -> None:
    """In-memory and per-worker databases are discarded, never dropped."""
    project.models()
    project.ini(sqla_url="sqlite://", sqla_lock_timeout="0.5")
    project.tests(UNREAD_RESULT)
    result = project.run()
    result.assert_outcomes(failed=1)


@pytest.mark.skipif(sys.version_info < (3, 11), reason="3.10's rollback resets the statement")
def test_an_unread_result_holding_a_named_file_is_explained(
    project: Project, database: str
) -> None:
    """Measured: Python 3.11+ keeps the read lock until the result is collected."""
    project.ini(sqla_lock_timeout="0.5")
    project.tests(UNREAD_RESULT)
    result = project.run()
    result.assert_outcomes(failed=1, errors=1)
    result.stdout.fnmatch_lines(["*could not drop the test schema*left unread*"])


# --------------------------------------------------------- xdist and dialects


def test_an_xdist_worker_gets_its_own_sqlite_file_and_removes_it(
    project: Project, database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw7")
    monkeypatch.setenv("PYTEST_XDIST_TESTRUNUID", "a-run")
    project.tests(
        """
        def test_own_file(sqla_url, sqla_connection):
            assert sqla_url.database.endswith("test.db")
            assert sqla_connection.engine.url.database.endswith("test-gw7.db")
        """
    )
    project.run().assert_outcomes(passed=1)
    assert sorted(path.name for path in project.path.glob("*.db")) == []


def test_in_none_mode_the_workers_share_the_configured_file(
    project: Project, database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sqlite_script(project.path / "test.db", TABLES)
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw7")
    project.ini(sqla_schema="none")
    project.tests(
        """
        def test_shared_file(sqla_connection):
            assert sqla_connection.engine.url.database.endswith("test.db")
        """
    )
    project.run().assert_outcomes(passed=1)


FAKE_DIALECT = """
from sqlalchemy.dialects import registry
from sqlalchemy.dialects.sqlite.pysqlite import SQLiteDialect_pysqlite


class FakeDialect(SQLiteDialect_pysqlite):
    name = "fakedb"
    supports_statement_cache = True


registry.register("fakedb", "conftest", "FakeDialect")
"""


def test_another_dialect_works_in_a_single_process(project: Project) -> None:
    project.models()
    project.conftest(FAKE_DIALECT)
    url = f"fakedb:///{project.path / 'fake.db'}"
    project.ini(sqla_url=url)
    project.tests(
        """
        from sqlalchemy import inspect

        def test_schema(sqla_connection):
            assert sqla_connection.dialect.name == "fakedb"
            assert "user_account" in inspect(sqla_connection).get_table_names()
        """
    )
    project.run().assert_outcomes(passed=1)
    assert table_names(f"sqlite:///{project.path / 'fake.db'}") == []


def test_another_dialect_cannot_be_serialised_across_xdist_workers(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
    project.models()
    project.conftest(FAKE_DIALECT)
    project.ini(sqla_url=f"fakedb:///{project.path / 'fake.db'}")
    project.tests(SEES_THE_SCHEMA)
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            "*pytest-sqla: cannot serialise schema creation across pytest-xdist workers on "
            "fakedb; supported are PostgreSQL, MySQL, MariaDB and SQLite. Create the schema "
            "before the run and set sqla_schema = none, or run without -n."
        ]
    )
