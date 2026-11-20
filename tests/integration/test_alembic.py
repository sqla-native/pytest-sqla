"""sqla_schema = alembic against real PostgreSQL 17 and MySQL 8.4 servers, every driver.

The worker that creates the database of the run, or of one xdist worker,
migrates it right there, under the database lock: the others wait on the lock
and join a migrated database, and a migration that fails drops the database
again, so nobody ever joins one half migrated. Each spec runs an inner pytest
session with ``--sqla-url``, in-process where one process will do, so that
coverage sees the server branches, and in a subprocess under pytest-xdist.

These specs are the measurements the design's four server probes never ran:
Alembic's own transactions on an idle connection, CREATE INDEX CONCURRENTLY
while other workers wait on the lock, env.py's own engine next to the lock,
and the guard reading the database from every driver's connect arguments.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from packaging.version import Version
from sqlalchemy import __version__ as sqlalchemy_version
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from pytest_sqla._database import lock_key, lock_name
from tests.alembic_kit import (
    APP_DATABASE_ENV,
    ASYNC_DOGFOOD,
    BRANCHES,
    COOKBOOK_ENV,
    FAILING,
    MIGRATION,
    alembic_project,
)
from tests.helpers import Project, table_names
from tests.integration.conftest import Database, sync_twin
from tests.integration.test_databases import (
    LOG_STATEMENTS,
    RUN_ID,
    configured,
    drop_databases,
    owned_databases,
    run_database,
    with_database,
)
from tests.integration.test_real_databases import run_twin
from tests.integration.test_xdist_backends import WRITERS
from tests.sync.test_alembic_mode import (
    LOGGING_CONFTEST,
    LOGGING_TESTS,
    relative_project,
)

pytest.importorskip("alembic")

DATABASE_FIXTURES = (
    "sync_database",
    "async_database",
    "postgresql_database",
    "mysql_database",
    "asyncpg_database",
)


@pytest.fixture(autouse=True)
def _no_database_left_behind(request: pytest.FixtureRequest) -> Iterator[None]:
    """Each spec starts with no database named like pytest-sqla's own, and leaves none."""
    databases = [
        request.getfixturevalue(fixture)
        for fixture in DATABASE_FIXTURES
        if fixture in request.fixturenames
    ]
    for database in databases:
        drop_databases(database, owned_databases(database))
    yield
    for database in databases:
        drop_databases(database, owned_databases(database))


@pytest.fixture
def run_id(monkeypatch: pytest.MonkeyPatch) -> str:
    """A fixed run id, the one xdist would share, so the database's name is known."""
    monkeypatch.setenv("PYTEST_XDIST_TESTRUNUID", RUN_ID)
    return RUN_ID


def run(project: Project, database: Database, *args: str) -> pytest.RunResult:
    project.ini(sqla_database="run")
    return project.run("--sqla-url", database.url, *args)


# ------------------------------------------------------------ every env.py


@pytest.mark.parametrize("env", [None, COOKBOOK_ENV], ids=["template", "cookbook"])
def test_the_sync_env_pys_migrate_the_run_database(
    project: Project, sync_database: Database, run_id: str, env: str | None
) -> None:
    """The generic template builds its own engine from sqlalchemy.url; the cookbook's uses the
    idle connection it is handed, on which Alembic begins and commits its own transactions."""
    alembic_project(project, env=env)
    result = run(project, sync_database)
    result.assert_outcomes(passed=3)
    result.stdout.fnmatch_lines(
        [f"sqla: url=*, schema=alembic, database=run ({run_database(sync_database)})"]
    )
    assert owned_databases(sync_database) == []
    assert sync_database.tables() == [], "the configured database is never touched"


def test_the_async_template_migrates_the_run_database(
    project: Project, async_database: Database
) -> None:
    """psycopg, asyncpg, aiomysql and asyncmy. The admin connection of an async URL runs the
    protocol inside run_sync, on a loop: the template's asyncio.run() works only because the
    migration runs on a thread of its own, with no loop."""
    alembic_project(project, template="async", tests=ASYNC_DOGFOOD)
    run(project, async_database).assert_outcomes(passed=3)
    assert owned_databases(async_database) == []
    assert async_database.tables() == []


# --------------------------------------------------- CREATE INDEX CONCURRENTLY

CONCURRENTLY = '''
"""an index built concurrently, which runs in no transaction block"""
import time

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    time.sleep(SLEEP)  # long enough for a second worker to wait on the lock meanwhile
    with op.get_context().autocommit_block():
        op.create_index("ix_note_body", "note", ["body"], postgresql_concurrently=True)


def downgrade():
    pass
'''

INDEX_TESTS = """
from sqlalchemy import text

VALID = (
    "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
    "WHERE c.relname = 'ix_note_body'"
)


def test_the_index_is_valid(sqla_connection):
    assert sqla_connection.scalar(text(VALID)) is True
    assert sqla_connection.scalar(text("SELECT version_num FROM alembic_version")) == "0002"
"""

ASYNC_INDEX_TESTS = """
import pytest
from sqlalchemy import text

VALID = (
    "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
    "WHERE c.relname = 'ix_note_body'"
)


@pytest.mark.asyncio
async def test_the_index_is_valid(sqla_async_connection):
    assert await sqla_async_connection.scalar(text(VALID)) is True
"""


@pytest.mark.parametrize("env", [None, COOKBOOK_ENV], ids=["template", "cookbook"])
def test_an_autocommit_block_builds_an_index_concurrently(
    project: Project, postgresql_database: Database, env: str | None
) -> None:
    """Measured: inside a transaction of pytest-sqla's own, autocommit_block() raised a bare
    AssertionError. The connection is handed over idle, so it runs as in production."""
    alembic_project(
        project,
        env=env,
        revisions=(MIGRATION, CONCURRENTLY.replace("SLEEP", "0")),
        tests=INDEX_TESTS,
    )
    run(project, postgresql_database).assert_outcomes(passed=1)
    assert owned_databases(postgresql_database) == []


def test_the_async_template_builds_an_index_concurrently(
    project: Project, asyncpg_database: Database
) -> None:
    alembic_project(
        project,
        template="async",
        revisions=(MIGRATION, CONCURRENTLY.replace("SLEEP", "0")),
        tests=ASYNC_INDEX_TESTS,
    )
    run(project, asyncpg_database).assert_outcomes(passed=1)
    assert owned_databases(asyncpg_database) == []


def test_an_index_built_concurrently_while_another_worker_waits_on_the_lock(
    project: Project, postgresql_database: Database
) -> None:
    """The waiting worker is inside a statement on the configured database, which has a
    snapshot; CREATE INDEX CONCURRENTLY waits for older snapshots of its own database only,
    so it must not wait for that worker, which waits for it."""
    alembic_project(
        project,
        revisions=(MIGRATION, CONCURRENTLY.replace("SLEEP", "2")),
        tests=WRITERS + INDEX_TESTS,
    )
    project.ini(sqla_database="run", sqla_lock_timeout="60")
    result = project.run_subprocess("-n", "2", "--sqla-url", postgresql_database.url, timeout=300)
    result.assert_outcomes(passed=13)
    assert owned_databases(postgresql_database) == []


# ------------------------------------------------------------- pytest-xdist

COUNTED = '''
"""user_account and note, and a line per run of the migration"""
import os
import time
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    with Path(COUNT_PATH).open("a") as count:
        count.write(f"{os.getpid()} {op.get_bind().engine.url.database}\\n")
    time.sleep(2)  # the other worker asks for the database meanwhile
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
    pass
'''


def counted(count: Path) -> str:
    return COUNTED.replace("COUNT_PATH", repr(str(count)))


def statements(log: Path) -> list[tuple[str, str]]:
    """(pid, the first two words) of every CREATE and DROP DATABASE the run sent."""
    found = []
    for line in log.read_text().splitlines():
        pid, statement = line.split(" ", 1)
        found.append((pid, " ".join(statement.split(" ")[:2])))
    return found


def test_a_run_database_is_migrated_once_while_the_other_workers_wait(
    project: Project, sync_database: Database
) -> None:
    """Both workers ask for the database at once; the one that takes the lock creates and
    migrates it, the other waits on the lock and joins a migrated database: every test sees
    the tables, and the migration ran once."""
    count, log = project.path / "migrated.txt", project.path / "statements.log"
    alembic_project(project, revisions=(counted(count),), tests=WRITERS)
    project.conftest(LOG_STATEMENTS.replace("LOG_PATH", repr(str(log))))
    project.ini(sqla_database="run")
    result = project.run_subprocess("-n", "2", "--sqla-url", sync_database.url, timeout=300)
    result.assert_outcomes(passed=12)
    assert len(count.read_text().splitlines()) == 1, count.read_text()
    assert [kind for _, kind in statements(log)] == ["CREATE DATABASE", "DROP DATABASE"]
    assert owned_databases(sync_database) == []


WORKER_NAMES = """
import pytest
from pathlib import Path
from models import User, count

NAMES = Path(NAMES_PATH)


@pytest.mark.parametrize("n", range(6))
def test_writer(sqla_session, sqla_database_url, worker_id, n):
    (NAMES / worker_id).write_text(sqla_database_url.database)
    sqla_session.add(User(email=f"user{n}@example.com"))
    sqla_session.commit()
    assert count(sqla_session) == 1
"""


def test_in_worker_mode_each_worker_migrates_a_database_of_its_own(
    project: Project, sync_database: Database
) -> None:
    count, names = project.path / "migrated.txt", project.path / "names"
    names.mkdir()
    alembic_project(
        project,
        revisions=(counted(count),),
        tests=WORKER_NAMES.replace("NAMES_PATH", repr(str(names))),
    )
    project.ini(sqla_database="worker")
    result = project.run_subprocess("-n", "2", "--sqla-url", sync_database.url, timeout=300)
    result.assert_outcomes(passed=6)
    used = sorted(path.read_text() for path in names.iterdir())
    migrated = sorted(line.split(" ")[1] for line in count.read_text().splitlines())
    assert len(used) == 2, used
    assert migrated == used, "each worker migrated the database it used, once"
    assert owned_databases(sync_database) == []


# ------------------------------------------------------ a migration that fails


def test_a_failing_migration_drops_the_database_it_ran_in(
    project: Project, sync_database: Database, run_id: str
) -> None:
    """On MySQL the table the migration created before it failed stays, DDL commits on its
    own; it goes with the database."""
    alembic_project(project, revisions=(MIGRATION, FAILING))
    result = run(project, sync_database)
    result.assert_outcomes(errors=3)
    result.stdout.fnmatch_lines(["E * RuntimeError: migration 0002 fails on purpose"])
    assert owned_databases(sync_database) == []
    assert sync_database.tables() == []


def test_under_xdist_every_worker_reports_the_failing_migration_itself(
    project: Project, sync_database: Database
) -> None:
    """Nobody joins the half-migrated database: it is dropped under the lock, and the worker
    that waited creates it again, migrates it, fails the same way, and drops it too."""
    log = project.path / "statements.log"
    alembic_project(project, revisions=(MIGRATION, FAILING), tests=WRITERS)
    project.conftest(LOG_STATEMENTS.replace("LOG_PATH", repr(str(log))))
    project.ini(sqla_database="run")
    result = project.run_subprocess("-n", "2", "--sqla-url", sync_database.url, timeout=300)
    result.assert_outcomes(errors=12)
    assert result.stdout.str().count("RuntimeError: migration 0002 fails on purpose") >= 12
    sent = statements(log)
    creators = {pid for pid, kind in sent if kind == "CREATE DATABASE"}
    droppers = {pid for pid, kind in sent if kind == "DROP DATABASE"}
    assert len(sent) == 4, sent
    assert len(creators) == 2, "each worker created the database itself"
    assert droppers == creators, "and dropped it again"
    assert owned_databases(sync_database) == []


@pytest.mark.parametrize(
    "revisions", [(MIGRATION,), (MIGRATION, FAILING)], ids=["migrated", "failed"]
)
def test_keep_database_keeps_a_migrated_database_and_still_drops_a_failed_one(
    project: Project, sync_database: Database, run_id: str, revisions: tuple[str, ...]
) -> None:
    """A database whose migration failed goes whatever the option says: kept, a sibling
    worker could join it half migrated."""
    alembic_project(project, revisions=revisions)
    result = run(project, sync_database, "--sqla-keep-database")
    if len(revisions) == 2:
        result.assert_outcomes(errors=3)
        assert owned_databases(sync_database) == []
        return
    result.assert_outcomes(passed=3)
    assert owned_databases(sync_database) == [run_database(sync_database)]
    kept = with_database(sync_database, run_database(sync_database))
    assert {"alembic_version", "note", "user_account"} <= set(table_names(sync_twin(kept.url)))


REMARKED = '''
"""marks the database as another run's, then fails"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    # Committed on its own, as a rollback would otherwise undo it.
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        if bind.dialect.name == "postgresql":
            name = bind.scalar(sa.text("SELECT current_database()"))
            bind.exec_driver_sql(f'COMMENT ON DATABASE "{name}" IS \\'not pytest-sqla any more\\'')
        else:
            bind.exec_driver_sql("UPDATE pytest_sqla_database SET run_id = 'remarked'")
    raise RuntimeError("migration 0002 re-marked the database, then failed")


def downgrade():
    pass
'''


def test_a_database_a_failed_migration_re_marked_is_left_alone(
    project: Project, sync_database: Database, run_id: str
) -> None:
    """The DROP after a failure takes the same three checks as any other: the migration ran
    in between, and the mark is no longer this run's."""
    alembic_project(project, revisions=(MIGRATION, REMARKED))
    result = run(project, sync_database)
    result.assert_outcomes(errors=3)
    result.stdout.fnmatch_lines(["E * RuntimeError: migration 0002 re-marked the database*"])
    assert owned_databases(sync_database) == [run_database(sync_database)]


# --------------------------------------------------------------- the lock


def test_the_lock_wait_names_the_migration_and_the_two_remedies(
    project: Project, sync_database: Database, run_id: str
) -> None:
    name = run_database(sync_database)
    engine = create_engine(
        sync_twin(sync_database.url), poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    holder = engine.connect()
    try:
        if sync_database.family == "postgresql":
            holder.execute(select(func.pg_advisory_lock(lock_key(name))))
        else:
            assert holder.scalar(select(func.get_lock(lock_name(name), 1))) == 1
        alembic_project(project)
        project.ini(sqla_lock_timeout="1")
        result = run(project, sync_database)
    finally:
        holder.close()
        engine.dispose()
    result.assert_outcomes(errors=3)
    result.stdout.fnmatch_lines(
        [
            "*SchemaConflictError: pytest-sqla: gave up after 1 s waiting for the lock on the "
            f"database *{name}; another pytest-sqla process, usually a worker of this run, is "
            "creating, joining or dropping it. A crashed process releases the lock when its "
            "connection closes. With sqla_schema = alembic, the worker that creates the "
            "database runs the migrations while it holds the lock: raise sqla_lock_timeout "
            "above the time they take, or set sqla_database = worker, so that each worker "
            "migrates a database of its own.*"
        ]
    )
    assert owned_databases(sync_database) == []


# ---------------------------------------------------------------- the guard


def test_an_env_py_that_reaches_the_configured_database_is_refused(
    project: Project, sync_database: Database, run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused in do_connect, before any connection exists, and the database pytest-sqla
    created is dropped again."""
    monkeypatch.setenv("APP_DATABASE_URL", sync_twin(sync_database.url))
    alembic_project(project, env=APP_DATABASE_ENV)
    result = run(project, sync_database)
    result.assert_outcomes(errors=3)
    driver = make_url(sync_twin(sync_database.url)).drivername
    result.stdout.fnmatch_lines(
        [
            f"*SchemaConflictError: pytest-sqla: env.py or a migration opened a {driver} "
            f"connection to {configured(sync_database)!r}, and sqla_schema = alembic migrates "
            f"*/{run_database(sync_database)} only, so pytest-sqla refused it before it was "
            "used.*"
        ]
    )
    assert sync_database.tables() == [], "nothing reached the configured database"
    assert owned_databases(sync_database) == []


# ------------------------------------------------- what the hermetic suite pins


def test_logging_is_restored(project: Project, sync_database: Database) -> None:
    log = project.path / "run.log"
    alembic_project(project, tests=LOGGING_TESTS)
    project.conftest(LOGGING_CONFTEST)
    result = run(project, sync_database, f"--log-file={log}", "--log-file-level=WARNING")
    result.assert_outcomes(passed=3)
    assert "APP-WARNING" in log.read_text()


VERSION_TABLE_TESTS = """
from sqlalchemy import inspect, text


def test_the_custom_table(sqla_connection):
    assert "alembic_version" not in inspect(sqla_connection).get_table_names()
    assert sqla_connection.scalar(text("SELECT version_num FROM pc_version")) == "0001"
"""


def test_a_custom_version_table(project: Project, sync_database: Database) -> None:
    migrations = alembic_project(project, tests=VERSION_TABLE_TESTS)
    env = migrations / "env.py"
    env.write_text(
        env.read_text().replace(
            "connection=connection, target_metadata=target_metadata",
            'connection=connection, target_metadata=target_metadata, version_table="pc_version"',
        )
    )
    run(project, sync_database).assert_outcomes(passed=1)


BRANCH_TESTS = """
from sqlalchemy import text


def test_heads(sqla_connection):
    rows = sqla_connection.execute(text("SELECT version_num FROM alembic_version"))
    assert sorted(row[0] for row in rows) == ["post2", "pre2"]
"""


@pytest.mark.parametrize("target", ["heads", "pre@head post@head"])
def test_two_heads_with_branch_labels(
    project: Project, sync_database: Database, target: str
) -> None:
    alembic_project(project, revisions=BRANCHES, tests=BRANCH_TESTS)
    project.ini(sqla_alembic_target=target)
    run(project, sync_database).assert_outcomes(passed=1)


def test_relative_paths_from_another_directory(
    project: Project, sync_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    relative_project(
        project,
        script_location="migrations",
        prepend_sys_path="src",
        version_locations="migrations/versions",
        path_separator="os",
    )
    elsewhere = project.path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    ini = project.path / "pytest.ini"
    result = run(
        project, sync_database, f"--rootdir={project.path}", "-c", str(ini), str(project.path)
    )
    result.assert_outcomes(passed=3)


def test_a_pyproject_toml_alone(project: Project, sync_database: Database) -> None:
    alembic_project(project, template="pyproject")
    (project.path / "alembic.ini").unlink()
    project.ini(sqla_alembic_config="pyproject.toml")
    run(project, sync_database).assert_outcomes(passed=3)


# ------------------------------------------------------ a password with %XX

PERCENT_ROLE = "pytest_sqla_percent"
PERCENT_URL = """
import pytest
from sqlalchemy.engine import make_url


@pytest.fixture(scope="session")
def sqla_url():
    # A URL object, so that nothing renders the password before pytest-sqla does.
    return make_url(BASE_URL).set(username=ROLE, password=PASSWORD)
"""


def drop_percent_role(database: Database) -> None:
    drop_databases(database, owned_databases(database))
    run_twin(database, f"DROP ROLE IF EXISTS {PERCENT_ROLE}")


def test_a_password_with_percent_escapes_reaches_env_py_whole(
    project: Project, postgresql_database: Database, run_id: str
) -> None:
    """SQLAlchemy before 2.0.24 writes p%41ss out as p%41ss, which reads back as pAss: there,
    the run refuses before it creates anything. The floor job runs this on 2.0.0."""
    password = "p%41ss"
    drop_percent_role(postgresql_database)
    # text() doubles the % for psycopg's paramstyle.
    run_twin(
        postgresql_database, f"CREATE ROLE {PERCENT_ROLE} LOGIN CREATEDB PASSWORD '{password}'"
    )
    try:
        alembic_project(project)
        project.ini(sqla_url=None, sqla_database="run")
        project.conftest(
            PERCENT_URL.replace("BASE_URL", repr(sync_twin(postgresql_database.url)))
            .replace("ROLE", repr(PERCENT_ROLE))
            .replace("PASSWORD", repr(password))
        )
        result = project.run()
    finally:
        drop_percent_role(postgresql_database)
    if Version(sqlalchemy_version) >= Version("2.0.24"):
        result.assert_outcomes(passed=3)
    else:
        result.assert_outcomes(errors=3)
        result.stdout.fnmatch_lines(
            [
                "*pytest-sqla: sqla_schema = alembic hands env.py the URL as text, in the "
                f"sqlalchemy.url option, and postgresql+psycopg://{PERCENT_ROLE}:***@*/"
                f"{run_database(postgresql_database)} does not read back as the same URL.*"
            ]
        )
    assert password not in result.stdout.str()
    assert owned_databases(postgresql_database) == []
