"""sqla_schema = alembic on in-memory SQLite: the migrations build the schema, once per session.

Every spec runs an inner pytest session on a project that ``alembic init``
wrote. The same behaviours against PostgreSQL and MySQL, where the worker that
creates the database migrates it under the lock, are in
``tests/integration/test_alembic.py``.
"""

from __future__ import annotations

import ast
import sys
import textwrap
from pathlib import Path

import pytest

from tests.alembic_kit import (
    APP_DATABASE_ENV,
    BRANCHES,
    COOKBOOK_ENV,
    FAILING,
    IDLE_ENV,
    MIGRATION,
    alembic_project,
    ini_lines,
)
from tests.helpers import Project, sqlite_rows

pytest.importorskip("alembic")

from alembic import command
from alembic.config import Config
from alembic.util import CommandError


def errors(project: Project, *args: str) -> pytest.RunResult:
    """Every database test errors at setup, as a session fixture that failed does."""
    result = project.run(*args)
    result.assert_outcomes(errors=3)
    return result


# --------------------------------------------------------------- the build


def test_the_migrations_build_the_schema_and_every_test_is_rolled_back(project: Project) -> None:
    """sqla_metadata is ignored, so one ini serves both modes; it names nothing importable."""
    alembic_project(project)
    project.ini(sqla_metadata="nowhere:Base")
    result = project.run("--sqla-recreate-schema")
    result.assert_outcomes(passed=3)
    result.stdout.fnmatch_lines(
        ["sqla: url=sqlite:// (from the sqla_url ini key), schema=alembic, database=configured"]
    )


BOTH_MODES = """
from sqlalchemy import inspect
from models import User, count


def test_commits(sqla_session):
    sqla_session.add(User(email="ada@example.com"))
    sqla_session.commit()
    assert count(sqla_session) == 1


def test_the_commit_was_rolled_back(sqla_session):
    assert count(sqla_session) == 0


def test_who_built_the_schema(sqla_connection):
    names = inspect(sqla_connection).get_table_names()
    print("built by", "alembic" if "alembic_version" in names else "create_all")
"""


def test_one_configuration_serves_both_modes(project: Project) -> None:
    """The ini keeps create_all and its metadata; a CI job adds -o sqla_schema=alembic."""
    alembic_project(project, tests=BOTH_MODES)
    project.ini(sqla_schema="create", sqla_metadata="models:Base")
    created = project.run("-s")
    created.assert_outcomes(passed=3)
    created.stdout.fnmatch_lines(["*built by create_all*"])
    migrated = project.run("-s", "-o", "sqla_schema=alembic")
    migrated.assert_outcomes(passed=3)
    migrated.stdout.fnmatch_lines(["*built by alembic*"])


COOKBOOK_RECORDS = """
from pathlib import Path

from sqlalchemy import make_url

HANDED = Path(HANDED_PATH)
connection = context.config.attributes.get("connection")
url = make_url(context.config.get_main_option("sqlalchemy.url"))
with HANDED.open("a") as handed:
    seen = (type(connection).__name__, connection.in_transaction(), url.database, dict(url.query))
    handed.write(repr(seen) + "\\n")
"""


def test_the_cookbook_env_py_migrates_on_the_idle_connection_it_is_handed(
    project: Project,
) -> None:
    """Idle: no transaction begun, so Alembic manages its own, as on its command line. The URL
    is handed over too, with every % doubled for the config parser, and reads back whole."""
    handed = project.path / "handed.txt"
    env = COOKBOOK_ENV.replace(
        "run_migrations_online()\n",
        COOKBOOK_RECORDS.replace("HANDED_PATH", repr(str(handed))) + "run_migrations_online()\n",
    )
    alembic_project(project, env=env)
    result = project.run()
    result.assert_outcomes(passed=3)
    # Once for the upgrade, once for the check that reads the heads back.
    seen = [ast.literal_eval(line) for line in handed.read_text().splitlines()]
    assert len(seen) == 2, seen
    for kind, in_transaction, database, query in seen:
        assert (kind, in_transaction) == ("Connection", False)
        assert database.startswith("file:/pytest-sqla-"), database
        assert query == {"uri": "true", "vfs": "memdb"}


def test_two_heads_with_branch_labels_are_both_reached(project: Project) -> None:
    alembic_project(
        project,
        revisions=BRANCHES,
        tests="""
        from sqlalchemy import text

        def test_heads(sqla_connection):
            rows = sqla_connection.execute(text("SELECT version_num FROM alembic_version"))
            assert sorted(row[0] for row in rows) == ["post2", "pre2"]
        """,
    )
    project.run().assert_outcomes(passed=1)


@pytest.mark.parametrize(
    ("target", "heads", "tables"),
    [
        (
            "pre@head post@head",
            ["post2", "pre2"],
            ["t_post1", "t_post2", "t_pre1", "t_pre2", "t_r0"],
        ),
        ("post@head", ["post2"], ["t_post1", "t_post2", "t_pre1", "t_r0"]),
        ("pre1", ["pre1"], ["t_pre1", "t_r0"]),
    ],
    ids=["two branches", "one branch and its dependency", "a revision"],
)
def test_sqla_alembic_target_names_what_to_upgrade_to(
    project: Project, target: str, heads: list[str], tables: list[str]
) -> None:
    """Split on whitespace, one upgrade each: "pre@head post@head" as one revision is not
    found, measured."""
    alembic_project(
        project,
        revisions=BRANCHES,
        tests=f"""
        from sqlalchemy import inspect, text

        def test_reached(sqla_connection):
            rows = sqla_connection.execute(text("SELECT version_num FROM alembic_version"))
            assert sorted(row[0] for row in rows) == {heads!r}
            names = inspect(sqla_connection).get_table_names()
            assert sorted(name for name in names if name.startswith("t_")) == {tables!r}
        """,
    )
    project.ini(sqla_alembic_target=target)
    project.run().assert_outcomes(passed=1)


def test_a_custom_version_table_is_read_back_through_env_py(project: Project) -> None:
    """The check reads the heads the way alembic current does, through env.py."""
    migrations = alembic_project(
        project,
        tests="""
        from sqlalchemy import inspect, text

        def test_the_custom_table(sqla_connection):
            assert "alembic_version" not in inspect(sqla_connection).get_table_names()
            assert sqla_connection.scalar(text("SELECT version_num FROM pc_version")) == "0001"
        """,
    )
    env = migrations / "env.py"
    env.write_text(
        env.read_text().replace(
            "connection=connection, target_metadata=target_metadata",
            'connection=connection, target_metadata=target_metadata, version_table="pc_version"',
        )
    )
    assert 'version_table="pc_version"' in env.read_text()
    project.run().assert_outcomes(passed=1)


def test_a_failing_migration_fails_every_database_test_with_its_own_error(
    project: Project,
) -> None:
    """Raised as the migration raised it, so its traceback shows the migration's own line."""
    alembic_project(project, revisions=(MIGRATION, FAILING))
    result = errors(project)
    result.stdout.fnmatch_lines(["E * RuntimeError: migration 0002 fails on purpose"])
    result.stdout.fnmatch_lines(["migrations/versions/0001.py:*: RuntimeError"])


# ------------------------------------------------------------------- the check


def test_an_env_py_that_never_runs_the_migrations_fails_the_check(project: Project) -> None:
    alembic_project(project, env=IDLE_ENV)
    result = errors(project)
    result.stdout.fnmatch_lines(
        [
            "*SchemaConflictError: pytest-sqla: after the upgrade to heads, env.py records no "
            "revision, which leaves out 0001. env.py did not run the migrations on the database "
            "pytest-sqla created, or recorded them where its check cannot see them. Make env.py "
            'run context.run_migrations() on config.attributes[[]"connection"], or on an engine '
            "built from the sqlalchemy.url option."
        ]
    )


@pytest.mark.parametrize(
    ("target", "alembic_says"),
    [
        ("+1", "Can't locate revision identified by '+1'"),
        ("0001 nope", "Can't locate revision identified by 'nope'"),
        ("head", "Multiple head revisions are present for given argument 'head'*"),
    ],
    ids=["relative", "a typo", "head with two heads"],
)
def test_a_target_alembic_cannot_resolve_is_refused_before_anything_runs(
    project: Project, target: str, alembic_says: str
) -> None:
    alembic_project(project, revisions=BRANCHES if target == "head" else (MIGRATION,))
    project.ini(sqla_alembic_target=target)
    result = errors(project)
    name = target.split()[-1]
    result.stdout.fnmatch_lines(
        [
            f"*SchemaConflictError: pytest-sqla: sqla_alembic_target names {name!r}, which "
            f"Alembic cannot resolve: {alembic_says}. Name heads*"
        ]
    )
    result.stdout.fnmatch_lines(
        [
            "*Name heads, a revision, or a branch such as name@head; pytest-sqla checks "
            "afterwards that the database reached each target, which it cannot do for a "
            "relative one such as +1."
        ]
    )


def test_a_script_directory_without_revisions_is_refused(project: Project) -> None:
    """A wrong version_locations migrates nothing at all, silently, on Alembic's command line."""
    alembic_project(project)
    ini_lines(project.path / "alembic.ini", version_locations="%(here)s/nowhere")
    result = errors(project)
    result.stdout.fnmatch_lines(
        [
            f"*SchemaConflictError: pytest-sqla: Alembic found no revisions in "
            f"{project.path / 'nowhere'}, so the upgrade would build nothing. Check "
            f"script_location and version_locations in {project.path / 'alembic.ini'}: "
            "pytest-sqla reads relative paths from the directory of that file, as %(here)s "
            "would."
        ]
    )


def test_a_configuration_without_script_location_is_refused(project: Project) -> None:
    alembic_project(project)
    ini_lines(project.path / "alembic.ini", script_location=None)
    result = errors(project)
    result.stdout.fnmatch_lines(
        [
            f"*SchemaConflictError: pytest-sqla: {project.path / 'alembic.ini'} sets no "
            "script_location, the directory of your migrations. Point sqla_alembic_config at "
            "the alembic.ini or pyproject.toml that sets it."
        ]
    )


def test_a_missing_configuration_file_is_named(project: Project) -> None:
    alembic_project(project)
    project.ini(sqla_alembic_config="config/alembic.ini")
    result = errors(project)
    result.stdout.fnmatch_lines(
        [
            f"*pytest-sqla: sqla_schema = alembic reads the Alembic configuration from "
            f"{project.path / 'config' / 'alembic.ini'}, which does not exist. Set "
            "sqla_alembic_config to your alembic.ini, or to a pyproject.toml with a "
            "[[]tool.alembic] table, relative to the rootdir; or override the "
            "sqla_alembic_config fixture."
        ]
    )


# ------------------------------------------------------------------ the config


def test_a_pyproject_toml_next_to_alembic_ini_is_read_too(project: Project) -> None:
    """alembic init -t pyproject leaves script_location to [tool.alembic]. The control:
    Alembic's own Config of the ini alone cannot find it."""
    alembic_project(project, template="pyproject")
    assert "script_location" not in (project.path / "alembic.ini").read_text()
    with pytest.raises(CommandError, match="No 'script_location' key found"):
        command.upgrade(Config(str(project.path / "alembic.ini")), "heads")
    project.run().assert_outcomes(passed=3)


def test_a_pyproject_toml_alone_is_a_whole_configuration(project: Project) -> None:
    """sqla_alembic_config names it; without an ini, env.py runs no fileConfig()."""
    alembic_project(project, template="pyproject")
    (project.path / "alembic.ini").unlink()
    project.ini(sqla_alembic_config="pyproject.toml")
    project.run().assert_outcomes(passed=3)


def test_an_overridden_config_without_a_file_resolves_against_the_rootdir(
    project: Project,
) -> None:
    alembic_project(project)
    project.conftest(
        """
        import pytest
        from alembic.config import Config

        @pytest.fixture(scope="session")
        def sqla_alembic_config():
            config = Config()
            config.set_main_option("script_location", "migrations")
            return config
        """
    )
    project.run().assert_outcomes(passed=3)


# ------------------------------------------------------------------- paths


PATH_TESTS = """
import sys
from pathlib import Path

from models import User, count


def test_commits(sqla_session):
    sqla_session.add(User(email="ada@example.com"))
    sqla_session.commit()
    assert count(sqla_session) == 1


def test_the_commit_was_rolled_back(sqla_session):
    assert count(sqla_session) == 0


def test_sys_path_is_as_it_was(sqla_session):
    import appmodels  # imported by env.py, through prepend_sys_path

    assert str(Path(appmodels.__file__).parent) not in sys.path
"""


def relative_project(project: Project, **paths: str) -> Path:
    """The layout ``alembic init`` wrote before 1.16, every path relative, and env.py importing
    a module only prepend_sys_path reaches."""
    migrations = alembic_project(project, tests=PATH_TESTS)
    (project.path / "src").mkdir()
    (project.path / "src" / "appmodels.py").write_text("from sqlalchemy import MetaData\n")
    env = migrations / "env.py"
    env.write_text("import appmodels  # noqa: F401\n" + env.read_text())
    ini_lines(project.path / "alembic.ini", **paths)
    return migrations


def test_relative_paths_are_read_from_the_config_files_directory(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """From another directory: Alembic reads them from the working directory, the control."""
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
    path = list(sys.path)
    try:
        with pytest.raises(CommandError, match="Path doesn't exist: migrations"):
            command.upgrade(Config(str(project.path / "alembic.ini")), "heads")
    finally:
        sys.path[:] = path  # Alembic prepended prepend_sys_path, relative, before it failed
    ini = project.path / "pytest.ini"
    result = project.run(f"--rootdir={project.path}", "-c", str(ini), str(project.path))
    result.assert_outcomes(passed=3)


def test_a_package_resource_is_left_as_it_is(project: Project) -> None:
    """script_location = package:directory, which Alembic itself looks up as a resource."""
    migrations = relative_project(project, prepend_sys_path="src", path_separator="os")
    migrations.rename(project.path / "src" / "appmigrations")
    (project.path / "src" / "appmigrations" / "__init__.py").write_text("")
    ini_lines(project.path / "alembic.ini", script_location="appmigrations:")
    project.run().assert_outcomes(passed=3)


def test_a_legacy_ini_gets_alembics_own_warning(project: Project) -> None:
    """No path_separator: Alembic 1.16+ warns, and its text says what to add. The inner run
    has filterwarnings = error, as the suites this plugin is for do."""
    alembic_project(project)
    ini_lines(project.path / "alembic.ini", path_separator=None)
    result = errors(project)
    result.stdout.fnmatch_lines(
        [
            "*DeprecationWarning: No path_separator found in configuration; falling back to "
            "legacy splitting on spaces, commas, and colons for prepend_sys_path.  Consider "
            "adding path_separator=os to Alembic config."
        ]
    )


# ----------------------------------------------------------------- logging


LOGGING_CONFTEST = """
import logging

APP = logging.getLogger("app")  # an application logger, created at import as they are
APP.setLevel(logging.INFO)


class Tagged(logging.Filter):
    def filter(self, record):
        record.tag = "tagged"
        return True


TAGGED = Tagged()
APP.addFilter(TAGGED)


def pytest_unconfigure(config):
    # The logging registry outlives an in-process run: leave "app" as it was found.
    APP.removeFilter(TAGGED)
    APP.setLevel(logging.NOTSET)
"""

LOGGING_TESTS = """
import logging

from conftest import APP, TAGGED


def test_migrated(sqla_session):
    pass


def test_caplog_sees_the_application(sqla_session, caplog):
    APP.info("APP-INFO")
    APP.warning("APP-WARNING")
    assert [record.getMessage() for record in caplog.records] == ["APP-INFO", "APP-WARNING"]
    assert caplog.records[0].tag == "tagged"


def test_the_loggers_are_as_they_were(sqla_session):
    assert not APP.disabled
    assert APP.level == logging.INFO
    assert APP.filters == [TAGGED]
    assert not logging.getLogger("sqlalchemy").disabled
    # fileConfig() put a StreamHandler on stderr on the root; it is gone again.
    assert not [h for h in logging.getLogger().handlers if type(h) is logging.StreamHandler]
"""


@pytest.mark.parametrize("template", ["generic", "pyproject"], ids=["fileConfig", "no fileConfig"])
def test_logging_is_restored_after_env_pys_fileconfig(project: Project, template: str) -> None:
    """fileConfig() closes pytest's handlers, disables every logger it does not name and
    replaces the root's: measured, caplog then stays empty and --log-file gets nothing."""
    log = project.path / "run.log"
    alembic_project(project, template=template, tests=LOGGING_TESTS)
    if template == "pyproject":
        (project.path / "alembic.ini").unlink()
        project.ini(sqla_alembic_config="pyproject.toml")
    project.conftest(LOGGING_CONFTEST)
    result = project.run(f"--log-file={log}", "--log-file-level=WARNING")
    result.assert_outcomes(passed=3)
    assert "APP-WARNING" in log.read_text()


# ------------------------------------------------------------------- the guard


def test_an_env_py_that_reaches_another_database_is_refused(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused in do_connect, before any connection exists: the file is never even created."""
    app = project.path / "app.db"
    monkeypatch.setenv("APP_DATABASE_URL", f"sqlite:///{app}")
    alembic_project(project, env=APP_DATABASE_ENV)
    result = errors(project)
    result.stdout.fnmatch_lines(
        [
            f"*SchemaConflictError: pytest-sqla: env.py or a migration opened a sqlite+pysqlite "
            f"connection to {str(app)!r}, and sqla_schema = alembic migrates "
            "sqlite:///file*/pytest-sqla-*?uri=true&vfs=memdb only, so pytest-sqla refused it "
            'before it was used. Make env.py use config.attributes[[]"connection"], or build its '
            "engine from the sqlalchemy.url option, which pytest-sqla sets to that database."
        ]
    )
    assert not app.exists()


CREATOR_ENV = """
import sqlite3

from alembic import context
from sqlalchemy import create_engine, pool

engine = create_engine(
    "sqlite://", creator=lambda: sqlite3.connect(OTHER_PATH), poolclass=pool.NullPool
)
with engine.connect() as connection:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()
"""

APP_DO_CONNECT_HOOK = """
import sqlite3

from sqlalchemy import event
from sqlalchemy.engine import Engine


def application_connects_itself(dialect, record, cargs, cparams):
    return sqlite3.connect(OTHER_PATH)


# Registered before pytest-sqla's guard, and removed again: SQLAlchemy's class-level
# listeners outlive the inner run, which only restores sys.modules.
def pytest_configure(config):
    event.listen(Engine, "do_connect", application_connects_itself)


def pytest_unconfigure(config):
    event.remove(Engine, "do_connect", application_connects_itself)
"""


@pytest.mark.parametrize("how", ["creator", "do_connect hook"])
def test_a_connection_sqlalchemy_did_not_make_from_its_arguments_is_refused(
    project: Project, how: str
) -> None:
    """An engine with creator=, or an application's do_connect hook registered before the
    guard: no do_connect of the guard's runs, so its connect listener refuses the record."""
    other = repr(str(project.path / "other.db"))
    alembic_project(project, env=CREATOR_ENV.replace("OTHER_PATH", other))
    if how == "do_connect hook":
        alembic_project_env(project, COOKBOOK_ENV)
        project.conftest(APP_DO_CONNECT_HOOK.replace("OTHER_PATH", other))
    result = errors(project)
    result.stdout.fnmatch_lines(
        [
            "*SchemaConflictError: pytest-sqla: refused a connection of the migration that "
            "SQLAlchemy did not make from its connect arguments: an engine made with creator=, "
            "or a do_connect hook that returns a connection of its own, such as an "
            "application's. pytest-sqla could not tell which database it reaches, and "
            "sqla_schema = alembic migrates sqlite:///file*/pytest-sqla-*?uri=true&vfs=memdb "
            'only. Make env.py use config.attributes[[]"connection"]*'
        ]
    )
    assert sqlite_rows(project.path / "other.db", "SELECT name FROM sqlite_master") == [], (
        "nothing ran there"
    )


def alembic_project_env(project: Project, env: str) -> None:
    (project.path / "migrations" / "env.py").write_text(textwrap.dedent(env).lstrip())


REWRITING_ENV = """
from alembic import context
from sqlalchemy import engine_from_config, event, pool

config = context.config
engine = engine_from_config(
    config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool
)


@event.listens_for(engine, "do_connect")
def elsewhere(dialect, record, cargs, cparams):
    # SQLAlchemy lets a hook change the arguments in place, and this one runs after the guard's.
    cargs[0] = OTHER_PATH


with engine.connect() as connection:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
"""


def test_a_do_connect_hook_that_points_env_pys_engine_elsewhere_is_refused(
    project: Project,
) -> None:
    """The guard's do_connect approved the arguments, which the hook changed afterwards; its
    connect listener reads them again, and refuses."""
    other = project.path / "other.db"
    alembic_project(project, env=REWRITING_ENV.replace("OTHER_PATH", repr(str(other))))
    result = errors(project)
    result.stdout.fnmatch_lines(
        [
            f"*SchemaConflictError: pytest-sqla: env.py or a migration opened a sqlite+pysqlite "
            f"connection to {str(other)!r}, and sqla_schema = alembic migrates "
            "sqlite:///file*/pytest-sqla-*?uri=true&vfs=memdb only, so pytest-sqla refused it "
            "before it was used.*"
        ]
    )
    assert sqlite_rows(other, "SELECT name FROM sqlite_master") == [], "nothing ran there"


ANOTHER_THREAD = """
import threading

from sqlalchemy import create_engine, pool

def connect_elsewhere():
    engine = create_engine("sqlite:///" + ELSEWHERE_PATH, poolclass=pool.NullPool)
    with engine.connect():
        pass
    engine.dispose()

thread = threading.Thread(target=connect_elsewhere)
thread.start()
thread.join()
"""


def test_connections_other_threads_open_meanwhile_pass(project: Project) -> None:
    elsewhere = project.path / "elsewhere.db"
    migrations = alembic_project(project)
    env = migrations / "env.py"
    env.write_text(ANOTHER_THREAD.replace("ELSEWHERE_PATH", repr(str(elsewhere))) + env.read_text())
    project.run().assert_outcomes(passed=3)
    assert elsewhere.exists(), "the other thread connected"


# ---------------------------------------------------------- the URL handed over


def test_a_url_that_does_not_read_back_whole_is_refused_before_anything_connects(
    project: Project,
) -> None:
    """A password without a user is not written out at all. SQLAlchemy before 2.0.24 changes
    %XX in a password the same way; the floor job meets that one."""
    alembic_project(project)
    project.ini(sqla_url=None, sqla_database="run")
    project.conftest(
        """
        import pytest
        from sqlalchemy.engine import URL

        @pytest.fixture(scope="session")
        def sqla_url():
            return URL.create(
                "postgresql+psycopg", password="s3cret", host="db.example", database="app"
            )
        """
    )
    result = errors(project)
    result.stdout.fnmatch_lines(
        [
            "*pytest-sqla: sqla_schema = alembic hands env.py the URL as text, in the "
            "sqlalchemy.url option, and postgresql+psycopg://db.example/app_* does not read "
            "back as the same URL, so env.py would connect with another one. SQLAlchemy writes "
            "out no password without a user name, and before 2.0.24 it changes %XX in a "
            "password: give the URL a user name, use a password without %, or upgrade "
            "SQLAlchemy to 2.0.24 or newer."
        ]
    )
    assert "s3cret" not in result.stdout.str()


@pytest.mark.parametrize(
    ("url", "refusal"),
    [
        (
            "sqlite:///{path}/app.db",
            "migrates an in-memory SQLite database, such as sqla_url = sqlite://, and "
            "sqlite:///*/app.db is a file.",
        ),
        (
            "postgresql+psycopg://ada:s3cret@db.example/app",
            "migrates a database pytest-sqla creates and drops, and sqla_database = configured "
            "uses postgresql+psycopg://ada:***@db.example/app as it is.",
        ),
    ],
    ids=["a SQLite file", "configured"],
)
def test_an_overridden_sqla_url_is_checked_like_a_configured_one(
    project: Project, url: str, refusal: str
) -> None:
    alembic_project(project)
    project.ini(sqla_url=None)
    project.conftest(
        f"""
        import pytest

        @pytest.fixture(scope="session")
        def sqla_url():
            return {url.format(path=project.path)!r}
        """
    )
    result = errors(project)
    result.stdout.fnmatch_lines([f"*pytest-sqla: sqla_schema = alembic {refusal}*"])
    assert "s3cret" not in result.stdout.str()
    assert not (project.path / "app.db").exists()


# -------------------------------------------------------------- inert, xdist


def test_a_run_that_never_uses_the_database_never_imports_alembic(project: Project) -> None:
    """A subprocess, because the outer process imported alembic long ago."""
    alembic_project(project, tests="def test_unrelated():\n    pass\n")
    project.conftest(
        """
        import sys

        def pytest_sessionfinish(session):
            print("alembic imported:", "alembic" in sys.modules)
        """
    )
    result = project.run_subprocess("-s")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*alembic imported: False"])


def test_each_xdist_worker_migrates_its_own_in_memory_database(project: Project) -> None:
    pytest.importorskip("xdist")
    alembic_project(
        project,
        tests="""
        import pytest
        from models import User, count

        @pytest.mark.parametrize("n", range(6))
        def test_writer(sqla_session, n):
            assert count(sqla_session) == 0
            sqla_session.add(User(email=f"user{n}@example.com"))
            sqla_session.commit()
            assert count(sqla_session) == 1
        """,
    )
    project.run_subprocess("-n", "2", timeout=120).assert_outcomes(passed=6)


def test_alembic_init_writes_the_layout_these_specs_assume(project: Project) -> None:
    """%(here)s/migrations, a relative prepend_sys_path and path_separator = os: what a user
    who ran alembic init on 1.16 or newer has."""
    alembic_project(project)
    lines = (project.path / "alembic.ini").read_text().splitlines()
    for line in (
        "script_location = %(here)s/migrations",
        "prepend_sys_path = .",
        "path_separator = os",
    ):
        assert line in lines, line
