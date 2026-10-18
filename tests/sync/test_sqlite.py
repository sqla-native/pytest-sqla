"""SQLite: the savepoint leak, the fix, shared in-memory databases, threads."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, event, func
from sqlalchemy import select as sa_select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from pytest_sqla import _sqlite
from tests.helpers import Project, sqlite_rows, sqlite_script

metadata = MetaData()
item = Table("item", metadata, Column("id", Integer, primary_key=True), Column("name", String(20)))


def committed_rows_after_rollback(path: Path, *, fix: bool) -> int:
    """The probe behind the design: a create_savepoint commit, then the outer rollback."""
    engine = create_engine(f"sqlite:///{path}")
    if fix:
        _sqlite.apply_savepoint_fix(engine, {})
    metadata.create_all(engine)
    with engine.connect() as connection:
        outer = connection.begin()
        session = Session(bind=connection, join_transaction_mode="create_savepoint")
        session.execute(item.insert().values(name="committed-by-app"))
        session.commit()
        session.close()
        outer.rollback()
    with engine.connect() as connection:
        left = connection.scalar(sa_select(func.count()).select_from(item))
    engine.dispose()
    return int(left or 0)


@pytest.mark.skipif(
    sys.version_info >= (3, 16), reason="legacy transaction control is no longer the default"
)
def test_without_the_fix_a_committed_row_survives_the_outer_rollback(tmp_path: Path) -> None:
    """The control: what pytest-sqla's own tests would look like without it."""
    assert committed_rows_after_rollback(tmp_path / "control.db", fix=False) == 1


def test_with_the_fix_nothing_survives(tmp_path: Path) -> None:
    assert committed_rows_after_rollback(tmp_path / "fixed.db", fix=True) == 0


@pytest.mark.parametrize(
    ("url", "begin"),
    [("sqlite:///test.db", "begin_immediate"), ("sqlite://", "begin")],
    ids=["file", "memory"],
)
def test_the_plugins_engine_carries_the_fix(project: Project, url: str, begin: str) -> None:
    """A file begins IMMEDIATE, so another process waits instead of failing."""
    project.models()
    project.ini(sqla_url=url)
    project.tests(
        f"""
        from sqlalchemy import event
        from pytest_sqla import _sqlite

        def test_listeners(sqla_connection):
            engine = sqla_connection.engine
            assert event.contains(engine, "connect", _sqlite.disable_implicit_transactions)
            assert event.contains(engine, "begin", _sqlite.{begin})
        """
    )
    project.run().assert_outcomes(passed=1)


PEP_249 = """
import pytest

@pytest.fixture(scope="session")
def sqla_engine_options():
    return {"connect_args": {"autocommit": False, "timeout": 0.2}}
"""
needs_pep_249 = pytest.mark.skipif(
    sys.version_info < (3, 12), reason="sqlite3 autocommit arrived in 3.12"
)


@needs_pep_249
def test_pep_249_mode_needs_no_fix_but_still_takes_the_write_lock(project: Project) -> None:
    """No second BEGIN, but on a file the test transaction still begins IMMEDIATE."""
    project.models()
    project.sqlite()
    project.conftest(PEP_249)
    project.tests(
        """
        from sqlalchemy import event
        from pytest_sqla import _sqlite
        from models import User, count

        def test_commits(sqla_session):
            engine = sqla_session.get_bind().engine
            assert not event.contains(engine, "connect", _sqlite.disable_implicit_transactions)
            assert not event.contains(engine, "begin", _sqlite.begin_immediate)
            assert event.contains(engine, "begin", _sqlite.restart_immediate)
            sqla_session.add(User(email="ada@example.com"))
            sqla_session.commit()
            assert count(sqla_session) == 1

        def test_rolled_back(sqla_session):
            assert count(sqla_session) == 0
        """
    )
    project.run().assert_outcomes(passed=2)


@needs_pep_249
def test_a_raw_commit_in_pep_249_mode_is_reported(project: Project) -> None:
    """Measured before: the root rollback raised "cannot rollback - no transaction is
    active", and that raw OperationalError hid the breach."""
    project.models()
    project.sqlite()
    project.ini(sqla_schema="none")
    sqlite_script(
        project.path / "test.db",
        ["CREATE TABLE user_account (id INTEGER PRIMARY KEY, email VARCHAR(100) UNIQUE)"],
    )
    project.conftest(PEP_249)
    project.tests(
        """
        from sqlalchemy import text
        from models import User

        def test_ends_it(sqla_session):
            sqla_session.add(User(email="leak@example.com"))
            sqla_session.flush()
            sqla_session.execute(text("COMMIT"))
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
    rows = sqlite_rows(project.path / "test.db", "SELECT email FROM user_account")
    assert rows == [("leak@example.com",)]


@needs_pep_249
def test_a_write_lock_that_never_comes_leaves_sqlite3_its_transaction(project: Project) -> None:
    """PEP 249 mode expects a transaction open; without one, the pool's reset failed."""
    project.models()
    project.sqlite()
    project.conftest(PEP_249)
    project.tests(
        """
        import logging
        import pytest
        from sqlalchemy.exc import OperationalError

        def test_a_second_writer(sqla_connection, caplog):
            other = sqla_connection.engine.connect()
            with pytest.raises(OperationalError, match="database is locked"):
                other.begin()
            other.close()
            errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
            assert errors == []
        """
    )
    project.run().assert_outcomes(passed=1)


def test_the_legacy_check_follows_the_runtime_default(monkeypatch: pytest.MonkeyPatch) -> None:
    legacy = getattr(sqlite3, "LEGACY_TRANSACTION_CONTROL", -1)
    assert _sqlite.default_autocommit() == legacy  # Python 3.10 to 3.15
    assert _sqlite.runs_legacy_transaction_control({})
    assert not _sqlite.runs_legacy_transaction_control({"autocommit": False})

    # The day the default flips to PEP 249 mode, the fix switches itself off.
    monkeypatch.setattr(_sqlite, "default_autocommit", lambda: False)
    assert not _sqlite.runs_legacy_transaction_control({})
    assert _sqlite.runs_legacy_transaction_control({"autocommit": legacy})
    engine = create_engine("sqlite://")
    assert not _sqlite.apply_savepoint_fix(engine, {})
    assert not event.contains(engine, "begin", _sqlite.begin)
    engine.dispose()


# ---------------------------------------------------------- in-memory databases


def test_an_in_memory_url_is_one_database_for_every_connection(project: Project) -> None:
    project.models()
    project.ini(sqla_url="sqlite://")
    project.tests(
        """
        from sqlalchemy import inspect
        from models import User, count

        def test_the_schema_connection_created_what_the_test_sees(sqla_connection):
            assert "user_account" in inspect(sqla_connection).get_table_names()

        def test_two_connections_see_one_database(sqla_connection):
            other = sqla_connection.engine.connect()
            try:
                assert "user_account" in inspect(other).get_table_names()
            finally:
                other.close()
        """
    )
    project.run().assert_outcomes(passed=2)


def test_the_keeper_holds_a_shared_memory_database_open(tmp_path: Path) -> None:
    url = _sqlite.shared_memory_url(make_url("sqlite://"), "pytest-sqla-keeper-probe")
    target = _sqlite.keeper_target(url)
    assert target == "file:/pytest-sqla-keeper-probe?vfs=memdb"
    keeper = _sqlite.open_keeper(target)
    engine = create_engine(url)
    with engine.begin() as connection:
        metadata.create_all(connection)
    engine.dispose()  # every connection SQLAlchemy held is gone
    other = sqlite3.connect(target, uri=True)
    try:
        assert other.execute("SELECT name FROM sqlite_master").fetchall() == [("item",)]
    finally:
        other.close()
    keeper.close()
    gone = sqlite3.connect(target, uri=True)
    try:
        assert gone.execute("SELECT name FROM sqlite_master").fetchall() == []
    finally:
        gone.close()


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///file:userdb?mode=memory&cache=shared&uri=true",
        "sqlite:///file:/userdb?vfs=memdb&uri=true",
        "sqlite:///file::memory:?cache=shared&uri=true",
    ],
    ids=["mode=memory", "vfs=memdb", "file::memory:"],
)
def test_a_shared_memory_url_of_your_own_is_used_as_given(project: Project, url: str) -> None:
    """mode=memory left to choose its pool is a SADeprecationWarning on SQLAlchemy 2.1."""
    project.models()
    project.ini(sqla_url=url)
    project.tests(
        """
        import threading
        from models import User, count

        def test_committed_then_read_from_another_thread(sqla_url, sqla_sessionmaker):
            assert sqla_url.database.startswith("file:")
            with sqla_sessionmaker() as session:
                session.add(User(email="ada@example.com"))
                session.commit()
            seen = []
            def read():
                with sqla_sessionmaker() as session:
                    seen.append(count(session))
            thread = threading.Thread(target=read)
            thread.start()
            thread.join()
            assert seen == [1]

        def test_rolled_back(sqla_session):
            assert count(sqla_session) == 0
        """
    )
    project.run().assert_outcomes(passed=2)


def test_the_test_connection_can_be_used_from_another_thread(project: Project) -> None:
    """Starlette's TestClient runs a sync app in its own thread."""
    project.models()
    project.ini(sqla_url="sqlite://")
    project.tests(
        """
        import threading
        from models import User, count

        def test_threaded(sqla_sessionmaker, sqla_session):
            def app():
                with sqla_sessionmaker() as session:
                    session.add(User(email="ada@example.com"))
                    session.commit()
            thread = threading.Thread(target=app)
            thread.start()
            thread.join()
            assert count(sqla_session) == 1
        """
    )
    project.run().assert_outcomes(passed=1)


def test_old_sqlite_cannot_share_an_in_memory_database() -> None:
    with pytest.raises(pytest.fail.Exception) as raised:
        _sqlite.require_shared_memory((3, 35, 5))
    assert str(raised.value) == (
        "pytest-sqla: an in-memory SQLite URL needs SQLite 3.36 or newer so that every "
        "connection sees the same database; this Python uses SQLite 3.35.5. Use a file URL "
        "such as sqlite:///test.db."
    )
    _sqlite.require_shared_memory((3, 36, 0))


# ------------------------------------------------------------------ URL shapes


@pytest.mark.parametrize(
    ("url", "plain_memory", "shared_memory", "plain_file"),
    [
        ("sqlite://", True, False, False),
        ("sqlite:///:memory:", True, False, False),
        ("sqlite+aiosqlite://", True, False, False),
        ("sqlite:///test.db", False, False, True),
        ("sqlite:///file:test.db?uri=true", False, False, False),
        ("sqlite:///file:x?mode=memory&uri=true", False, True, False),
        ("sqlite:///file:/x?vfs=memdb&uri=true", False, True, False),
        ("sqlite:///file::memory:?uri=true", False, True, False),
        ("sqlite:///file:x?mode=memory", False, False, True),
        ("postgresql://db/app", False, False, False),
    ],
)
def test_url_shapes(url: str, plain_memory: bool, shared_memory: bool, plain_file: bool) -> None:
    parsed = make_url(url)
    assert _sqlite.is_plain_memory(parsed) is plain_memory
    assert _sqlite.is_shared_memory(parsed) is shared_memory
    assert _sqlite.is_plain_file(parsed) is plain_file


@pytest.mark.parametrize(
    ("database", "derived"),
    [
        ("test.db", "test-gw3.db"),
        ("data/test.sqlite3", "data/test-gw3.sqlite3"),
        ("/abs/dir.v2/test", "/abs/dir.v2/test-gw3"),
        (".hidden", ".hidden-gw3"),
    ],
)
def test_per_worker_files_sit_next_to_the_configured_one(database: str, derived: str) -> None:
    url = make_url(f"sqlite:///{database}")
    assert _sqlite.per_worker_url(url, "gw3").database == derived


def test_the_keeper_target_matches_what_sqlalchemy_opens() -> None:
    url = make_url("sqlite:///file::memory:?cache=shared&timeout=5&uri=true")
    assert _sqlite.keeper_target(url) == "file::memory:?cache=shared"
    assert _sqlite.keeper_target(make_url("sqlite:///file::memory:?uri=true")) == "file::memory:"
