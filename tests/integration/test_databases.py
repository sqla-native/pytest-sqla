"""sqla_database = run and worker against real PostgreSQL 17 and MySQL 8.4 servers.

Each spec runs inner pytest sessions, then looks at the server from outside:
which databases exist and what their marks say. The ones that need several
processes - pytest-xdist workers, two runs at once, a worker killed with
SIGKILL - run in subprocesses; the rest run in-process, so that coverage sees
every PostgreSQL and MySQL branch. A fixed run id (``PYTEST_XDIST_TESTRUNUID``,
the variable xdist sets) makes the derived name known in advance.

On MySQL the ``sqla`` user may create the databases named after its own,
``sqla_...``, and ``sqla_nocreate`` may not: ``scripts/mysql-init.sql``, which
compose.yaml mounts and CI runs.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from typing import TypeVar

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from pytest_sqla._config import Owned
from pytest_sqla._database import (
    Mark,
    _PostgreSQL,
    comment,
    lock_key,
    lock_name,
    mark_table,
    parse_comment,
)
from tests.helpers import Project, table_names
from tests.integration.conftest import Database, sync_twin
from tests.integration.test_real_databases import inner
from tests.integration.test_xdist_backends import ASYNC_WRITERS, WRITERS

pytest.importorskip("xdist")

RUN_ID = "0123456789abcdef0123456789abcdef"
ANOTHER_RUN = Mark("f" * 32, 1, "2026-11-01T09:00:00+00:00", "elsewhere:4242/gw1")
DATABASE_FIXTURES = (
    "sync_database",
    "async_database",
    "postgresql_database",
    "mysql_database",
)
_T = TypeVar("_T")


# ------------------------------------------------------------------ helpers


def configured(database: Database) -> str:
    """The configured database: what pytest-sqla names its own after."""
    return str(make_url(database.url).database)


def run_database(database: Database) -> str:
    """The name ``sqla_database = run`` derives for RUN_ID."""
    return f"{configured(database)}_{RUN_ID[:8]}"


def with_database(database: Database, name: str) -> Database:
    url = make_url(database.url).set(database=name)
    return Database(database.backend, url.render_as_string(hide_password=False))


def on_server(
    database: Database, work: Callable[[Connection], _T], *, name: str | None = None
) -> _T:
    """``work`` on an AUTOCOMMIT connection through the sync twin, to ``name`` or the
    configured database: CREATE and DROP DATABASE run in no transaction block."""
    url = make_url(sync_twin(database.url))
    if name is not None:
        url = url.set(database=name)
    engine = create_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            return work(connection)
    finally:
        engine.dispose()


def quoted(connection: Connection, name: str) -> str:
    return connection.dialect.identifier_preparer.quote_identifier(name)


def owned_databases(database: Database) -> list[str]:
    """The databases on the server named like pytest-sqla's own: ``sqla_...``."""
    query = (
        "SELECT datname FROM pg_database WHERE datname LIKE :pattern"
        if database.family == "postgresql"
        else "SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE :pattern"
    )
    pattern = configured(database) + "\\_%"
    return on_server(database, lambda c: sorted(c.scalars(text(query), {"pattern": pattern})))


def drop_databases(database: Database, names: list[str]) -> None:
    def drop(connection: Connection) -> None:
        if database.family == "mysql":
            # A leaked transaction must not hang the clean-up.
            connection.exec_driver_sql("SET SESSION lock_wait_timeout = 10")
        force = " WITH (FORCE)" if database.family == "postgresql" else ""
        for name in names:
            connection.exec_driver_sql(f"DROP DATABASE IF EXISTS {quoted(connection, name)}{force}")

    on_server(database, drop)


def write_mark(connection: Connection, name: str, mark: Mark) -> None:
    """What pytest-sqla writes: the comment on PostgreSQL, a table inside on MySQL."""
    if connection.dialect.name == "postgresql":
        connection.exec_driver_sql(
            f"COMMENT ON DATABASE {quoted(connection, name)} IS '{comment(mark)}'"
        )
        return
    table = mark_table(name)
    table.create(connection, checkfirst=True)
    connection.execute(table.delete())
    connection.execute(
        table.insert().values(
            run_id=mark.run_id, workers=mark.workers, started_at=mark.started_at, owner=mark.owner
        )
    )


def read_mark(database: Database, name: str) -> Mark | None:
    def read(connection: Connection) -> Mark | None:
        if connection.dialect.name == "postgresql":
            value = connection.scalar(
                text(
                    "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
                    "WHERE datname = :name"
                ),
                {"name": name},
            )
            return parse_comment(value)
        row = connection.execute(select(mark_table(name))).first()
        return None if row is None else Mark(*row)

    return on_server(database, read)


def create_database(database: Database, name: str, mark: Mark | None = None) -> None:
    def create(connection: Connection) -> None:
        connection.exec_driver_sql(f"CREATE DATABASE {quoted(connection, name)}")
        if mark is not None:
            write_mark(connection, name, mark)

    on_server(database, create)


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
    monkeypatch.setenv("PYTEST_XDIST_TESTRUNUID", RUN_ID)
    return RUN_ID


RUN_TESTS = """
from sqlalchemy import make_url, text
from models import User, count


def current(connection):
    postgresql = connection.dialect.name == "postgresql"
    sql = "SELECT current_database()" if postgresql else "SELECT DATABASE()"
    return connection.scalar(text(sql))


def test_commits(sqla_session):
    sqla_session.add(User(email="ada@example.com"))
    sqla_session.commit()
    assert count(sqla_session) == 1


def test_sees_nothing(sqla_session):
    assert count(sqla_session) == 0


def test_its_own_database(sqla_connection, sqla_url, sqla_database_url):
    assert current(sqla_connection) == sqla_database_url.database
    assert sqla_database_url.database != make_url(sqla_url).database
    assert sqla_connection.engine.url == sqla_database_url
"""


# ---------------------------------------------------- one database per run


def test_a_run_gets_a_database_of_its_own_and_drops_it(
    project: Project, sync_database: Database, run_id: str
) -> None:
    project.ini(sqla_database="run")
    result = inner(project, sync_database, RUN_TESTS)
    result.assert_outcomes(passed=3)
    result.stdout.fnmatch_lines(
        [f"sqla: url=*, schema=create, database=run ({run_database(sync_database)})"]
    )
    assert owned_databases(sync_database) == []
    assert sync_database.tables() == [], "the configured database is never touched"


def test_an_overridden_sqla_url_is_the_base_of_the_name(
    project: Project, sync_database: Database
) -> None:
    """A container's or a server fixture's URL, as the README's testcontainers recipe."""
    project.ini(sqla_database="run")
    project.models()
    project.conftest(
        f"""
        import pytest

        @pytest.fixture(scope="session")
        def sqla_url():
            return {sync_database.url!r}
        """
    )
    project.tests(RUN_TESTS)
    project.run().assert_outcomes(passed=3)
    assert owned_databases(sync_database) == []


ASYNC_RUN_TESTS = """
import pytest
from sqlalchemy import text
from models import User, acount

pytestmark = MARKER


async def test_commits(sqla_async_session):
    sqla_async_session.add(User(email="ada@example.com"))
    await sqla_async_session.commit()
    assert await acount(sqla_async_session) == 1


async def test_sees_nothing(sqla_async_session):
    assert await acount(sqla_async_session) == 0


async def test_its_own_database(sqla_async_connection, sqla_async_database_url):
    postgresql = sqla_async_connection.dialect.name == "postgresql"
    sql = "SELECT current_database()" if postgresql else "SELECT DATABASE()"
    assert await sqla_async_connection.scalar(text(sql)) == sqla_async_database_url.database
"""


@pytest.mark.parametrize(
    "marker",
    ["pytest.mark.asyncio", 'pytest.mark.asyncio(loop_scope="session")'],
    ids=["function loop", "session loop"],
)
def test_async_urls_get_a_database_of_their_own_too(
    project: Project, async_database: Database, marker: str
) -> None:
    """asyncpg, aiomysql and asyncmy URLs create and drop it on a thread of their own. On a
    session loop the pooled engine lives until the session ends, and is disposed first."""
    project.ini(sqla_database="run")
    result = inner(project, async_database, ASYNC_RUN_TESTS.replace("MARKER", marker))
    result.assert_outcomes(passed=3)
    assert owned_databases(async_database) == []


def test_a_sibling_joins_and_only_the_last_one_out_drops(
    project: Project, sync_database: Database, run_id: str
) -> None:
    """Without xdist: the mark a sibling worker of this run would have written."""
    name = run_database(sync_database)
    create_database(sync_database, name, Mark(RUN_ID, 1, "2026-11-20T09:00:00+00:00", "ci:1/gw0"))
    project.ini(sqla_database="run")
    inner(project, sync_database, RUN_TESTS).assert_outcomes(passed=3)
    mark = read_mark(sync_database, name)
    assert mark is not None
    assert mark.workers == 1, "this process left; the sibling still counts"
    assert owned_databases(sync_database) == [name]


# ----------------------------------------------------------- under pytest-xdist

LOG_STATEMENTS = """
import os
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine

LOG = Path(LOG_PATH)


@event.listens_for(Engine, "before_cursor_execute")
def log_database_statements(conn, cursor, statement, parameters, context, executemany):
    if statement.startswith(("CREATE DATABASE", "DROP DATABASE")):
        with LOG.open("a") as log:
            log.write(f"{os.getpid()} {statement}\\n")
"""

LATE_WORKER = """
import time
from pathlib import Path

FLAGS = Path(FLAGS_PATH)


def wait_for(flag):
    deadline = time.monotonic() + 60
    while not (FLAGS / flag).exists():
        assert time.monotonic() < deadline, f"no {flag} within 60 s: the tests share a worker"
        time.sleep(0.05)


def test_early(sqla_session):
    (FLAGS / "early").touch()
    wait_for("late")


def test_late(request):
    wait_for("early")
    request.getfixturevalue("sqla_session")
    (FLAGS / "late").touch()
"""


def test_a_run_creates_its_database_once_and_drops_it_once(
    project: Project, sync_database: Database
) -> None:
    """Two workers; the second asks for the database only once the first is inside, and the
    first stays until the second has joined. One CREATE DATABASE and one DROP DATABASE."""
    log = project.path / "statements.log"
    flags = project.path / "flags"
    flags.mkdir()
    project.ini(sqla_database="run")
    project.models()
    project.conftest(LOG_STATEMENTS.replace("LOG_PATH", repr(str(log))))
    project.tests(LATE_WORKER.replace("FLAGS_PATH", repr(str(flags))))
    result = project.run_subprocess("-n", "2", "--sqla-url", sync_database.url, timeout=300)
    result.assert_outcomes(passed=2)
    statements = [line.split(" ", 1)[1] for line in log.read_text().splitlines()]
    assert [statement.split(" ")[:2] for statement in statements] == [
        ["CREATE", "DATABASE"],
        ["DROP", "DATABASE"],
    ], statements
    assert owned_databases(sync_database) == []


def test_sync_workers_share_one_run_database(project: Project, sync_database: Database) -> None:
    project.ini(sqla_database="run")
    project.models()
    project.tests(WRITERS)
    result = project.run_subprocess("-n", "3", "--sqla-url", sync_database.url, timeout=300)
    result.assert_outcomes(passed=12)
    assert owned_databases(sync_database) == []


def test_async_workers_share_one_run_database(project: Project, async_database: Database) -> None:
    project.ini(sqla_database="run")
    project.models()
    project.tests(ASYNC_WRITERS)
    result = project.run_subprocess("-n", "3", "--sqla-url", async_database.url, timeout=300)
    result.assert_outcomes(passed=12)
    assert owned_databases(async_database) == []


WORKER_DATABASES = """
import pytest
from pathlib import Path
from models import User, count

NAMES = Path(NAMES_PATH)


@pytest.mark.parametrize("n", range(6))
def test_writer(sqla_session, sqla_database_url, worker_id, n):
    (NAMES / f"{worker_id}-{n}").write_text(sqla_database_url.database)
    sqla_session.add(User(email=f"user{n}@example.com"))
    sqla_session.commit()
    assert count(sqla_session) == 1
"""


def test_worker_mode_gives_each_worker_a_database_of_its_own(
    project: Project, sync_database: Database
) -> None:
    names = project.path / "names"
    names.mkdir()
    project.ini(sqla_database="worker")
    project.models()
    project.tests(WORKER_DATABASES.replace("NAMES_PATH", repr(str(names))))
    result = project.run_subprocess("-n", "3", "--sqla-url", sync_database.url, timeout=300)
    result.assert_outcomes(passed=6)
    used = {path.name.split("-")[0]: path.read_text() for path in names.iterdir()}
    assert len(used) >= 2, used
    assert len(set(used.values())) == len(used), "one database per worker"
    for worker, name in used.items():
        assert re.fullmatch(rf"{configured(sync_database)}_[0-9a-f]{{8}}_{worker}", name), name
    assert owned_databases(sync_database) == []


SHARED_BY_HAND = """
import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

ADMIN = ADMIN_URL
SHARED = SHARED_NAME


def drop_and_create(create=True):
    engine = create_engine(ADMIN, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{SHARED}" WITH (FORCE)')
        if create:
            connection.exec_driver_sql(f'CREATE DATABASE "{SHARED}"')
    engine.dispose()


@pytest.fixture(scope="session")
def sqla_url():
    '''The usual hand-made test database: drop it, create it, and drop it at the end.'''
    drop_and_create()
    yield make_url(ADMIN).set(database=SHARED)
    drop_and_create(create=False)
"""

EVERY_WORKER_INSIDE = """
import time
from pathlib import Path

import pytest
from models import User, count

INSIDE = Path(INSIDE_PATH)


@pytest.mark.parametrize("n", range(4))
def test_writer(sqla_session, worker_id, n):
    sqla_session.add(User(email=f"user{n}@example.com"))
    sqla_session.commit()
    (INSIDE / worker_id).touch()
    deadline = time.monotonic() + 20
    while len(list(INSIDE.iterdir())) < 4 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert count(sqla_session) == 1
"""


def test_a_database_dropped_and_created_by_every_worker_breaks_and_run_does_not(
    project: Project, postgresql_database: Database
) -> None:
    """The control, the pattern several adopters use: every worker drops and creates one
    test database at its start. Each worker's tests wait until all four are inside, so the
    lifetimes overlap as they do in any real run; a later worker's DROP then ends an earlier
    one's connections and tables. The same tests with sqla_database = run all pass.

    PostgreSQL only: MySQL's DROP DATABASE has no FORCE and would wait for the earlier
    worker's transaction instead of ending it."""
    inside = project.path / "inside"
    inside.mkdir()
    project.models()
    project.ini(sqla_lock_timeout="5")
    project.tests(EVERY_WORKER_INSIDE.replace("INSIDE_PATH", repr(str(inside))))
    shared = f"{configured(postgresql_database)}_shared_by_every_worker"
    project.conftest(
        SHARED_BY_HAND.replace("ADMIN_URL", repr(sync_twin(postgresql_database.url))).replace(
            "SHARED_NAME", repr(shared)
        )
    )
    by_hand = project.run_subprocess("-n", "4", timeout=300)
    assert by_hand.parseoutcomes().get("passed", 0) < 4, by_hand.stdout.str()

    drop_databases(postgresql_database, [shared])
    (project.path / "conftest.py").unlink()
    for path in inside.iterdir():
        path.unlink()
    project.ini(sqla_database="run")
    result = project.run_subprocess("-n", "4", "--sqla-url", postgresql_database.url, timeout=300)
    result.assert_outcomes(passed=4)
    assert owned_databases(postgresql_database) == []


BOTH_RUNS_INSIDE = """
import time
from pathlib import Path
from models import User, count

INSIDE = Path(INSIDE_PATH)


def test_both_runs_are_inside(sqla_session, sqla_database_url):
    (INSIDE / sqla_database_url.database).touch()
    deadline = time.monotonic() + 60
    while len(list(INSIDE.iterdir())) < 2:
        assert time.monotonic() < deadline, "the other run never got its database"
        time.sleep(0.05)
    sqla_session.add(User(email="ada@example.com"))
    sqla_session.commit()
    assert count(sqla_session) == 1
"""


def test_two_runs_at_once_on_one_server_both_pass(
    project: Project, sync_database: Database
) -> None:
    """Measured in configured mode: the second run gets SchemaConflictError for the first
    run's schema (tests/sync/test_schema.py). Each run's test waits for the other's."""
    inside = project.path / "inside"
    inside.mkdir()
    project.ini(sqla_database="run")
    project.models()
    project.tests(BOTH_RUNS_INSIDE.replace("INSIDE_PATH", repr(str(inside))))
    project.write_ini()
    command = [sys.executable, "-m", "pytest", "--sqla-url", sync_database.url]
    runs = [
        project.pytester.popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(2)
    ]
    try:
        outputs = [run.communicate(timeout=300)[0].decode() for run in runs]
    finally:
        for run in runs:
            if run.poll() is None:
                run.kill()
    assert [run.returncode for run in runs] == [0, 0], "\n".join(outputs)
    assert len(list(inside.iterdir())) == 2, "two runs, two databases"
    assert owned_databases(sync_database) == []


KILLED = """
import os
import time
from pathlib import Path

PIDS = Path(PIDS_PATH)


def test_waits_to_be_killed(sqla_session, worker_id):
    (PIDS / worker_id).write_text(str(os.getpid()))
    time.sleep(300)


def test_quick(sqla_session):
    pass
"""


def test_a_killed_worker_leaves_the_database_behind_and_the_next_run_names_it(
    project: Project, sync_database: Database
) -> None:
    """A worker killed with SIGKILL never leaves, so the count never reaches 0."""
    pids = project.path / "pids"
    pids.mkdir()
    project.ini(sqla_database="run")
    project.models()
    project.tests(KILLED.replace("PIDS_PATH", repr(str(pids))))
    project.write_ini()
    command = [sys.executable, "-m", "pytest", "-n", "2", "--sqla-url", sync_database.url]
    run = project.pytester.popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 120
        while not [path for path in pids.iterdir() if path.read_text()]:
            assert run.poll() is None, run.communicate()[0].decode()
            assert time.monotonic() < deadline, "no worker got into the test"
            time.sleep(0.1)
        (pid,) = [int(path.read_text()) for path in pids.iterdir()]
        os.kill(pid, signal.SIGKILL)
        output = run.communicate(timeout=300)[0].decode()
    finally:
        if run.poll() is None:
            run.kill()
    assert run.returncode != 0, output
    left = owned_databases(sync_database)
    assert len(left) == 1, output
    mark = read_mark(sync_database, left[0])
    assert mark is not None
    assert mark.workers == 1, "the killed worker is still counted in"

    (project.path / "test_inner.py").write_text("def test_next(sqla_session):\n    pass\n")
    result = project.run("--sqla-url", sync_database.url)
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(
        [
            "*- pytest-sqla -*",
            f"*/{left[0]} is still on the server, from the pytest-sqla run started "
            f"{mark.started_at} by {mark.owner}, with 1 worker(s) counted in: that run is still "
            "going, was killed before its end, or kept it with --sqla-keep-database. Drop it if "
            "nothing uses it.",
        ]
    )
    assert owned_databases(sync_database) == left, "the next run dropped its own, not that one"


# ---------------------------------------------------------------- dropping

LEAK_CONFTEST = """
import contextlib

from sqlalchemy.exc import DBAPIError

LEAKED = []


def pytest_sessionfinish(session):
    for connection, engine in LEAKED:
        with contextlib.suppress(DBAPIError):  # PostgreSQL ended it for the DROP
            connection.close()
        engine.dispose()
"""

LEAKS = """
from sqlalchemy import create_engine, text
from conftest import LEAKED


def test_leaks(sqla_schema, sqla_database_url):
    engine = create_engine(sqla_database_url)  # the application's own engine
    connection = engine.connect()
    if IN_A_TRANSACTION:
        connection.execute(text("SELECT count(*) FROM user_account"))
    LEAKED.append((connection, engine))
"""


@pytest.mark.parametrize("transaction", [False, True], ids=["idle", "in a transaction"])
def test_a_connection_the_run_leaked_cannot_hang_the_drop(
    project: Project, sync_database: Database, transaction: bool
) -> None:
    """PostgreSQL ends both for the DROP. MySQL's DROP ignores an idle connection and waits
    for one in a transaction, at most sqla_lock_timeout, then keeps the database."""
    project.ini(sqla_database="run", sqla_lock_timeout="2")
    project.models()
    project.conftest(LEAK_CONFTEST)
    project.tests(LEAKS.replace("IN_A_TRANSACTION", str(transaction)))
    result = project.run("--sqla-url", sync_database.url)
    if transaction and sync_database.family == "mysql":
        result.assert_outcomes(passed=1, errors=1)
        result.stdout.fnmatch_lines(
            [
                "*ERROR at teardown of test_leaks*",
                "*SchemaConflictError: pytest-sqla: could not drop the database *_???????? for "
                "sqla_database = run. Another connection held a lock it needs for longer than "
                "sqla_lock_timeout (2 s): some code in this run opened its own connection (for "
                "example through the application's own engine) and left a transaction open. The "
                "database is left as it is. The driver reported: (1205, *",
            ]
        )
        assert len(owned_databases(sync_database)) == 1
    else:
        result.assert_outcomes(passed=1)
        assert owned_databases(sync_database) == []


COUNT_SESSIONS = """
import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import QueuePool

SESSIONS = []
ON_POSTGRESQL = (
    "SELECT count(*) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()"
)
ON_MYSQL = "SELECT count(*) FROM information_schema.processlist WHERE db = %s"


def count_sessions(conn, cursor, statement, parameters, context, executemany):
    # PostgreSQL ends the database's sessions right before its DROP; MySQL drops with them on.
    if conn.dialect.name == "postgresql" and "pg_terminate_backend" in statement:
        cursor.execute(ON_POSTGRESQL, (parameters["name"],))
    elif conn.dialect.name == "mysql" and statement.startswith("DROP DATABASE"):
        cursor.execute(ON_MYSQL, (statement.split("`")[1],))
    else:
        return
    SESSIONS.append(cursor.fetchone()[0])


def pytest_configure(config):
    event.listen(Engine, "before_cursor_execute", count_sessions)


def pytest_unconfigure(config):
    event.remove(Engine, "before_cursor_execute", count_sessions)
    print(f"sessions on the database before its drop: {SESSIONS}")


@pytest.fixture(scope="session")
def sqla_engine_options():
    return {"poolclass": QueuePool}
"""


def test_pytest_sqlas_own_connections_are_closed_before_the_drop(
    project: Project, sync_database: Database
) -> None:
    """With a pool of your own the sync engine keeps an idle connection after the tests;
    every fixture that connects depends on the one that drops, so all are gone by then."""
    project.ini(sqla_database="run")
    project.models()
    project.conftest(COUNT_SESSIONS)
    project.tests(RUN_TESTS)
    result = project.run("--sqla-url", sync_database.url)
    result.assert_outcomes(passed=3)
    result.stdout.fnmatch_lines(["sessions on the database before its drop: [[]0[]]"])
    assert owned_databases(sync_database) == []


def test_before_postgresql_13_the_sessions_are_ended_before_the_drop(
    postgresql_database: Database,
) -> None:
    """PostgreSQL 12 and earlier have no FORCE. The control, on 17: a plain DROP fails while
    a session is on the database. pytest-sqla's way for older servers ends it first."""
    name = f"{configured(postgresql_database)}_old_server"
    create_database(postgresql_database, name)
    leaked_url = make_url(sync_twin(postgresql_database.url)).set(database=name)
    leaked_engine = create_engine(leaked_url, poolclass=NullPool)
    leaked = leaked_engine.connect()
    leaked.execute(text("SELECT 1"))
    try:
        with pytest.raises(DBAPIError, match="is being accessed by other users"):
            on_server(postgresql_database, lambda c: c.exec_driver_sql(f'DROP DATABASE "{name}"'))
        owned = Owned(
            admin=make_url(sync_twin(postgresql_database.url)),
            name=name,
            mode="run",
            run_id=RUN_ID,
            worker=None,
            lock_timeout=5.0,
            keep=False,
        )
        on_server(postgresql_database, lambda c: _PostgreSQL().drop(c, owned, version=(12, 0)))
    finally:
        with contextlib.suppress(DBAPIError):
            leaked.close()
        leaked_engine.dispose()
    assert owned_databases(postgresql_database) == []


def test_a_database_marked_by_another_run_since_is_left_alone(
    project: Project, sync_database: Database, run_id: str
) -> None:
    """The checks before the DROP: the name is this run's, but the mark no longer is."""
    name = run_database(sync_database)
    remark = (
        f"COMMENT ON DATABASE \"{name}\" IS '{comment(ANOTHER_RUN)}'"
        if sync_database.family == "postgresql"
        else f"UPDATE `{name}`.pytest_sqla_database SET run_id = '{ANOTHER_RUN.run_id}'"
    )
    project.ini(sqla_database="run")
    project.models()
    project.tests(
        f"""
        from sqlalchemy import create_engine
        from sqlalchemy.pool import NullPool

        def test_another_run_marks_it(sqla_session):
            engine = create_engine(
                {sync_twin(sync_database.url)!r}, poolclass=NullPool, isolation_level="AUTOCOMMIT"
            )
            with engine.connect() as connection:
                connection.exec_driver_sql({remark!r})
            engine.dispose()
        """
    )
    project.run("--sqla-url", sync_database.url).assert_outcomes(passed=1)
    assert owned_databases(sync_database) == [name], "not this run's any more, so not dropped"


def test_keep_database_keeps_it_with_its_tables_and_names_it(
    project: Project, sync_database: Database
) -> None:
    project.ini(sqla_database="run")
    result = inner(project, sync_database, RUN_TESTS, "--sqla-keep-database")
    result.assert_outcomes(passed=3)
    (kept,) = owned_databases(sync_database)
    result.stdout.fnmatch_lines(
        [
            "*- pytest-sqla -*",
            f"kept the database *{kept}, as --sqla-keep-database asked; drop {kept} when you "
            "are done with it.",
        ]
    )
    tables = set(table_names(sync_twin(with_database(sync_database, kept).url)))
    assert {"note", "pytest_sqla", "user_account"} <= tables
    assert ("pytest_sqla_database" in tables) is (sync_database.family == "mysql")


def test_keep_database_names_every_workers_database(
    project: Project, sync_database: Database
) -> None:
    """Under xdist the workers hand the names to the controller, which prints them."""
    project.ini(sqla_database="worker")
    project.models()
    project.tests(WRITERS)
    result = project.run_subprocess(
        "-n", "2", "--sqla-url", sync_database.url, "--sqla-keep-database", timeout=300
    )
    result.assert_outcomes(passed=12)
    kept = owned_databases(sync_database)
    assert len(kept) == 2, kept
    for name in kept:
        result.stdout.fnmatch_lines([f"kept the database *{name}, as --sqla-keep-database asked*"])


# ------------------------------------------------- refusals and bounded waits


@pytest.mark.parametrize("mark", [None, ANOTHER_RUN], ids=["unmarked", "another run's"])
def test_a_database_with_the_name_but_not_the_mark_is_refused_and_kept(
    project: Project, sync_database: Database, run_id: str, mark: Mark | None
) -> None:
    name = run_database(sync_database)
    create_database(sync_database, name, mark)

    def store(connection: Connection) -> None:
        connection.exec_driver_sql("CREATE TABLE precious (id INTEGER)")
        connection.exec_driver_sql("INSERT INTO precious VALUES (1)")

    on_server(sync_database, store, name=name)
    project.ini(sqla_database="run")
    result = inner(project, sync_database, "def test_db(sqla_session):\n    pass\n")
    result.assert_outcomes(errors=1)
    whose = (
        "does not carry pytest-sqla's mark"
        if mark is None
        else "belongs to another pytest-sqla run (started 2026-11-01T09:00:00+00:00 by "
        "elsewhere:4242/gw1)"
    )
    result.stdout.fnmatch_lines(
        [
            f"*SchemaConflictError: pytest-sqla: the database *{name} already exists and "
            f"{whose}, so this run did not create it. pytest-sqla will neither use nor drop it; "
            "drop it yourself if nothing needs it."
        ]
    )
    assert owned_databases(sync_database) == [name]
    precious = on_server(
        sync_database, lambda c: c.scalar(text("SELECT count(*) FROM precious")), name=name
    )
    assert precious == 1


def test_a_held_database_lock_times_out(
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
        project.ini(sqla_database="run", sqla_lock_timeout="1")
        result = inner(project, sync_database, "def test_db(sqla_session):\n    pass\n")
    finally:
        holder.close()
        engine.dispose()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            "*SchemaConflictError: pytest-sqla: gave up after 1 s waiting for the lock on the "
            f"database *{name}; another pytest-sqla process, usually a worker of this run, is "
            "creating, joining or dropping it. A crashed process releases the lock when its "
            "connection closes.*"
        ]
    )
    assert owned_databases(sync_database) == []


NO_CREATEDB = "pytest_sqla_no_createdb"


def test_a_role_that_may_not_create_databases_is_told_what_to_grant(
    project: Project, postgresql_database: Database
) -> None:
    password = "s3cret-of-the-role"

    def drop_role(connection: Connection) -> None:
        connection.exec_driver_sql(f"DROP ROLE IF EXISTS {NO_CREATEDB}")

    on_server(postgresql_database, drop_role)
    on_server(
        postgresql_database,
        lambda c: c.exec_driver_sql(f"CREATE ROLE {NO_CREATEDB} LOGIN PASSWORD '{password}'"),
    )
    try:
        url = make_url(postgresql_database.url).set(username=NO_CREATEDB, password=password)
        role = Database(postgresql_database.backend, url.render_as_string(hide_password=False))
        project.ini(sqla_database="run")
        result = inner(project, role, "def test_db(sqla_session):\n    pass\n")
    finally:
        on_server(postgresql_database, drop_role)
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            "*SchemaConflictError: pytest-sqla: could not create the database "
            f"postgresql+psycopg://{NO_CREATEDB}:***@*/sqla_???????? for sqla_database = run. The "
            f"PostgreSQL role {NO_CREATEDB} may not create databases. Give it CREATEDB (ALTER "
            f"ROLE {NO_CREATEDB} CREATEDB), connect as a role that has it, or set sqla_database "
            "= configured and create the database before the run. The driver reported: "
            "*permission denied to create database*"
        ]
    )
    assert password not in result.stdout.str()
    assert owned_databases(postgresql_database) == []


def test_a_mysql_user_without_create_is_told_what_to_grant(
    project: Project, mysql_database: Database
) -> None:
    """sqla_nocreate has what the image gives its user, all privileges on sqla only."""
    url = make_url(mysql_database.url).set(username="sqla_nocreate")
    user = Database(mysql_database.backend, url.render_as_string(hide_password=False))
    project.ini(sqla_database="run")
    result = inner(project, user, "def test_db(sqla_session):\n    pass\n")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            "*SchemaConflictError: pytest-sqla: could not create the database "
            "mysql+pymysql://sqla_nocreate:***@*/sqla_???????? for sqla_database = run. The MySQL "
            "user sqla_nocreate has no CREATE privilege on it. Grant privileges on the names "
            "pytest-sqla derives from the configured database (GRANT ALL ON `sqla\\_%`.* TO "
            "'sqla_nocreate'@'%'), connect as a user that has them, or set sqla_database = "
            "configured and create the database before the run. The driver reported: (1044, *"
        ]
    )
    assert owned_databases(mysql_database) == []


# ------------------------------------------------------------- name lengths


def limit_of(database: Database) -> int:
    """PostgreSQL takes 63 bytes, MySQL 64 characters; these names are ASCII."""
    return 63 if database.family == "postgresql" else 64


def test_a_name_at_the_servers_limit_is_used_whole(
    project: Project, sync_database: Database, run_id: str
) -> None:
    limit = limit_of(sync_database)
    base = configured(sync_database) + "_" + "b" * (limit - len(configured(sync_database)) - 10)
    create_database(sync_database, base)  # the configured database must exist
    project.ini(sqla_database="run")
    result = inner(
        project,
        with_database(sync_database, base),
        RUN_TESTS
        + f"""
def test_the_whole_name(sqla_connection, sqla_database_url):
    assert len(current(sqla_connection)) == len(sqla_database_url.database) == {limit}
""",
    )
    result.assert_outcomes(passed=4)
    assert owned_databases(sync_database) == [base], "its own dropped, the configured one kept"


def test_postgresql_shortens_a_longer_name_and_mysql_refuses_it(sync_database: Database) -> None:
    """Why the names are checked at startup: PostgreSQL creates a longer name cut to its
    first 63 bytes with only a NOTICE, so two names could become one; MySQL refuses it."""
    limit = limit_of(sync_database)
    name = configured(sync_database) + "_" + "c" * (limit - len(configured(sync_database)))
    assert len(name) == limit + 1
    if sync_database.family == "postgresql":
        create_database(sync_database, name)
        assert owned_databases(sync_database) == [name[:63]]
    else:
        with pytest.raises(DBAPIError, match=r"(?i)too long|incorrect database name"):
            create_database(sync_database, name)


# ------------------------------------------------------ the Alembic recipe


def test_the_readme_alembic_recipe_migrates_a_run_database(
    project: Project, postgresql_database: Database
) -> None:
    from tests.sync.test_alembic_recipe import alembic_project, readme_recipe

    alembic_project(project, readme_recipe())
    project.ini(sqla_database="run")
    result = project.run("--sqla-url", postgresql_database.url)
    result.assert_outcomes(passed=2)
    assert owned_databases(postgresql_database) == []
    assert postgresql_database.tables() == [], "Alembic never touched the configured database"


def test_the_readme_alembic_recipe_migrates_each_workers_database(
    project: Project, postgresql_database: Database
) -> None:
    """Under xdist the override runs once per worker; in worker mode each has a database of
    its own to migrate, so the migrations need no lock."""
    from tests.sync.test_alembic_recipe import alembic_project, readme_recipe

    alembic_project(project, readme_recipe())
    project.tests(WRITERS)
    project.ini(sqla_database="worker")
    result = project.run_subprocess("-n", "3", "--sqla-url", postgresql_database.url, timeout=300)
    result.assert_outcomes(passed=12)
    assert owned_databases(postgresql_database) == []
    assert postgresql_database.tables() == []
