"""sqla_schema = alembic: what is checked at configure time, before any test runs.

Nothing here needs Alembic. The sync-only CI job, which has none installed,
runs these too: there the mode fails with the usage error that names the
extra, for real; everywhere else an inner conftest makes ``import alembic``
fail the way it fails there.
"""

from __future__ import annotations

import importlib.util

import pytest

from tests.helpers import Project
from tests.sync.test_config import usage_error

HAS_ALEMBIC = importlib.util.find_spec("alembic") is not None
NOT_INSTALLED = (
    "*pytest-sqla: sqla_schema = alembic runs your Alembic migrations, and Alembic is not "
    "installed. Install pytest-sqla[[]alembic]."
)


def test_without_alembic_the_mode_is_a_usage_error_that_names_the_extra(
    project: Project,
) -> None:
    """None in sys.modules makes the import fail, and find_spec report no Alembic."""
    project.ini(sqla_schema="alembic", sqla_url="sqlite://")
    project.conftest("import sys\n\nsys.modules['alembic'] = None\n")
    result = usage_error(project)
    result.stderr.fnmatch_lines([NOT_INSTALLED])


def test_the_mode_runs_wherever_alembic_is_installed(project: Project) -> None:
    """In the sync-only job, without Alembic, the real thing: the usage error. Elsewhere a run
    whose tests never touch the database needs no alembic.ini either."""
    project.ini(sqla_schema="alembic", sqla_url="sqlite://")
    project.tests("def test_nothing():\n    pass\n")
    result = project.run()
    if HAS_ALEMBIC:
        result.assert_outcomes(passed=1)
    else:
        assert result.ret == pytest.ExitCode.USAGE_ERROR
        result.stderr.fnmatch_lines([NOT_INSTALLED])


@pytest.mark.parametrize(
    ("url", "message"),
    [
        (
            "sqlite:///app.db",
            "sqla_schema = alembic migrates an in-memory SQLite database, such as sqla_url = "
            "sqlite://, and sqlite:///app.db is a file. pytest-sqla never deletes a file you "
            "named, so it could not give it back empty. Use an in-memory URL, or migrate the file "
            "before the run and set sqla_schema = none.",
        ),
        (
            "postgresql+psycopg://ada:s3cret@db.example/app",
            "sqla_schema = alembic migrates a database pytest-sqla creates and drops, and "
            "sqla_database = configured uses postgresql+psycopg://ada:***@db.example/app as it "
            "is. Set sqla_database = run or worker, or migrate the database before the run and "
            "set sqla_schema = none.",
        ),
        (
            "mssql+pyodbc://ada:s3cret@db.example/app",
            "sqla_schema = alembic runs on PostgreSQL, MySQL and MariaDB, with sqla_database = "
            "run or worker, and on in-memory SQLite; mssql+pyodbc://ada:***@db.example/app is "
            "mssql. Migrate the database before the run and set sqla_schema = none.",
        ),
        (
            "sqlite:///file:/private?mode=memory&vfs=memdb&uri=true",
            "sqla_schema = alembic creates the tables on one connection and runs every test on "
            "another, but each connection to sqlite:///file*/private?mode=memory&uri=true&"
            "vfs=memdb gets its own empty database.*",
        ),
    ],
    ids=["a SQLite file", "configured", "another dialect", "a private in-memory URI"],
)
def test_the_mode_migrates_only_a_database_pytest_sqla_made(
    project: Project, url: str, message: str
) -> None:
    """Checked before Alembic is looked for: installing it would not help."""
    project.ini(sqla_schema="alembic")
    project.conftest("import sys\n\nsys.modules['alembic'] = None\n")
    result = usage_error(project, "--sqla-url", url)
    result.stderr.fnmatch_lines([f"*pytest-sqla: {message}"])
    assert "s3cret" not in result.stderr.str()


@pytest.mark.parametrize(
    ("url", "database"),
    [
        ("sqlite://", "configured"),
        ("sqlite:///file:/shared?vfs=memdb&uri=true", "configured"),
        ("postgresql+psycopg://ada:s3cret@db.example/app", "run"),
        ("mysql+pymysql://ada:s3cret@db.example/app", "worker"),
    ],
    ids=["in memory", "a shared in-memory URI", "PostgreSQL, run", "MySQL, worker"],
)
def test_the_mode_accepts_the_databases_it_can_migrate(
    project: Project, url: str, database: str
) -> None:
    pytest.importorskip("alembic")
    project.ini(sqla_schema="alembic", sqla_database=database)
    project.tests("def test_nothing():\n    pass\n")
    result = project.run("--sqla-url", url)
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines([f"sqla: url=*, schema=alembic, database={database}*"])


@pytest.mark.parametrize("value", ["sqla_async_url", "Async", "both"])
def test_sqla_alembic_url_is_sync_or_async(project: Project, value: str) -> None:
    project.ini(sqla_alembic_url=value)
    result = usage_error(project)
    result.stderr.fnmatch_lines(
        [f"*pytest-sqla: sqla_alembic_url must be 'sync' or 'async', got {value!r}."]
    )
