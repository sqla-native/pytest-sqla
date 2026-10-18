"""A small FastAPI service in the usual shape: one AsyncSession per request.

The application owns its engine and its ``get_session`` dependency. Nothing in
here knows about tests; the tests override ``get_session`` with pytest-sqla's
``sqla_async_sessionmaker``, so every request joins the test's transaction.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import String, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "api_user"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(100), unique=True)


# Created at import, never connected in the tests: creating an engine opens nothing.
engine = create_async_engine(os.environ.get("APP_DATABASE_URL", "sqlite+aiosqlite:///app.db"))
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


app = FastAPI()


@app.post("/users", status_code=201)
async def create_user(email: str, session: AsyncSession = Depends(get_session)) -> dict[str, object]:
    user = User(email=email)
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="email taken") from None
    return {"id": user.id, "email": user.email}


@app.get("/users/count")
async def count_users(session: AsyncSession = Depends(get_session)) -> dict[str, int]:
    total = await session.scalar(select(func.count()).select_from(User))
    return {"count": total or 0}
