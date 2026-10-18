"""pytest-xdist workers sharing one real database.

Measured before this design: a naive session fixture calling create_all in
every worker, with ``-n 4``, ended "2 passed, 6 errors" on 3 of 3 runs per
backend. Here every worker runs tests that write, the schema must be created
once under the lock, joined by the siblings, and dropped by the last one out.
"""

from __future__ import annotations

import pytest

from tests.helpers import Project
from tests.integration.conftest import Database

pytest.importorskip("xdist")

WRITERS = """
import pytest
from models import User, acount, count

@pytest.mark.parametrize("n", range(12))
def test_sync_writer(sqla_session, n):
    assert count(sqla_session) == 0
    sqla_session.add(User(email=f"sync{n}@example.com"))
    sqla_session.commit()
    assert count(sqla_session) == 1
"""
ASYNC_WRITERS = """
import pytest
from models import User, acount

@pytest.mark.asyncio
@pytest.mark.parametrize("n", range(12))
async def test_async_writer(sqla_async_session, n):
    assert await acount(sqla_async_session) == 0
    sqla_async_session.add(User(email=f"async{n}@example.com"))
    await sqla_async_session.commit()
    assert await acount(sqla_async_session) == 1
"""


def test_sync_workers_share_one_schema(project: Project, sync_database: Database) -> None:
    project.models()
    project.tests(WRITERS)
    result = project.run_subprocess("-n", "3", "--sqla-url", sync_database.url)
    result.assert_outcomes(passed=12)
    assert sync_database.tables() == []


def test_async_workers_share_one_schema(project: Project, async_database: Database) -> None:
    """Async-only URLs: every worker's DDL runs on a thread of its own, under the lock."""
    project.models()
    project.tests(ASYNC_WRITERS)
    result = project.run_subprocess("-n", "3", "--sqla-url", async_database.url)
    result.assert_outcomes(passed=12)
    assert async_database.tables() == []
