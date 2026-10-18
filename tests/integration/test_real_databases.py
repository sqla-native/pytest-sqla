"""pytest-sqla against real PostgreSQL 17 and MySQL 8.4 servers, every driver.

Everything else runs on SQLite, which is in-process, has transactional DDL and
does not care which event loop calls it. These servers differ on exactly the
points pytest-sqla exists for: MySQL commits DDL implicitly, PostgreSQL aborts
a transaction on error, and asyncpg, aiomysql and asyncmy break when a
connection crosses event loops. Each spec runs an inner pytest session with
``--sqla-url``, the way a CI job would.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from tests.helpers import MODELS, Project
from tests.integration.conftest import Database, sync_twin

ANYIO = """
import pytest

@pytest.fixture
def anyio_backend():
    return "asyncio"
"""


def inner(project: Project, database: Database, source: str, *args: str) -> pytest.RunResult:
    project.models()
    project.conftest(ANYIO)
    project.tests(source)
    return project.run("--sqla-url", database.url, *args)


def rendered(database: Database) -> str:
    return make_url(database.url).render_as_string(hide_password=True)


# ------------------------------------------------------------------ sync


SYNC_ISOLATION = """
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from models import User, count

def test_commits(sqla_session):
    sqla_session.add(User(email="ada@example.com"))
    sqla_session.commit()
    assert count(sqla_session) == 1

def test_sees_nothing(sqla_session):
    assert count(sqla_session) == 0

def test_a_conflict_keeps_the_seeded_row(sqla_session, sqla_sessionmaker):
    sqla_session.add(User(email="seed@example.com"))
    sqla_session.commit()
    with sqla_sessionmaker() as app:
        app.add(User(email="seed@example.com"))
        with pytest.raises(IntegrityError):
            app.commit()
        app.rollback()
        assert count(app) == 1
        app.add(User(email="after@example.com"))
        app.commit()
    assert count(sqla_session) == 2

def test_sessions_share_the_transaction(sqla_sessionmaker, sqla_connection):
    with sqla_sessionmaker() as first:
        first.add(User(email="first@example.com"))
        first.commit()
    with sqla_sessionmaker() as second:
        assert count(second) == 1
    assert sqla_connection.scalar(text("SELECT count(*) FROM user_account")) == 1

def test_the_default_join_mode_is_guarded_too(sqla_session, sqla_connection):
    sqla_session.add(User(email="seed@example.com"))
    sqla_session.commit()
    app = Session(bind=sqla_connection)
    app.add(User(email="app@example.com"))
    app.rollback()
    assert count(sqla_session) == 1
    app.close()

def test_untidy_sessions(sqla_session, sqla_sessionmaker):
    assert count(sqla_session) == 0
    first = sqla_sessionmaker()
    first.add(User(email="first@example.com"))
    first.flush()
    second = sqla_sessionmaker()
    assert count(second) == 1
    first.close()
    third = sqla_sessionmaker()
    third.add(User(email="left-open@example.com"))
    third.flush()

def test_rolling_the_connection_back_is_fine(sqla_session, sqla_connection):
    sqla_session.add(User(email="gone@example.com"))
    sqla_session.flush()
    sqla_connection.rollback()

def test_still_nothing(sqla_session):
    assert count(sqla_session) == 0
"""


def test_sync_isolation(project: Project, sync_database: Database) -> None:
    inner(project, sync_database, SYNC_ISOLATION).assert_outcomes(passed=8)
    assert sync_database.tables() == []


def test_a_committed_connection_is_reported_and_did_leak(
    project: Project, sync_database: Database
) -> None:
    result = inner(
        project,
        sync_database,
        """
        from models import User, count

        def test_escapes(sqla_session):
            sqla_session.add(User(email="leak@example.com"))
            sqla_session.flush()
            sqla_session.connection().commit()

        def test_later(sqla_session):
            assert count(sqla_session) == 1, "the leak is real, which is why it is an error"
        """,
    )
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(["*IsolationError: pytest-sqla: test_inner.py::test_escapes*"])
    assert sync_database.tables() == []


def test_postgresql_rolls_back_ddl_inside_a_test(
    project: Project, postgresql_database: Database
) -> None:
    result = inner(
        project,
        postgresql_database,
        """
        from sqlalchemy import inspect, text

        def test_creates_a_table(sqla_connection):
            sqla_connection.execute(text("CREATE TABLE it_extra (id INTEGER)"))
            assert "it_extra" in inspect(sqla_connection).get_table_names()

        def test_it_is_gone(sqla_connection):
            assert "it_extra" not in inspect(sqla_connection).get_table_names()
        """,
    )
    result.assert_outcomes(passed=2)
    assert postgresql_database.tables() == []


@pytest.mark.parametrize(
    "statements",
    [
        ["CREATE TABLE it_extra (id INTEGER)"],
        ["TRUNCATE TABLE note"],
        ["LOCK TABLES user_account WRITE", "UNLOCK TABLES"],
    ],
    ids=["CREATE TABLE", "TRUNCATE", "LOCK TABLES"],
)
def test_mysql_implicit_commits_are_reported(
    project: Project, mysql_database: Database, statements: list[str]
) -> None:
    executes = "\n    ".join(f"sqla_session.execute(text({sql!r}))" for sql in statements)
    result = inner(
        project,
        mysql_database,
        f"""
from sqlalchemy import text
from models import User

def test_ends_it(sqla_session):
    sqla_session.add(User(email="before@example.com"))
    sqla_session.flush()
    {executes}
    sqla_session.add(User(email="after@example.com"))
    sqla_session.flush()
""",
    )
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        [
            "*IsolationError: pytest-sqla: the database ended test_inner.py::test_ends_it's "
            "transaction on its own*The driver reported: (1305, 'SAVEPOINT sa_savepoint_1 does not "
            "exist')"
        ]
    )


# ---------------------------------------------------------------- schema


def run_twin(database: Database, *statements: str) -> None:
    engine = create_engine(sync_twin(database.url), poolclass=NullPool)
    try:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
    finally:
        engine.dispose()


def test_foreign_tables_are_refused_and_kept(project: Project, sync_database: Database) -> None:
    run_twin(
        sync_database,
        "CREATE TABLE user_account (id INTEGER PRIMARY KEY, email VARCHAR(100))",
        "INSERT INTO user_account (id, email) VALUES (1, 'precious')",
    )
    result = inner(project, sync_database, "def test_db(sqla_session):\n    pass\n")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            f"*SchemaConflictError: pytest-sqla: the database at {rendered(sync_database)} "
            "already has tables that pytest-sqla did not create: user_account.*"
        ]
    )
    assert sync_database.tables() == ["user_account"]
    engine = create_engine(sync_twin(sync_database.url), poolclass=NullPool)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT email FROM user_account")) == "precious"
    engine.dispose()


def test_another_runs_schema_is_refused_then_recreated(
    project: Project, sync_database: Database
) -> None:
    project.models()
    project.tests("def test_db(sqla_session):\n    pass\n")
    run_twin(
        sync_database,
        "CREATE TABLE pytest_sqla (run_id VARCHAR(64) PRIMARY KEY, workers INTEGER NOT NULL, "
        "started_at VARCHAR(32) NOT NULL, owner VARCHAR(255) NOT NULL)",
        "INSERT INTO pytest_sqla VALUES ('killed', 3, '2026-10-18T09:00:00+00:00', 'ci:1/gw2')",
        "CREATE TABLE user_account (id INTEGER PRIMARY KEY, email VARCHAR(100))",
    )
    refused = project.run("--sqla-url", sync_database.url)
    refused.assert_outcomes(errors=1)
    refused.stdout.fnmatch_lines(
        [
            "*holds the schema of another pytest-sqla run (started 2026-10-18T09:00:00+00:00 by "
            "ci:1/gw2; 3 worker(s) never finished)*"
        ]
    )
    recreated = project.run("--sqla-url", sync_database.url, "--sqla-recreate-schema")
    recreated.assert_outcomes(passed=1)
    assert sync_database.tables() == []


def test_a_failed_creation_leaves_nothing_behind(project: Project, sync_database: Database) -> None:
    """MySQL committed the first tables before the failure; pytest-sqla drops them."""
    project.models(
        MODELS
        + """
from sqlalchemy import CheckConstraint, ForeignKey


class Broken(Base):
    __tablename__ = "zz_broken"
    __table_args__ = (CheckConstraint("this is not ( sql"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    note_id: Mapped[int] = mapped_column(ForeignKey("note.id"))
"""
    )
    project.tests("def test_db(sqla_session):\n    pass\n")
    result = project.run("--sqla-url", sync_database.url)
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*ProgrammingError*"])
    assert sync_database.tables() == []


def test_a_held_schema_lock_times_out(project: Project, sync_database: Database) -> None:
    engine = create_engine(sync_twin(sync_database.url), poolclass=NullPool)
    holder = engine.connect()
    if sync_database.family == "postgresql":
        holder.begin()
        holder.execute(select(func.pg_advisory_xact_lock(0x7079_7465_7374_5351)))
    else:
        database = str(make_url(sync_database.url).database)
        name = "pytest_sqla:" + hashlib.sha1(database.encode()).hexdigest()
        assert holder.scalar(select(func.get_lock(name, 1))) == 1
    try:
        project.ini(sqla_lock_timeout="1")
        result = inner(project, sync_database, "def test_db(sqla_session):\n    pass\n")
    finally:
        holder.close()
        engine.dispose()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            "*SchemaConflictError: pytest-sqla: gave up after 1 s waiting for the schema lock on "
            f"{rendered(sync_database)}; another pytest-sqla process*"
        ]
    )


def test_a_leaked_transaction_cannot_hang_the_drop(
    project: Project, sync_database: Database
) -> None:
    project.ini(sqla_lock_timeout="2")
    project.models()
    project.conftest(
        """
        LEAKED = []

        def pytest_sessionfinish(session):
            for connection, engine in LEAKED:
                connection.close()
                engine.dispose()
        """
    )
    project.tests(
        f"""
        from sqlalchemy import create_engine, select
        from conftest import LEAKED
        from models import User

        def test_leaks(sqla_schema):
            engine = create_engine({sync_twin(sync_database.url)!r})
            connection = engine.connect()
            connection.begin()
            connection.execute(select(User))
            LEAKED.append((connection, engine))
        """
    )
    result = project.run("--sqla-url", sync_database.url)
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        [
            "*ERROR at teardown of test_leaks*",
            "*SchemaConflictError: pytest-sqla: could not drop the test schema in "
            f"{rendered(sync_database)} within 2 s: another connection still holds a lock*",
        ]
    )


# ----------------------------------------------------------------- async


ASYNC_ISOLATION = """
import pytest
from sqlalchemy.exc import IntegrityError
from models import User, acount

pytestmark = {marker}

async def test_commits(sqla_async_session):
    sqla_async_session.add(User(email="ada@example.com"))
    await sqla_async_session.commit()
    assert await acount(sqla_async_session) == 1

async def test_sees_nothing(sqla_async_session):
    assert await acount(sqla_async_session) == 0

async def test_a_conflict_keeps_the_seeded_row(sqla_async_session, sqla_async_sessionmaker):
    sqla_async_session.add(User(email="seed@example.com"))
    await sqla_async_session.commit()
    async with sqla_async_sessionmaker() as app:
        app.add(User(email="seed@example.com"))
        with pytest.raises(IntegrityError):
            await app.commit()
        await app.rollback()
        assert await acount(app) == 1
    assert await acount(sqla_async_session) == 1

async def test_untidy_sessions(sqla_async_session, sqla_async_sessionmaker):
    assert await acount(sqla_async_session) == 0
    first = sqla_async_sessionmaker()
    first.add(User(email="first@example.com"))
    await first.flush()
    second = sqla_async_sessionmaker()
    assert await acount(second) == 1
    await first.close()

async def test_still_nothing(sqla_async_connection):
    from sqlalchemy import text
    assert await sqla_async_connection.scalar(text("SELECT count(*) FROM user_account")) == 0
"""


@pytest.mark.parametrize(
    "marker",
    [
        "pytest.mark.asyncio",
        'pytest.mark.asyncio(loop_scope="module")',
        'pytest.mark.asyncio(loop_scope="session")',
        "pytest.mark.anyio",
    ],
    ids=["function loop", "module loop", "session loop", "anyio"],
)
def test_async_isolation_on_every_loop(
    project: Project, async_database: Database, marker: str
) -> None:
    result = inner(project, async_database, ASYNC_ISOLATION.format(marker=marker))
    result.assert_outcomes(passed=5)
    assert async_database.tables() == []


def test_an_async_committed_connection_is_reported(
    project: Project, async_database: Database
) -> None:
    result = inner(
        project,
        async_database,
        """
        import pytest
        from models import User

        @pytest.mark.asyncio
        async def test_escapes(sqla_async_session):
            sqla_async_session.add(User(email="leak@example.com"))
            await sqla_async_session.flush()
            await (await sqla_async_session.connection()).commit()
        """,
    )
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*IsolationError: pytest-sqla: test_inner.py::test_escapes*"])


def test_a_loop_mismatch_leaves_the_connection_usable(
    project: Project, async_database: Database
) -> None:
    """asyncpg, aiomysql and asyncmy break a connection used from another loop."""
    result = inner(
        project,
        async_database,
        """
        import pytest
        import pytest_asyncio
        from sqlalchemy import text
        from models import acount

        @pytest_asyncio.fixture
        async def on_the_function_loop(sqla_async_session):
            await sqla_async_session.execute(text("SELECT 1"))

        @pytest.mark.asyncio(loop_scope="module")
        async def test_mismatch(on_the_function_loop):
            pass

        @pytest.mark.asyncio(loop_scope="module")
        async def test_the_pooled_connection_still_works(sqla_async_session):
            assert await acount(sqla_async_session) == 0
        """,
    )
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*LoopMismatchError: pytest-sqla: sqla_async_session belongs*"])
    assert "ERROR at teardown" not in result.stdout.str()


def test_the_module_loop_pool_is_reused(project: Project, async_database: Database) -> None:
    result = inner(
        project,
        async_database,
        """
        import pytest
        from sqlalchemy.pool import AsyncAdaptedQueuePool

        ENGINES = []
        pytestmark = pytest.mark.asyncio(loop_scope="module")

        @pytest.mark.parametrize("n", range(3))
        async def test_pooled(sqla_async_connection, n):
            ENGINES.append(sqla_async_connection.engine.sync_engine)
            assert isinstance(ENGINES[0].pool, AsyncAdaptedQueuePool)
            assert all(engine is ENGINES[0] for engine in ENGINES)
            assert ENGINES[0].pool.checkedout() == 1
        """,
    )
    result.assert_outcomes(passed=3)


def test_a_thread_without_a_loop_is_stopped_by_the_guard(
    project: Project, asyncpg_database: Database
) -> None:
    """asyncpg makes a cursor without awaiting, so the guard is what stops it.

    psycopg, aiomysql and asyncmy await to make one, and SQLAlchemy raises
    MissingGreenlet first (measured)."""
    result = inner(
        project,
        asyncpg_database,
        """
        import asyncio
        import pytest
        from pytest_sqla import LoopMismatchError

        @pytest.mark.asyncio
        async def test_from_a_thread(sqla_async_connection):
            sync_connection = sqla_async_connection.sync_connection
            with pytest.raises(LoopMismatchError, match="a thread with no running event loop"):
                await asyncio.to_thread(sync_connection.exec_driver_sql, "SELECT 1")
            assert await sqla_async_connection.scalar(select_one()) == 1

        def select_one():
            from sqlalchemy import text
            return text("SELECT 1")
        """,
    )
    result.assert_outcomes(passed=1)
