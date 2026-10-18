"""What does one test's isolation cycle cost, pooled and with NullPool?

Committed rather than run once and forgotten, because the docs quote these
numbers and they should stay checkable. Re-run it before changing a claim.

    docker compose up -d --wait
    source scripts/local-databases.env
    python scripts/benchmark.py

The URLs come from the same SQLA_TEST_* variables as the real-database suite;
drivers without one are left out. One cycle is what pytest-sqla does per test:
connect, BEGIN and the guard SAVEPOINT, a session that inserts a row and
commits (a savepoint and its release), the guard's ROLLBACK TO, ROLLBACK, and
close. Best of 5 runs of 200 cycles, because a single run is too noisy to
publish a number from.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable

from sqlalchemy import Column, Integer, MetaData, String, Table
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from pytest_sqla._engines import async_engine, sync_engine
from pytest_sqla._isolation import end_isolation, open_isolation

CYCLES = 200
ROUNDS = 5
metadata = MetaData()
item = Table(
    "bench_item", metadata, Column("id", Integer, primary_key=True), Column("name", String(20))
)

SYNC = {
    "psycopg": "SQLA_TEST_POSTGRES_PSYCOPG_URL",
    "pymysql": "SQLA_TEST_MYSQL_PYMYSQL_URL",
}
ASYNC = {
    "asyncpg": "SQLA_TEST_POSTGRES_ASYNCPG_URL",
    "psycopg async": "SQLA_TEST_POSTGRES_PSYCOPG_URL",
    "aiomysql": "SQLA_TEST_MYSQL_AIOMYSQL_URL",
    "asyncmy": "SQLA_TEST_MYSQL_ASYNCMY_URL",
}


def sync_cycle(engine: Engine) -> None:
    with engine.connect() as connection:
        isolation = open_isolation(connection)
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            session.execute(item.insert().values(name="x"))
            session.commit()
        assert end_isolation(isolation, nodeid="bench", fixture="bench") is None


async def async_cycle(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        isolation = await connection.run_sync(open_isolation)
        async with AsyncSession(bind=connection, join_transaction_mode="create_savepoint") as s:
            await s.execute(item.insert().values(name="x"))
            await s.commit()
        breach = await connection.run_sync(
            lambda _connection: end_isolation(isolation, nodeid="bench", fixture="bench")
        )
        assert breach is None


def best_sync(engine: Engine) -> float:
    sync_cycle(engine)  # warm
    runs = []
    for _ in range(ROUNDS):
        start = time.perf_counter()
        for _ in range(CYCLES):
            sync_cycle(engine)
        runs.append((time.perf_counter() - start) * 1000 / CYCLES)
    return min(runs)


async def best_async(engine: AsyncEngine, cycle: Callable[[AsyncEngine], Awaitable[None]]) -> float:
    await cycle(engine)  # warm
    runs = []
    for _ in range(ROUNDS):
        start = time.perf_counter()
        for _ in range(CYCLES):
            await cycle(engine)
        runs.append((time.perf_counter() - start) * 1000 / CYCLES)
    return min(runs)


def measure_sync(url: URL) -> tuple[float, float]:
    pooled = sync_engine(url, {})
    metadata.create_all(pooled)
    try:
        nullpool = sync_engine(url, {"poolclass": NullPool})
        return best_sync(pooled), best_sync(nullpool)
    finally:
        metadata.drop_all(pooled)
        pooled.dispose()


async def measure_async(url: URL) -> tuple[float, float]:
    pooled = async_engine(url, {}, pooled=True)
    nullpool = async_engine(url, {}, pooled=False)
    async with pooled.begin() as connection:
        await connection.run_sync(metadata.create_all)
    try:
        return await best_async(pooled, async_cycle), await best_async(nullpool, async_cycle)
    finally:
        async with pooled.begin() as connection:
            await connection.run_sync(metadata.drop_all)
        await pooled.dispose()
        await nullpool.dispose()


def main() -> int:
    rows = []
    for name, variable in SYNC.items():
        if os.environ.get(variable):
            rows.append((name, *measure_sync(make_url(os.environ[variable]))))
    for name, variable in ASYNC.items():
        if os.environ.get(variable):
            rows.append((name, *asyncio.run(measure_async(make_url(os.environ[variable])))))
    if not rows:
        print("No SQLA_TEST_* URL is set; see scripts/test-integration.sh for the local ones.")
        return 1
    print(f"\nms per test, best of {ROUNDS} x {CYCLES} isolation cycles\n")
    print(f"  {'driver':<16}{'pooled':>10}{'NullPool':>12}{'difference':>14}")
    for name, pooled, nullpool in rows:
        print(f"  {name:<16}{pooled:>10.2f}{nullpool:>12.2f}{nullpool - pooled:>+14.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
