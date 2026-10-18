"""The README's FastAPI example, run in CI on SQLite and on PostgreSQL."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi_app.app import User


@pytest.mark.asyncio
async def test_a_duplicate_email_is_a_conflict(
    client: AsyncClient, sqla_async_session: AsyncSession
) -> None:
    assert (await client.post("/users", params={"email": "ada@example.com"})).status_code == 201
    # The app commits, fails on the unique email, and rolls back: all inside
    # the test transaction, which survives its rollback.
    assert (await client.post("/users", params={"email": "ada@example.com"})).status_code == 409

    # The test sees what the app committed, on the same connection.
    assert await sqla_async_session.scalar(select(func.count()).select_from(User)) == 1
    assert (await client.get("/users/count")).json() == {"count": 1}


@pytest.mark.asyncio
async def test_the_previous_tests_commit_was_rolled_back(
    client: AsyncClient, sqla_async_session: AsyncSession
) -> None:
    assert (await client.get("/users/count")).json() == {"count": 0}
    assert await sqla_async_session.scalar(select(func.count()).select_from(User)) == 0


@pytest.mark.asyncio(loop_scope="module")
async def test_a_wider_loop_scope_works_the_same(client: AsyncClient) -> None:
    """The client fixture runs on the function loop; it only builds the client.

    Its requests run in this test, on the module loop, where pytest-sqla opened
    the connection for this test.
    """
    assert (await client.post("/users", params={"email": "grace@example.com"})).status_code == 201
    assert (await client.get("/users/count")).json() == {"count": 1}
