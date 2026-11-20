"""pytest-xdist: every worker is its own process.

Most of these run in a subprocess. The ones about what the controller does
run in-process, so coverage sees the controller's hooks; the workers are
processes of their own either way. The real-database half - one shared
PostgreSQL or MySQL database, created once under a lock and dropped by the
last worker, and databases of pytest-sqla's own - is in
``tests/integration/test_xdist_backends.py`` and ``test_databases.py``.
"""

from __future__ import annotations

import re
import sys

import pytest

from tests.helpers import Project, sqlite_script

pytest.importorskip("xdist")

MANY_TESTS = """
import pytest
from models import User, count

@pytest.mark.parametrize("n", range(9))
def test_isolated(sqla_session, n):
    assert count(sqla_session) == 0
    sqla_session.add(User(email=f"user{n}@example.com"))
    sqla_session.commit()
    assert count(sqla_session) == 1
"""


def test_a_sqlite_file_becomes_one_file_per_worker_and_is_removed(project: Project) -> None:
    project.models()
    project.sqlite()
    project.tests(
        MANY_TESTS
        + """
def test_own_file(sqla_connection, worker_id):
    assert sqla_connection.engine.url.database.endswith(f"test-{worker_id}.db")
"""
    )
    result = project.run_subprocess("-n", "3")
    result.assert_outcomes(passed=10)
    assert sorted(path.name for path in project.path.glob("*.db*")) == []


def test_in_memory_databases_are_per_worker(project: Project) -> None:
    project.models()
    project.ini(sqla_url="sqlite://")
    project.tests(MANY_TESTS)
    project.run_subprocess("-n", "3").assert_outcomes(passed=9)


def test_none_mode_shares_the_configured_file(project: Project) -> None:
    project.models()
    project.sqlite()
    project.ini(sqla_schema="none")
    sqlite_script(
        project.path / "test.db",
        ["CREATE TABLE user_account (id INTEGER PRIMARY KEY, email VARCHAR(100) UNIQUE)"],
    )
    project.tests(MANY_TESTS)
    project.run_subprocess("-n", "2").assert_outcomes(passed=9)


@pytest.mark.skipif(sys.version_info < (3, 12), reason="sqlite3 autocommit arrived in 3.12")
def test_none_mode_shares_the_file_in_pep_249_mode_too(project: Project) -> None:
    """Measured without the write lock at BEGIN: "1 failed, 8 passed", database is locked."""
    project.models()
    project.sqlite()
    project.ini(sqla_schema="none")
    project.conftest(
        """
        import pytest

        @pytest.fixture(scope="session")
        def sqla_engine_options():
            return {"connect_args": {"autocommit": False}}
        """
    )
    sqlite_script(
        project.path / "test.db",
        ["CREATE TABLE user_account (id INTEGER PRIMARY KEY, email VARCHAR(100) UNIQUE)"],
    )
    project.tests(MANY_TESTS)
    project.run_subprocess("-n", "2").assert_outcomes(passed=9)


# ------------------------------------------- the controller and its workers


def test_the_controller_hands_its_run_id_to_every_worker(project: Project) -> None:
    """The controller's header names the databases its workers use.

    xdist generates its own testrun_uid only after the header is printed, so
    pytest-sqla hands the workers the controller's run id. Nothing connects:
    these tests never ask for a database.
    """
    ids = project.path / "ids"
    ids.mkdir()
    project.ini(sqla_url="postgresql+psycopg://ada@db.example/app", sqla_database="worker")
    project.tests(
        f"""
        import pytest
        from pathlib import Path
        from pytest_sqla._config import SETTINGS

        @pytest.mark.parametrize("n", range(4))
        def test_run_id(pytestconfig, worker_id, n):
            run_id = pytestconfig.stash[SETTINGS].run_id
            Path({str(ids)!r}, f"{{worker_id}}-{{n}}").write_text(run_id)
        """
    )
    result = project.run("-n", "2")
    result.assert_outcomes(passed=4)
    header = re.search(r"database=worker \(app_([0-9a-f]{8})_gwN\)", result.stdout.str())
    assert header is not None, result.stdout.str()
    run_ids = {path.read_text() for path in ids.iterdir()}
    assert len(run_ids) == 1
    assert run_ids.pop()[:8] == header[1]


def test_what_a_worker_announces_reaches_the_controllers_summary(project: Project) -> None:
    """The way --sqla-keep-database's names reach the terminal, once, from any worker."""
    project.conftest(
        """
        def pytest_sessionfinish(session):
            output = getattr(session.config, "workeroutput", None)
            if output is not None:
                output.setdefault("pytest_sqla", []).append("kept the database example")
        """
    )
    project.tests("def test_one():\n    pass\n\ndef test_two():\n    pass\n")
    result = project.run("-n", "2")
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(["*- pytest-sqla -*", "kept the database example"])
    assert result.stdout.str().count("kept the database example") == 1


def test_the_controller_checks_the_longest_worker_name(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker that fails its own check is replaced by xdist, not reported (measured), so
    the controller refuses a name that fits the run but not the last worker."""
    monkeypatch.setenv("PYTEST_XDIST_TESTRUNUID", "0123456789abcdef0123456789abcdef")
    project.ini(sqla_url="postgresql+psycopg://ada@db/" + "a" * 51, sqla_database="worker")
    project.tests("def test_nothing():\n    pass\n")
    result = project.run("-n", "2")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*would name the database a*_01234567_gw1, which is 64 bytes*"])
