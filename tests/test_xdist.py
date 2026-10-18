"""pytest-xdist: every worker is its own process, so these run in a subprocess.

The real-database half of this - one shared PostgreSQL or MySQL database,
created once under a lock and dropped by the last worker - is in
``tests/integration/test_xdist_backends.py``.
"""

from __future__ import annotations

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
