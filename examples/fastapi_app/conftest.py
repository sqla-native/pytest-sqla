from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fastapi_app.app import app, get_session


@pytest.fixture(scope="session")
def sqla_session_options() -> dict[str, Any]:
    """Make the test sessions behave like the app's: SessionLocal does not expire on commit.

    With the default, ``user.id`` after ``commit()`` would reload the row with
    sync IO outside the event loop, and SQLAlchemy raises MissingGreenlet.
    """
    return {"expire_on_commit": False}


@pytest_asyncio.fixture
async def client(
    sqla_async_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    """The app, with every request's session inside the test transaction.

    httpx.AsyncClient with ASGITransport runs the app on the test's own event
    loop. Starlette's TestClient would run it in a thread with a loop of its
    own, and the test's connection belongs to the test's loop.
    """

    async def override() -> AsyncIterator[AsyncSession]:
        async with sqla_async_sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()
