"""The guard of sqla_schema = alembic reads the database from each server driver's arguments.

No server is needed: these are the arguments SQLAlchemy hands the driver,
the same ones the guard's ``do_connect`` listener receives. Measured, the
drivers name the database under three keys: psycopg ``dbname``, asyncpg and
pymysql ``database``, aiomysql and asyncmy ``db``. The real-database suite
connects through every one of them with the guard installed.
"""

from __future__ import annotations

import importlib.util

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from pytest_sqla._alembic import _database_of

pytest.importorskip("alembic")


@pytest.mark.parametrize(
    ("driver", "module"),
    [
        ("postgresql+psycopg", "psycopg"),
        ("postgresql+asyncpg", "asyncpg"),
        ("mysql+pymysql", "pymysql"),
        ("mariadb+pymysql", "pymysql"),
        ("mysql+aiomysql", "aiomysql"),
        ("mysql+asyncmy", "asyncmy"),
    ],
)
def test_the_database_is_read_from_the_drivers_connect_arguments(driver: str, module: str) -> None:
    if importlib.util.find_spec(module) is None:
        pytest.skip(f"the {module} driver is not installed")
    url = make_url(f"{driver}://app:s3cret@db.example:4242/app_test_0123abcd")
    engine = create_engine(url, poolclass=NullPool)
    try:
        cargs, cparams = engine.dialect.create_connect_args(url)
        assert _database_of(engine.dialect, tuple(cargs), cparams) == "app_test_0123abcd"
    finally:
        engine.dispose()
