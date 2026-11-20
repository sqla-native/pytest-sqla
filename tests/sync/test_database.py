"""sqla_database: the setting, its checks, the report header, SQLite, the URL fixtures.

Everything here runs without a server: pytest-sqla checks a database of its own
before it connects, and in configured mode and on SQLite it creates nothing.
Creating, sharing and dropping real databases is in
``tests/integration/test_databases.py``.
"""

from __future__ import annotations

import hashlib

import pytest

from tests.helpers import Project, table_names
from tests.sync.test_config import usage_error

RUN_ID = "0123456789abcdef0123456789abcdef"
SERVER = "postgresql+psycopg://ada:s3cret@db.example/app"


@pytest.fixture
def run_id(monkeypatch: pytest.MonkeyPatch) -> str:
    """A fixed run id, so the derived names are known: the one xdist would share."""
    monkeypatch.setenv("PYTEST_XDIST_TESTRUNUID", RUN_ID)
    return RUN_ID


# ------------------------------------------------------------- the setting


@pytest.mark.parametrize("value", ["Run", "per-worker", ""])
def test_sqla_database_is_configured_run_or_worker(project: Project, value: str) -> None:
    project.ini(sqla_database=value)
    result = usage_error(project)
    result.stderr.fnmatch_lines(
        [f"*pytest-sqla: sqla_database must be 'configured', 'run' or 'worker', got {value!r}."]
    )


def test_keep_database_needs_a_database_to_keep(project: Project) -> None:
    result = usage_error(project, "--sqla-keep-database", "--sqla-url", SERVER)
    result.stderr.fnmatch_lines(
        [
            "*pytest-sqla: --sqla-keep-database keeps the databases sqla_database = run and "
            "worker create, and sqla_database = configured creates none."
        ]
    )


def test_another_dialect_cannot_have_a_database_of_its_own(project: Project) -> None:
    project.ini(sqla_database="run")
    result = usage_error(project, "--sqla-url", "oracle://ada:s3cret@db.example/app")
    result.stderr.fnmatch_lines(
        [
            "*pytest-sqla: sqla_database = run creates databases on PostgreSQL, MySQL and "
            "MariaDB, and has no effect on SQLite; oracle://ada:***@db.example/app is oracle. "
            "Set sqla_database = configured and create the database before the run."
        ]
    )
    assert "s3cret" not in result.stderr.str()


def test_a_url_must_name_the_database_to_derive_from(project: Project) -> None:
    project.ini(sqla_database="worker")
    result = usage_error(project, "--sqla-url", "postgresql+psycopg://ada:s3cret@db.example")
    result.stderr.fnmatch_lines(
        [
            "*pytest-sqla: sqla_database = worker names its database after the one in the URL, "
            "but postgresql+psycopg://ada:***@db.example names none. Add the database "
            "pytest-sqla should connect to, for example /postgres on PostgreSQL."
        ]
    )
    assert "s3cret" not in result.stderr.str()


@pytest.mark.parametrize(
    ("url", "name", "length", "server", "limit"),
    [
        ("postgresql+psycopg://ada@db/" + "a" * 55, "a" * 55, "64 bytes", "PostgreSQL", 63),
        ("postgresql+psycopg://ada@db/" + "é" * 28, "é" * 28, "65 bytes", "PostgreSQL", 63),
        ("mysql+pymysql://ada@db/" + "a" * 56, "a" * 56, "65 characters", "MySQL", 64),
        ("mariadb+pymysql://ada@db/" + "é" * 56, "é" * 56, "65 characters", "MariaDB", 64),
    ],
    ids=["PostgreSQL", "PostgreSQL, two bytes a letter", "MySQL", "MariaDB"],
)
def test_a_name_the_server_would_not_take_whole_is_refused(
    project: Project, run_id: str, url: str, name: str, length: str, server: str, limit: int
) -> None:
    """PostgreSQL counts bytes and silently shortens a longer name; MySQL counts characters."""
    project.ini(sqla_database="run")
    result = usage_error(project, "--sqla-url", url)
    result.stderr.fnmatch_lines(
        [
            f"*pytest-sqla: sqla_database = run would name the database {name}_01234567, which "
            f"is {length} long, but {server} takes at most {limit}. Shorten the database name in "
            "the URL."
        ]
    )


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://ada@db/" + "a" * 54,
        "postgresql+psycopg://ada@db/" + "é" * 27,
        "mysql+pymysql://ada@db/" + "é" * 55,
    ],
    ids=["63 bytes", "63 bytes of two-byte letters", "64 characters, 119 bytes"],
)
def test_a_name_at_the_limit_is_fine(project: Project, run_id: str, url: str) -> None:
    project.ini(sqla_database="run")
    project.tests("def test_nothing():\n    pass\n")
    project.run("--sqla-url", url).assert_outcomes(passed=1)


def test_a_worker_checks_the_name_with_its_own_id(
    project: Project, run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run's name would fit; gw12's does not."""
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw12")
    project.ini(sqla_database="worker")
    result = usage_error(project, "--sqla-url", "postgresql+psycopg://ada@db/" + "a" * 51)
    result.stderr.fnmatch_lines(["*_01234567_gw12, which is 65 bytes long*"])


# ----------------------------------------------------------------- the header


@pytest.mark.parametrize(
    ("mode", "url", "worker", "database"),
    [
        ("configured", SERVER, None, "configured"),
        ("run", SERVER, None, "run (app_01234567)"),
        ("worker", SERVER, None, "worker (app_01234567)"),
        ("run", SERVER, "gw3", "run (app_01234567)"),
        ("worker", "mysql+pymysql://ada:s3cret@db.example/app", "gw3", "worker (app_01234567_gw3)"),
        ("run", "sqlite:///test.db", None, "run (no effect on SQLite)"),
        ("worker", "sqlite://", None, "worker (no effect on SQLite)"),
    ],
    ids=[
        "configured",
        "run",
        "worker without xdist",
        "run in an xdist worker",
        "worker in an xdist worker",
        "run on SQLite",
        "worker on SQLite",
    ],
)
def test_the_header_names_the_mode_and_the_database(
    project: Project,
    run_id: str,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    url: str,
    worker: str | None,
    database: str,
) -> None:
    if worker is not None:
        monkeypatch.setenv("PYTEST_XDIST_WORKER", worker)
    project.ini(sqla_database=mode)
    project.tests("def test_nothing():\n    pass\n")
    result = project.run("--sqla-url", url)
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines([f"sqla: url=*, schema=create, database={database}"])


def test_a_run_id_that_is_not_hex_still_names_the_database_in_hex(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """xdist's run id is 32 hex digits; one set by hand need not be."""
    monkeypatch.setenv("PYTEST_XDIST_TESTRUNUID", "nightly-build")
    project.ini(sqla_database="run")
    project.tests("def test_nothing():\n    pass\n")
    result = project.run("--sqla-url", SERVER)
    digits = hashlib.sha256(b"nightly-build").hexdigest()[:8]
    result.stdout.fnmatch_lines([f"sqla: *, database=run (app_{digits})"])


def test_the_async_url_alone_names_the_database(project: Project, run_id: str) -> None:
    project.ini(sqla_database="run")
    project.tests("def test_nothing():\n    pass\n")
    result = project.run("--sqla-async-url", "postgresql+asyncpg://ada@db/app")
    result.stdout.fnmatch_lines(["sqla: async_url=*, database=run (app_01234567)"])


# ------------------------------------------------------------ the URL fixtures


def test_in_configured_mode_the_database_urls_are_the_configured_ones(project: Project) -> None:
    """No connection: in configured mode on a server the fixtures only parse the URLs."""
    project.ini(sqla_url=SERVER, sqla_async_url="postgresql+asyncpg://ada:s3cret@db.example/app")
    project.tests(
        """
        from sqlalchemy import make_url

        def test_urls(sqla_url, sqla_async_url, sqla_database_url, sqla_async_database_url):
            assert sqla_database_url == make_url(sqla_url)
            assert sqla_async_database_url == make_url(sqla_async_url)
        """
    )
    project.run().assert_outcomes(passed=1)


def test_without_a_url_the_database_urls_are_none(project: Project) -> None:
    project.tests(
        """
        def test_urls(sqla_database_url, sqla_async_database_url):
            assert sqla_database_url is None
            assert sqla_async_database_url is None
        """
    )
    project.run().assert_outcomes(passed=1)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (
            '"oracle://ada:s3cret@db.example/app"',
            "*sqla_database = run creates databases on PostgreSQL, MySQL and MariaDB, and has no "
            "effect on SQLite; oracle://ada:***@db.example/app is oracle.*",
        ),
        (
            '"postgresql+psycopg://ada:s3cret@db.example"',
            "*sqla_database = run names its database after the one in the URL, but "
            "postgresql+psycopg://ada:***@db.example names none.*",
        ),
        (
            '"postgresql+psycopg://ada:s3cret@db.example/' + "a" * 60 + '"',
            "*_01234567, which is 69 bytes long, but PostgreSQL takes at most 63.*",
        ),
    ],
    ids=["another dialect", "no database", "too long"],
)
def test_an_overridden_url_is_checked_where_a_test_asks_for_it(
    project: Project, run_id: str, value: str, message: str
) -> None:
    """An override of sqla_url, such as a container's URL, is the base the database derives
    from, so the checks that ran on the configured URL at startup run on it at setup."""
    project.ini(sqla_database="run")
    project.conftest(
        f"""
        import pytest

        @pytest.fixture(scope="session")
        def sqla_url():
            return {value}
        """
    )
    project.tests("def test_db(sqla_database_url):\n    pass\n\ndef test_unrelated():\n    pass\n")
    result = project.run()
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines([message])
    assert "s3cret" not in result.stdout.str()


# --------------------------------------------------------------------- SQLite


SQLITE_TESTS = """
from models import User, count

def test_commits(sqla_session, sqla_database_url):
    assert sqla_session.get_bind().engine.url == sqla_database_url
    sqla_session.add(User(email="ada@example.com"))
    sqla_session.commit()
    assert count(sqla_session) == 1

def test_rolled_back(sqla_session):
    assert count(sqla_session) == 0
"""


@pytest.mark.parametrize("mode", ["run", "worker"])
def test_on_a_sqlite_file_run_and_worker_change_nothing(project: Project, mode: str) -> None:
    project.models()
    database = project.sqlite()
    project.ini(sqla_database=mode)
    project.tests(
        SQLITE_TESTS
        + """
def test_the_configured_file(sqla_database_url, sqla_url):
    assert sqla_database_url == sqla_url
"""
    )
    project.run().assert_outcomes(passed=3)
    assert table_names(database) == []


def test_in_memory_the_database_url_is_the_shared_memory_database(project: Project) -> None:
    """The memdb database the sync and async engines share, held open while the URL is."""
    project.models()
    project.ini(sqla_url="sqlite://", sqla_database="run")
    project.tests(
        SQLITE_TESTS
        + """
def test_the_memdb_database(sqla_database_url, sqla_async_database_url):
    assert sqla_database_url.query["vfs"] == "memdb"
    assert sqla_async_database_url == sqla_database_url
"""
    )
    project.run().assert_outcomes(passed=3)


def test_keep_database_on_sqlite_keeps_nothing(project: Project) -> None:
    project.models()
    project.sqlite()
    project.ini(sqla_database="worker")
    project.tests(SQLITE_TESTS)
    result = project.run("--sqla-keep-database")
    result.assert_outcomes(passed=2)
    result.stdout.no_fnmatch_line("*pytest-sqla*kept*")
