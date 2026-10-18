"""The real-database suite's gate: skip in a clone, fail in CI.

Each backend and driver comes from an environment variable. A clone without
them skips these specs and stays green. CI sets ``SQLA_TEST_REQUIRE_ALL=1``,
which turns every such skip into a failure naming what is missing, because a
skipped suite that reports green looks like coverage that is not there.

    docker compose up -d --wait
    ./scripts/test-integration.sh
    docker compose down -v
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from tests.helpers import table_names

REQUIRE_ALL = os.environ.get("SQLA_TEST_REQUIRE_ALL") == "1"

# Every table an inner run may leave behind when a test breaks it on purpose.
TABLES = ("zz_broken", "note", "user_account", "pytest_sqla", "it_extra", "recipe_item")


@dataclass(frozen=True)
class Backend:
    name: str
    variable: str
    module: str
    sync: bool
    """Serves the sync fixtures."""
    asynchronous: bool
    """Serves the async fixtures."""

    @property
    def family(self) -> str:
        return self.name.split("+")[0]


BACKENDS = [
    Backend("postgresql+psycopg", "SQLA_TEST_POSTGRES_PSYCOPG_URL", "psycopg", True, True),
    Backend("postgresql+asyncpg", "SQLA_TEST_POSTGRES_ASYNCPG_URL", "asyncpg", False, True),
    Backend("mysql+pymysql", "SQLA_TEST_MYSQL_PYMYSQL_URL", "pymysql", True, False),
    Backend("mysql+aiomysql", "SQLA_TEST_MYSQL_AIOMYSQL_URL", "aiomysql", False, True),
    Backend("mysql+asyncmy", "SQLA_TEST_MYSQL_ASYNCMY_URL", "asyncmy", False, True),
]


def _params(
    *, sync: bool | None = None, family: str | None = None, name: str | None = None
) -> list[object]:
    return [
        pytest.param(backend, id=backend.name)
        for backend in BACKENDS
        if (sync is None or (backend.sync if sync else backend.asynchronous))
        and (family is None or backend.family == family)
        and (name is None or backend.name == name)
    ]


def url_for(backend: Backend) -> str:
    value = os.environ.get(backend.variable)
    problem = None if value else f"{backend.variable} is not set"
    if problem is None and importlib.util.find_spec(backend.module) is None:
        problem = f"the {backend.module} driver is not installed"
    if problem is None:
        return str(value)
    if REQUIRE_ALL:
        pytest.fail(f"{problem}, and SQLA_TEST_REQUIRE_ALL=1 forbids skipping", pytrace=False)
    pytest.skip(problem)


def sync_twin(url: str) -> str:
    """The same database through a sync driver, for setup and cleanup from outside."""
    parsed = make_url(url)
    driver = "postgresql+psycopg" if parsed.get_backend_name() == "postgresql" else "mysql+pymysql"
    return parsed.set(drivername=driver).render_as_string(hide_password=False)


def drop_leftovers(url: str) -> None:
    engine = create_engine(sync_twin(url), poolclass=NullPool)
    cascade = " CASCADE" if engine.dialect.name == "postgresql" else ""
    try:
        with engine.begin() as connection:
            for table in TABLES:
                connection.execute(text(f"DROP TABLE IF EXISTS {table}{cascade}"))
    finally:
        engine.dispose()


@dataclass(frozen=True)
class Database:
    backend: Backend
    url: str

    @property
    def family(self) -> str:
        return self.backend.family

    def tables(self) -> list[str]:
        return [name for name in table_names(sync_twin(self.url)) if name in TABLES]


def _database(request: pytest.FixtureRequest) -> Iterator[Database]:
    database = Database(request.param, url_for(request.param))
    drop_leftovers(database.url)
    yield database
    drop_leftovers(database.url)


@pytest.fixture(params=_params(sync=True))
def sync_database(request: pytest.FixtureRequest) -> Iterator[Database]:
    yield from _database(request)


@pytest.fixture(params=_params(sync=False))
def async_database(request: pytest.FixtureRequest) -> Iterator[Database]:
    yield from _database(request)


@pytest.fixture(params=_params(sync=True, family="postgresql"))
def postgresql_database(request: pytest.FixtureRequest) -> Iterator[Database]:
    yield from _database(request)


@pytest.fixture(params=_params(sync=True, family="mysql"))
def mysql_database(request: pytest.FixtureRequest) -> Iterator[Database]:
    yield from _database(request)


@pytest.fixture(params=_params(name="postgresql+asyncpg"))
def asyncpg_database(request: pytest.FixtureRequest) -> Iterator[Database]:
    yield from _database(request)
