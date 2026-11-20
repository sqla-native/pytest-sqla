"""SQLite: the savepoint leak, the fix, shared and private in-memory databases, threads."""

from __future__ import annotations

import sqlite3
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    event,
    func,
    inspect,
    text,
)
from sqlalchemy import select as sa_select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from pytest_sqla import _sqlite
from tests.helpers import Project, sqlite_rows, sqlite_script
from tests.sync.test_config import usage_error

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
    assert _sqlite.default_autocommit() == legacy  # Python 3.11 to 3.15
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


# Whether a second connection to one URI sees the table the first created. Measured with
# sqlite3 on Python 3.9.6 (SQLite 3.54.0), 3.11.15 and 3.12.13 (SQLite 3.53.1), and through
# SQLAlchemy 2.0.0, 2.0.58 and 2.1.9: mode=memory, or the name :memory:, is private to its
# connection, vfs=memdb or not, and vfs=memdb alone shares a name beginning with a slash;
# cache=shared shares either, unless the name is empty, which makes a temporary database.
MEMORY_URIS = [
    ("file:{name}?mode=memory", False),
    ("file:{name}?mode=memory&cache=shared", True),
    ("file::memory:", False),
    ("file::memory:?cache=shared", True),
    (":memory:", False),
    ("file:/{name}?vfs=memdb", True),
    ("file:{name}?vfs=memdb", False),
    ("file:{name}?vfs=memdb&cache=shared", True),
    ("file:{name}?mode=memory&vfs=memdb", False),
    ("file:/{name}?mode=memory&vfs=memdb", False),
    ("file:/{name}?mode=memory&vfs=memdb&cache=shared", True),
    ("file:///{name}?vfs=memdb", True),
    ("file://localhost/{name}?vfs=memdb", True),
    ("file:%2F{name}?vfs=memdb", True),
    ("file:\\{name}?vfs=memdb", True),
    ("file:/?vfs=memdb", False),
    ("file:///?vfs=memdb", False),
    ("file:?mode=memory&cache=shared", False),
]


@pytest.mark.parametrize(
    ("form", "shared"), MEMORY_URIS, ids=[form.format(name="NAME") for form, _ in MEMORY_URIS]
)
def test_an_in_memory_uri_is_shared_exactly_when_sqlite_shares_it(form: str, shared: bool) -> None:
    """The control behind the refusal: a private form gives a second connection an empty
    database, so a schema built on one connection is in none of the others."""
    table = f"t_{uuid.uuid4().hex}"
    target = form.format(name=table)
    url = make_url(f"sqlite:///{target}{'&' if '?' in target else '?'}uri=true")
    assert _sqlite.is_memory_uri(url)
    assert (_sqlite.is_shared_memory(url), _sqlite.is_private_memory(url)) == (shared, not shared)
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.connect() as first:
            first.execute(text(f"CREATE TABLE {table} (id INTEGER)"))
            first.commit()
            with engine.connect() as second:
                seen = table in inspect(second).get_table_names()
    finally:
        engine.dispose()
    assert seen is shared


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///file:userdb?mode=memory&cache=shared&uri=true",
        "sqlite:///file:/userdb?vfs=memdb&uri=true",
        "sqlite:///file::memory:?cache=shared&uri=true",
        "sqlite:///file:userdb?vfs=memdb&cache=shared&uri=true",
        "sqlite:///file:/userdb?mode=memory&vfs=memdb&cache=shared&uri=true",
        "sqlite:///file:///userdb?vfs=memdb&uri=true",
    ],
    ids=[
        "mode=memory&cache=shared",
        "/NAME?vfs=memdb",
        "file::memory:?cache=shared",
        "NAME?vfs=memdb&cache=shared",
        "mode=memory&vfs=memdb&cache=shared",
        "file:///NAME?vfs=memdb",
    ],
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


# -------------------------------------------------- private in-memory databases


PRIVATE_URL = "sqlite:///file:app?mode=memory&uri=true"
PRIVATE_ASYNC_URL = "sqlite+aiosqlite:///file:app?mode=memory&uri=true"
PRIVATE_URLS = [
    PRIVATE_URL,
    "sqlite:///file::memory:?uri=true",
    "sqlite:///file:app?vfs=memdb&uri=true",
    "sqlite:///file:/app?mode=memory&vfs=memdb&uri=true",
    "sqlite:///:memory:?uri=true",
]
PRIVATE_IDS = [
    "NAME?mode=memory",
    "file::memory:",
    "NAME?vfs=memdb",
    "/NAME?mode=memory&vfs=memdb",
    ":memory:?uri=true",
]


def refusal(url: str) -> str:
    """The error for a private in-memory URL, with the URL rendered as SQLAlchemy renders it:
    2.1 percent-encodes the colon of file:, 2.0 does not."""
    return (
        "pytest-sqla: sqla_schema = create creates the tables on one connection and runs every "
        f"test on another, but each connection to {make_url(url).render_as_string()} gets its "
        "own empty database. Use sqlite://, which pytest-sqla shares as one memdb database for "
        "the session, or a URI that SQLite shares between connections: file:/NAME?vfs=memdb "
        "without mode=memory, or one with cache=shared."
    )


@pytest.mark.parametrize("url", PRIVATE_URLS, ids=PRIVATE_IDS)
def test_create_mode_refuses_a_private_in_memory_url(project: Project, url: str) -> None:
    """Measured before: the schema went into a database no test connection opened, and every
    test that touched a table failed with "no such table"."""
    result = usage_error(project, "--sqla-url", url)
    result.stderr.fnmatch_lines([f"*{refusal(url)}"])


@pytest.mark.parametrize(
    ("place", "name", "url"),
    [
        ("variable", "PYTEST_SQLA_URL", PRIVATE_URL),
        ("ini", "sqla_url", PRIVATE_URL),
        ("option", "--sqla-async-url", PRIVATE_ASYNC_URL),
        ("variable", "PYTEST_SQLA_ASYNC_URL", PRIVATE_ASYNC_URL),
        ("ini", "sqla_async_url", PRIVATE_ASYNC_URL),
    ],
    ids=[
        "PYTEST_SQLA_URL",
        "sqla_url",
        "--sqla-async-url",
        "PYTEST_SQLA_ASYNC_URL",
        "sqla_async_url",
    ],
)
def test_the_refusal_comes_before_the_run_from_every_source(
    project: Project, monkeypatch: pytest.MonkeyPatch, place: str, name: str, url: str
) -> None:
    if place == "variable":
        monkeypatch.setenv(name, url)
    if place == "ini":
        project.ini(**{name: url})
    result = usage_error(project, *((name, url) if place == "option" else ()))
    result.stderr.fnmatch_lines([f"*{refusal(url)}"])


@pytest.mark.parametrize(
    ("fixture", "url", "test"),
    [
        (
            "sqla_url",
            PRIVATE_URL,
            "def test_db(sqla_session):\n    assert count(sqla_session) == 0\n",
        ),
        (
            "sqla_async_url",
            PRIVATE_ASYNC_URL,
            "def test_db(sqla_async_database_url):\n    pass\n",
        ),
    ],
    ids=["sqla_url", "sqla_async_url"],
)
def test_an_overridden_private_in_memory_url_fails_the_tests_that_ask_for_it(
    project: Project, fixture: str, url: str, test: str
) -> None:
    """An override, such as a container's URL, is known only once a test asks for it."""
    project.models()
    project.conftest(
        f"""
        import pytest

        @pytest.fixture(scope="session")
        def {fixture}():
            return {url!r}
        """
    )
    project.tests(f"from models import count\n\n{test}\n\ndef test_unrelated():\n    pass\n")
    result = project.run()
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines([f"*{refusal(url)}"])


@pytest.mark.parametrize("url", PRIVATE_URLS, ids=PRIVATE_IDS)
def test_in_none_mode_each_test_connection_has_a_private_database_of_its_own(
    project: Project, url: str
) -> None:
    """The tests own the schema there, and a test can build it on its own connection. No
    keeper: it would hold one more empty database that nothing else opens."""
    project.ini(sqla_url=url, sqla_schema="none")
    project.tests(
        """
        import threading
        from sqlalchemy import event, inspect, text
        from pytest_sqla import _sqlite

        def test_builds_its_own_table(sqla_connection, _sqla_sqlite_keeper):
            assert _sqla_sqlite_keeper is None
            assert event.contains(sqla_connection.engine, "begin", _sqlite.begin)
            sqla_connection.execute(text("CREATE TABLE note (body TEXT)"))
            sqla_connection.execute(text("INSERT INTO note VALUES ('mine')"))
            other = sqla_connection.engine.connect()
            try:
                assert inspect(other).get_table_names() == []
            finally:
                other.close()
            seen = []
            def read():  # as Starlette's TestClient would, from a thread of its own
                seen.append(sqla_connection.scalar(text("SELECT body FROM note")))
            thread = threading.Thread(target=read)
            thread.start()
            thread.join()
            assert seen == ["mine"]

        def test_the_next_test_starts_empty(sqla_connection):
            assert inspect(sqla_connection).get_table_names() == []
        """
    )
    project.run().assert_outcomes(passed=2)


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
    ("url", "plain_memory", "shared_memory", "private_memory", "plain_file"),
    [
        ("sqlite://", True, False, False, False),
        ("sqlite:///:memory:", True, False, False, False),
        ("sqlite+aiosqlite://", True, False, False, False),
        ("sqlite:///test.db", False, False, False, True),
        ("sqlite:///file:test.db?uri=true", False, False, False, False),
        ("sqlite:///file:test.db?cache=shared&uri=true", False, False, False, False),
        # Files, measured: ":memory:x", because only the name :memory: itself is in memory,
        # and ":memory:?mode=memory", because sqlite3 reads a name without file: as a plain
        # filename, with SQLAlchemy's parameters appended.
        ("sqlite:///file::memory:x?uri=true", False, False, False, False),
        ("sqlite:///:memory:?mode=memory&uri=true", False, False, False, False),
        ("sqlite:///file:x?mode=memory&cache=shared&uri=true", False, True, False, False),
        ("sqlite:///file:x?mode=memory&uri=true", False, False, True, False),
        ("sqlite:///file:/x?vfs=memdb&uri=true", False, True, False, False),
        ("sqlite:///file::memory:?uri=true", False, False, True, False),
        ("sqlite:///file:x?mode=memory", False, False, False, True),
        ("postgresql://db/app", False, False, False, False),
    ],
)
def test_url_shapes(
    url: str, plain_memory: bool, shared_memory: bool, private_memory: bool, plain_file: bool
) -> None:
    parsed = make_url(url)
    assert _sqlite.is_plain_memory(parsed) is plain_memory
    assert _sqlite.is_shared_memory(parsed) is shared_memory
    assert _sqlite.is_private_memory(parsed) is private_memory
    assert _sqlite.is_memory_uri(parsed) is (shared_memory or private_memory)
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
