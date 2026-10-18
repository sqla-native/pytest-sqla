"""The control for the recipe's one essential choice: the join mode.

Plain SQLAlchemy, no plugin: a test seeds a row, then application code hits an
IntegrityError, rolls back, and commits something else - the shape of a
"409 Conflict" endpoint. The real-database suite runs the same probe on
PostgreSQL and MySQL.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pytest_sqla import _sqlite

metadata = MetaData()
item = Table(
    "recipe_item",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(40), unique=True),
)


def after_an_app_rollback(engine: Engine, **session_options: object) -> tuple[int, int]:
    """(rows the test still sees after the app's rollback, rows left after teardown)."""
    with engine.connect() as connection:
        outer = connection.begin()
        session = Session(bind=connection, **session_options)  # type: ignore[arg-type]
        session.execute(item.insert().values(name="seeded-by-the-test"))
        session.commit()  # the test seeds through a session, as a fixture would
        try:
            session.execute(item.insert().values(name="seeded-by-the-test"))
        except IntegrityError:
            session.rollback()  # what the application does
        seen = int(session.scalar(select(func.count()).select_from(item)) or 0)
        session.execute(item.insert().values(name="written-after-the-rollback"))
        session.commit()
        session.close()
        if outer.is_active:
            outer.rollback()
    with engine.connect() as connection:
        left = int(connection.scalar(select(func.count()).select_from(item)) or 0)
    return seen, left


def run_probe(engine: Engine, probe: Callable[[Engine], tuple[int, int]]) -> tuple[int, int]:
    metadata.drop_all(engine)
    metadata.create_all(engine)
    try:
        return probe(engine)
    finally:
        metadata.drop_all(engine)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{tmp_path / 'recipe.db'}")
    _sqlite.apply_savepoint_fix(engine, {})
    return engine


def test_the_default_join_mode_loses_the_seed_and_leaks_the_next_commit(engine: Engine) -> None:
    """conditional_savepoint becomes rollback_only on a connection in a transaction."""
    assert run_probe(engine, after_an_app_rollback) == (0, 1)
    engine.dispose()


def test_create_savepoint_keeps_the_seed_and_leaks_nothing(engine: Engine) -> None:
    def probe(engine: Engine) -> tuple[int, int]:
        return after_an_app_rollback(engine, join_transaction_mode="create_savepoint")

    assert run_probe(engine, probe) == (1, 0)
    engine.dispose()
