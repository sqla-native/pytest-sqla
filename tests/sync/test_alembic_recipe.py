"""The README's Alembic recipe, run as written: the inner conftest is its code block.

Measured before the recipe was fixed: a password with "@" or "%" made Alembic's
config raise "invalid interpolation syntax", a str sqla_url (the testcontainers
recipe returns one) had no render_as_string(), and the block showed no imports.
The real-database suite runs it against PostgreSQL with such a password.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.helpers import Project, table_names

pytest.importorskip("alembic")

README = Path(__file__).resolve().parents[2] / "README.md"
ALEMBIC_INI = "[alembic]\nscript_location = %(here)s/migrations\n"
ENV_PY = """
from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
engine = engine_from_config(
    config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool
)
with engine.connect() as connection:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
"""
VERSION = """
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


def downgrade():
    op.drop_table("user_account")
"""
TESTS = """
from models import User, count

def test_commits(sqla_session):
    sqla_session.add(User(email="ada@example.com"))
    sqla_session.commit()
    assert count(sqla_session) == 1

def test_rolled_back(sqla_session):
    assert count(sqla_session) == 0
"""


def readme_recipe() -> str:
    blocks = re.findall(r"```python\n(.*?)```", README.read_text(), flags=re.DOTALL)
    (recipe,) = [block for block in blocks if "alembic.command.upgrade" in block]
    return recipe


def alembic_project(project: Project, conftest: str) -> Project:
    """Models for the tests to use, and Alembic, not pytest-sqla, building the schema."""
    project.models()
    project.ini(sqla_schema="none")
    (project.path / "alembic.ini").write_text(ALEMBIC_INI)
    versions = project.path / "migrations" / "versions"
    versions.mkdir(parents=True)
    (project.path / "migrations" / "env.py").write_text(ENV_PY)
    (versions / "0001_user_account.py").write_text(VERSION)
    project.conftest(conftest)
    project.tests(TESTS)
    return project


def test_the_recipe_with_the_configured_url(project: Project) -> None:
    url = project.sqlite("app.db")
    alembic_project(project, readme_recipe()).run().assert_outcomes(passed=2)
    assert table_names(url) == ["alembic_version"], "upgraded, then downgraded to base"


def test_the_recipe_with_an_overridden_str_url(project: Project) -> None:
    path = project.path / "container.db"
    override = f"""
import pytest

@pytest.fixture(scope="session")
def sqla_url():
    return "sqlite:///{path}"
"""
    alembic_project(project, override + readme_recipe()).run().assert_outcomes(passed=2)
    assert table_names(f"sqlite:///{path}") == ["alembic_version"]
