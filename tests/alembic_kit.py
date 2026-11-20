"""Inner projects whose schema Alembic builds, for the ``sqla_schema = alembic`` specs.

``alembic init`` writes the project the way a user's looks, and the env.py
variants below are the ones pytest-sqla meets: Alembic's own templates, which
build an engine from ``sqlalchemy.url``, the cookbook's, which uses
``config.attributes["connection"]``, and a few that misbehave on purpose.
Alembic is imported inside the functions, so collecting a module that imports
this one needs no Alembic.
"""

from __future__ import annotations

import contextlib
import io
import os
import textwrap
from collections.abc import Sequence
from pathlib import Path

from tests.helpers import MODELS, Project

MIGRATION = '''
"""user_account and note, the tables of tests.helpers.MODELS"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_account",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(100), unique=True),
    )
    op.create_table(
        "note",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("user_account.id")),
        sa.Column("body", sa.String(200)),
    )


def downgrade():
    op.drop_table("note")
    op.drop_table("user_account")
'''

FAILING = '''
"""creates a table, then fails"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("half_done", sa.Column("id", sa.Integer, primary_key=True))
    raise RuntimeError("migration 0002 fails on purpose")


def downgrade():
    pass
'''

# Two heads with branch labels, as pass-culture labels them: r0, then pre1 -> pre2
# and post1 -> post2, where post2 depends on pre1.
BRANCH = """
import sqlalchemy as sa
from alembic import op

revision = "{revision}"
down_revision = {down}
branch_labels = {labels}
depends_on = {depends}


def upgrade():
    op.create_table("t_{revision}", sa.Column("id", sa.Integer, primary_key=True))


def downgrade():
    op.drop_table("t_{revision}")
"""
BRANCHES = [
    BRANCH.format(revision=revision, down=down, labels=labels, depends=depends)
    for revision, down, labels, depends in [
        ("r0", None, None, None),
        ("pre1", '"r0"', '("pre",)', None),
        ("pre2", '"pre1"', None, None),
        ("post1", '"r0"', '("post",)', None),
        ("post2", '"post1"', None, '"pre1"'),
    ]
]

# Alembic's cookbook, "Sharing a Connection across one or more programmatic
# migration commands", with the template's fileConfig() kept.
COOKBOOK_ENV = """
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def run_migrations_online():
    connectable = config.attributes.get("connection", None)
    if connectable is None:
        connectable = engine_from_config(
            config.get_section(config.config_ini_section) or {},
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
        with connectable.connect() as connection:
            context.configure(connection=connection)
            with context.begin_transaction():
                context.run_migrations()
    else:
        context.configure(connection=connectable)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
"""

# Its asyncio twin, "Programmatic API use (connection sharing) With Asyncio".
ASYNC_COOKBOOK_ENV = """
import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config


def do_run_migrations(connection):
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations():
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section) or {},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


connectable = config.attributes.get("connection", None)
if connectable is None:
    asyncio.run(run_async_migrations())
else:
    do_run_migrations(connectable)
"""

# The pattern the guard is for: the application's own variable wins over
# sqlalchemy.url, so in a test run env.py would migrate the application's database.
APP_DATABASE_ENV = """
import os

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
url = os.environ.get("APP_DATABASE_URL") or config.get_main_option("sqlalchemy.url")
connectable = engine_from_config(
    {"sqlalchemy.url": url}, prefix="sqlalchemy.", poolclass=pool.NullPool
)
with connectable.connect() as connection:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()
"""

# An env.py that never runs the migrations online, so nothing is recorded.
IDLE_ENV = """
from alembic import context

config = context.config
"""


def alembic_project(
    project: Project,
    *,
    template: str = "generic",
    env: str | None = None,
    revisions: Sequence[str] = (MIGRATION,),
    tests: str | None = None,
) -> Path:
    """``alembic init`` in the inner project, the revisions, and tests that dogfood the promise.

    Returns the migrations directory. ``sqla_url`` defaults to an in-memory
    SQLite database; the models the tests use are tests.helpers.MODELS, but
    pytest-sqla never reads them: ``sqla_metadata`` is not needed here.
    """
    from alembic import command
    from alembic.config import Config

    project.pytester.makepyfile(models=MODELS)
    project.settings.setdefault("sqla_url", "sqlite://")
    project.ini(sqla_schema="alembic")
    migrations = project.path / "migrations"
    config = Config(
        str(project.path / "alembic.ini"), toml_file=str(project.path / "pyproject.toml")
    )
    # Relative, as a user types it: alembic 1.16+ then writes %(here)s/migrations.
    with contextlib.redirect_stdout(io.StringIO()):
        command.init(config, os.path.relpath(migrations), template=template)
    for index, revision in enumerate(revisions):
        (migrations / "versions" / f"{index:04}.py").write_text(textwrap.dedent(revision).lstrip())
    if env is not None:
        (migrations / "env.py").write_text(textwrap.dedent(env).lstrip())
    project.tests(DOGFOOD if tests is None else tests)
    return migrations


DOGFOOD = """
from sqlalchemy import text
from models import User, count


def test_commits(sqla_session):
    sqla_session.add(User(email="ada@example.com"))
    sqla_session.commit()
    assert count(sqla_session) == 1


def test_the_commit_was_rolled_back(sqla_session):
    assert count(sqla_session) == 0


def test_the_revision_is_recorded(sqla_connection):
    assert sqla_connection.scalar(text("SELECT version_num FROM alembic_version")) == "0001"
"""

ASYNC_DOGFOOD = """
import pytest
from sqlalchemy import text
from models import User, acount


@pytest.mark.asyncio
async def test_commits(sqla_async_session):
    sqla_async_session.add(User(email="ada@example.com"))
    await sqla_async_session.commit()
    assert await acount(sqla_async_session) == 1


@pytest.mark.asyncio
async def test_the_commit_was_rolled_back(sqla_async_session):
    assert await acount(sqla_async_session) == 0


@pytest.mark.asyncio
async def test_the_revision_is_recorded(sqla_async_connection):
    version = await sqla_async_connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert version == "0001"
"""


def ini_lines(path: Path, **options: str | None) -> None:
    """Set or remove options in the ``[alembic]`` section of an ini file ``alembic init`` wrote."""
    lines = path.read_text().splitlines()
    start = lines.index("[alembic]") + 1
    kept = [
        line
        for line in lines[start:]
        if "=" not in line or line.split("=")[0].strip() not in options
    ]
    added = [f"{key} = {value}" for key, value in options.items() if value is not None]
    path.write_text("\n".join([*lines[:start], *added, *kept]) + "\n")
