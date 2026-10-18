"""The same app under anyio, which FastAPI already depends on.

anyio runs a plain ``async def`` fixture when the test uses ``anyio_backend``,
so the client fixture here is an ordinary ``@pytest.fixture``. pytest-asyncio
stays in strict mode in this project and leaves these tests alone.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fastapi_app.app import app, get_session

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def anyio_client(
    sqla_async_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    async def override() -> AsyncIterator[AsyncSession]:
        async with sqla_async_sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def test_a_duplicate_email_is_a_conflict(anyio_client: AsyncClient) -> None:
    assert (await anyio_client.post("/users", params={"email": "ada@example.com"})).status_code == 201
    assert (await anyio_client.post("/users", params={"email": "ada@example.com"})).status_code == 409
    assert (await anyio_client.get("/users/count")).json() == {"count": 1}


async def test_the_previous_tests_commit_was_rolled_back(anyio_client: AsyncClient) -> None:
    assert (await anyio_client.get("/users/count")).json() == {"count": 0}
