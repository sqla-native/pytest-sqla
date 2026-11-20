"""sqla_schema = alembic with async drivers, on in-memory SQLite through aiosqlite.

Alembic's async template calls ``asyncio.run()``, which works only on a thread
with no running loop, and env.py's engine must be of the kind its URL is: a
synchronous engine on an async driver raises MissingGreenlet, an async one on
a synchronous driver "requires an async driver". ``sqla_alembic_url`` picks the
URL env.py gets; the messages say which value to set.
"""

from __future__ import annotations

import ast

import pytest

from tests.alembic_kit import ASYNC_COOKBOOK_ENV, ASYNC_DOGFOOD, alembic_project
from tests.helpers import Project

pytest.importorskip("alembic")
pytest.importorskip("aiosqlite")
pytest.importorskip("pytest_asyncio")

RECORDS = """
from pathlib import Path

from sqlalchemy import make_url

with Path(HANDED_PATH).open("a") as handed:
    url = make_url(config.get_main_option("sqlalchemy.url"))
    handed.write(repr((type(connectable).__name__, url.get_driver_name())) + "\\n")
"""


def recording(env: str, handed: object) -> str:
    """env.py, writing down what it was handed before it runs the migrations."""
    marker = "connectable = config.attributes.get"
    head, _, tail = env.partition(marker)
    line, _, rest = tail.partition("\n")
    return head + marker + line + "\n" + RECORDS.replace("HANDED_PATH", repr(str(handed))) + rest


def test_the_async_template_migrates_an_async_only_configuration(project: Project) -> None:
    """sqla_url names an async driver: no sync connection exists outside a loop, so env.py
    gets the URL alone, and asyncio.run() runs on a thread with no loop of its own."""
    project.ini(sqla_url="sqlite+aiosqlite://")
    alembic_project(project, template="async", tests=ASYNC_DOGFOOD)
    project.run().assert_outcomes(passed=3)


def test_the_async_cookbook_env_py_gets_the_url_alone(project: Project) -> None:
    handed = project.path / "handed.txt"
    project.ini(sqla_url="sqlite+aiosqlite://")
    alembic_project(project, env=recording(ASYNC_COOKBOOK_ENV, handed), tests=ASYNC_DOGFOOD)
    project.run().assert_outcomes(passed=3)
    seen = [ast.literal_eval(line) for line in handed.read_text().splitlines()]
    assert seen == [("NoneType", "aiosqlite")] * 2, "the upgrade, then the check"


def test_the_async_cookbook_env_py_uses_the_sync_connection_when_there_is_one(
    project: Project,
) -> None:
    """sqla_url = sqlite:// for the connection; the async tests share its memdb database."""
    handed = project.path / "handed.txt"
    project.ini(sqla_url="sqlite://", sqla_async_url="sqlite+aiosqlite://")
    alembic_project(project, env=recording(ASYNC_COOKBOOK_ENV, handed), tests=ASYNC_DOGFOOD)
    project.run().assert_outcomes(passed=3)
    seen = [ast.literal_eval(line) for line in handed.read_text().splitlines()]
    assert seen == [("Connection", "pysqlite")] * 2


def test_sqla_alembic_url_async_hands_the_async_template_sqla_async_urls_driver(
    project: Project,
) -> None:
    """The default is sync while sqla_url is set: the async template then gets pysqlite."""
    project.ini(sqla_url="sqlite://", sqla_async_url="sqlite+aiosqlite://")
    alembic_project(project, template="async", tests=ASYNC_DOGFOOD)
    refused = project.run()
    refused.assert_outcomes(errors=3)
    refused.stdout.fnmatch_lines(
        [
            "*SchemaConflictError: pytest-sqla: env.py opened an async engine, and "
            "sqlalchemy.url, sqlite:///file*/pytest-sqla-*?uri=true&vfs=memdb, uses the "
            "synchronous driver pysqlite. SQLAlchemy raised InvalidRequestError: The asyncio "
            "extension requires an async driver to be used. The loaded 'pysqlite' is not async. "
            "Set sqla_alembic_url = async, which hands env.py sqla_async_url's driver, with "
            "sqla_async_url naming an async driver for the database (for example "
            "sqlite+aiosqlite://...); or make env.py synchronous, like Alembic's generic "
            "template."
        ]
    )
    project.ini(sqla_alembic_url="async")
    project.run().assert_outcomes(passed=3)


def test_a_sync_env_py_on_an_async_only_configuration_is_told_what_to_set(
    project: Project,
) -> None:
    project.ini(sqla_url="sqlite+aiosqlite://")
    alembic_project(project, tests=ASYNC_DOGFOOD)
    result = project.run()
    result.assert_outcomes(errors=3)
    result.stdout.fnmatch_lines(
        [
            "*SchemaConflictError: pytest-sqla: env.py opened a synchronous engine, and "
            "sqlalchemy.url, sqlite+aiosqlite:///file*/pytest-sqla-*?uri=true&vfs=memdb, uses "
            "the async driver aiosqlite. SQLAlchemy raised MissingGreenlet: greenlet_spawn has "
            "not been called; can't call await_*() here.* Set sqla_alembic_url = sync, which "
            "hands env.py sqla_url's driver, with sqla_url naming a synchronous driver for the "
            "database (for example sqlite://...) and sqla_async_url the async one; or make "
            "env.py async, like Alembic's async template."
        ]
    )


def test_sqla_alembic_url_sync_needs_sqla_url(project: Project) -> None:
    project.ini(sqla_async_url="sqlite+aiosqlite://", sqla_alembic_url="sync")
    alembic_project(project, template="async", tests=ASYNC_DOGFOOD)
    project.ini(sqla_url=None)
    result = project.run()
    result.assert_outcomes(errors=3)
    result.stdout.fnmatch_lines(
        [
            "*pytest-sqla: sqla_alembic_url = sync hands env.py the database through "
            "sqla_url's driver, and sqla_url is not set. Set it, or sqla_alembic_url to the "
            "other value."
        ]
    )


def test_the_migration_runs_on_a_thread_of_its_own_even_inside_a_running_loop(
    project: Project,
) -> None:
    """Where an async admin URL calls it: inside run_sync, on a running loop. Measured with
    the migration run there directly instead, the async template's asyncio.run() raised
    "cannot be called from a running event loop" and left "coroutine
    'run_async_migrations' was never awaited"."""
    import asyncio
    import sqlite3

    from alembic.config import Config
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine

    from pytest_sqla import _alembic
    from pytest_sqla._config import Settings, Targets

    alembic_project(project, template="async")
    name = f"/pytest-sqla-loop-{project.path.name}"
    url = make_url(f"sqlite+aiosqlite:///file:{name}?vfs=memdb&uri=true")
    settings = Settings(
        url=None,
        async_url=None,
        metadata_path=None,
        schema_mode="alembic",
        lock_timeout=30.0,
        recreate_schema=False,
        run_id="0123456789abcdef",
        worker=None,
        memory_name=name,
        database_mode="configured",
        keep_database=False,
    )
    targets = Targets(
        sync=None,
        asynchronous=url,
        async_setting="sqla_async_url",
        keeper=None,
        worker_file=None,
        disposable=True,
    )
    migration = _alembic.plan(
        Config(str(project.path / "alembic.ini")), targets, settings, {}, project.path
    )

    async def inside_a_loop() -> None:
        engine = create_async_engine("sqlite+aiosqlite://")
        try:
            async with engine.connect() as connection:
                await connection.run_sync(lambda _: _alembic.migrate(migration))
        finally:
            await engine.dispose()

    keeper = sqlite3.connect(f"file:{name}?vfs=memdb", uri=True)
    try:
        asyncio.run(inside_a_loop())
        tables = keeper.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    finally:
        keeper.close()
    assert [table for (table,) in tables] == ["alembic_version", "note", "user_account"]
