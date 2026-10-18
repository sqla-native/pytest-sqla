"""What a sync-only install sees: the async fixtures fail with a clear message.

These run in the sync-only CI job too, where pytest-asyncio, anyio and
greenlet are not installed, so they prove plain ``pip install pytest-sqla``
works on SQLAlchemy 2.1.
"""

from __future__ import annotations

import pytest

from pytest_sqla import _asyncio, plugin
from tests.helpers import Project

F5 = (
    "*pytest-sqla: {fixture} needs an async test, but no async plugin runs "
    "test_inner.py::{test}. Mark it with @pytest.mark.asyncio (pip install "
    "pytest-sqla[[]asyncio]) or @pytest.mark.anyio, or set asyncio_mode = auto."
)


@pytest.mark.parametrize(
    "fixture", ["sqla_async_connection", "sqla_async_sessionmaker", "sqla_async_session"]
)
def test_a_sync_test_cannot_have_an_async_fixture(project: Project, fixture: str) -> None:
    project.models()
    project.sqlite()
    project.tests(f"def test_sync({fixture}):\n    pass\n")
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines([F5.format(fixture=fixture, test="test_sync")])


def test_an_async_test_without_an_async_plugin_fails_at_setup(project: Project) -> None:
    project.models()
    project.sqlite()
    project.ini(asyncio_mode=None, asyncio_default_fixture_loop_scope=None)
    project.tests("async def test_async(sqla_async_session):\n    pass\n")
    result = project.run("-p", "no:asyncio", "-p", "no:anyio")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines([F5.format(fixture="sqla_async_session", test="test_async")])


def test_the_sync_fixtures_work_without_any_async_plugin(project: Project) -> None:
    project.models()
    project.sqlite()
    project.ini(asyncio_mode=None, asyncio_default_fixture_loop_scope=None)
    project.tests(
        """
        from models import User, count

        def test_commits(sqla_session):
            sqla_session.add(User(email="ada@example.com"))
            sqla_session.commit()

        def test_rolled_back(sqla_session):
            assert count(sqla_session) == 0
        """
    )
    project.run("-p", "no:asyncio", "-p", "no:anyio").assert_outcomes(passed=2)


def test_optional_imports_degrade_to_nothing() -> None:
    assert plugin._optional_import("pytest_sqla_has_no_such_module") is None
    assert _asyncio.pytest_asyncio_fixtures(None) == {}
