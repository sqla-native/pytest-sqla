# pytest-sqla

Every test gets a SQLAlchemy session whose work is rolled back, sync or async,
even when your code calls `commit()`.

```python
def test_signup(sqla_session):
    signup(sqla_session, "ada@example.com")  # commits, as production code does
    assert sqla_session.scalar(select(func.count(User.id))) == 1
    # ...and the next test starts from an empty table again.
```

pytest-sqla is a pytest plugin for SQLAlchemy 2.0 and 2.1. It gives each test
one connection inside an outer transaction, binds sessions to it with
`join_transaction_mode="create_savepoint"`, and rolls everything back at
teardown. That is SQLAlchemy's own documented recipe, ["Joining a Session into
an External Transaction"][recipe]; the plugin is the dozen details around it
that a 30-line conftest gets wrong.

## Why this exists

The plugin most projects find first, pytest-async-sqlalchemy, last shipped in
October 2021 and fails before its first test on pytest-asyncio 1.x, with
`fixture 'event_loop' not found`. The others are sync-only, or leave the
SQLite workaround to you, or monkeypatch SQLAlchemy. pytest-sqla gets six hard
parts right on its own, each one measured:

1. **Commit and rollback stay inside the test.** The default join mode turns
   into `rollback_only` on a connection that is already in a transaction. An
   application's `rollback()` after an IntegrityError then rolls back the
   rows the test seeded, and its next `commit()` survives the test.
   `create_savepoint` keeps both inside.
2. **SQLite actually rolls back.** In sqlite3's legacy transaction control,
   Python's default through 3.15, a row committed through a savepoint session
   survives the outer rollback. pytest-sqla applies SQLAlchemy's documented
   fix itself.
3. **In-memory SQLite is one database.** `sqlite://` is private to a single
   connection; pytest-sqla shares it between the sync engine, the async engine
   and the schema through SQLite's memdb VFS.
4. **Async objects live on the test's own event loop**, under all five
   pytest-asyncio loop scopes, in strict and auto mode, and under anyio. A
   connection used from another loop raises a clear error before the driver
   sees it; asyncpg, for one, is left unusable by such a call.
5. **The schema is created once, even under pytest-xdist.** A naive
   `create_all` per worker errors on every run against PostgreSQL and MySQL;
   pytest-sqla serialises it under a database lock and never drops a table it
   did not create.
6. **Breaches fail loudly, in the test that caused them.** A commit of the
   connection itself, or MySQL committing DDL on its own, fails that test's
   teardown instead of leaking rows into later tests.

## Install

```bash
pip install pytest-sqla                # sync fixtures
pip install "pytest-sqla[asyncio]"     # adds pytest-asyncio and greenlet
```

The two runtime dependencies are `sqlalchemy>=2.0` and `pytest>=8.2`. The
`asyncio` extra adds `pytest-asyncio>=1.2` and `sqlalchemy[asyncio]`: SQLAlchemy
2.1 no longer installs greenlet, which its asyncio extension cannot import
without. anyio users already have anyio through Starlette, so they need
`pip install pytest-sqla "sqlalchemy[asyncio]"`.

## Quick start

```toml
# pyproject.toml
[tool.pytest.ini_options]
sqla_url = "sqlite://"
sqla_metadata = "app.models:Base"
```

```python
from sqlalchemy import func, select

from app.models import User


def test_commits_are_rolled_back(sqla_session):
    sqla_session.add(User(email="ada@example.com"))
    sqla_session.commit()
    assert sqla_session.scalar(select(func.count()).select_from(User)) == 1


def test_and_the_next_test_starts_clean(sqla_session):
    assert sqla_session.scalar(select(func.count()).select_from(User)) == 0
```

The tables in `app.models:Base` are created before the first database test and
dropped after the last one. For the async fixtures, install the `asyncio` extra
and give them an async driver:

```toml
[tool.pytest.ini_options]
sqla_url = "sqlite://"
sqla_async_url = "sqlite+aiosqlite://"  # the same in-memory database; needs aiosqlite
sqla_metadata = "app.models:Base"
asyncio_mode = "strict"
asyncio_default_fixture_loop_scope = "function"
```

```python
import pytest


@pytest.mark.asyncio
async def test_async_commits_are_rolled_back_too(sqla_async_session):
    sqla_async_session.add(User(email="ada@example.com"))
    await sqla_async_session.commit()
```

In CI, point the same tests at a real server through the environment, which
takes precedence over the ini file:

```yaml
env:
  PYTEST_SQLA_URL: postgresql+psycopg://app:app@localhost:5432/app_test
  PYTEST_SQLA_ASYNC_URL: postgresql+psycopg://app:app@localhost:5432/app_test
```

`postgresql+psycopg` serves both the sync and the async fixtures. Set the async
variable whenever the ini file sets `sqla_async_url`: the two URLs must name the
same database, and pytest-sqla refuses to run with one on PostgreSQL and the
other still on SQLite. With MySQL, use `mysql+pymysql` and `mysql+aiomysql`.

## FastAPI

Override the application's session dependency with `sqla_async_sessionmaker`.
Every request gets its own AsyncSession, all of them inside the test's
transaction, so the application commits and rolls back as it does in
production, and the next test sees none of it.

```python
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app, get_session


@pytest.fixture(scope="session")
def sqla_session_options():
    return {"expire_on_commit": False}  # as the app's own sessionmaker


@pytest_asyncio.fixture
async def client(sqla_async_sessionmaker):
    async def override():
        async with sqla_async_sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_a_duplicate_email_is_a_conflict(client):
    assert (await client.post("/users", params={"email": "ada@example.com"})).status_code == 201
    assert (await client.post("/users", params={"email": "ada@example.com"})).status_code == 409
    assert (await client.get("/users/count")).json() == {"count": 1}


@pytest.mark.asyncio
async def test_the_previous_tests_commit_was_rolled_back(client):
    assert (await client.get("/users/count")).json() == {"count": 0}
```

Use `httpx.AsyncClient` with `ASGITransport`, not Starlette's `TestClient`.
TestClient runs the application in a thread with an event loop of its own, and
the test's connection belongs to the test's loop. Match `sqla_session_options`
to your own sessionmaker: with the default `expire_on_commit=True`, reading
`user.id` after `commit()` reloads it with blocking IO, which SQLAlchemy's
asyncio extension refuses with MissingGreenlet.

This example runs in CI on SQLite, PostgreSQL and MySQL, under pytest-asyncio
and under anyio: see [`examples/`](examples/).

## Fixtures

| Fixture | Scope | Family | What it gives you |
| --- | --- | --- | --- |
| `sqla_url` | session | config | The configured database URL, or None. Override it to compute one. |
| `sqla_async_url` | session | config | The async fixtures' URL; `sqla_url` unless set separately. |
| `sqla_metadata` | session | config | The metadata to create, from the `sqla_metadata` ini key or an override. |
| `sqla_engine_options` | session | config | Keyword arguments for every engine. Default `{}`. |
| `sqla_session_options` | session | config | Keyword arguments for the sessionmakers. Default `{}`. |
| `sqla_schema` | session | config | Creates the tables once, drops them at the end. Override it for Alembic. |
| `sqla_connection` | function | sync | A `Connection` inside the test transaction. |
| `sqla_sessionmaker` | function | sync | A `sessionmaker` bound to it, for dependency overrides. |
| `sqla_session` | function | sync | A `Session` from it, closed at teardown. |
| `sqla_async_connection` | function | async | An `AsyncConnection` inside the test transaction, on the test's loop. |
| `sqla_async_sessionmaker` | function | async | An `async_sessionmaker` bound to it. |
| `sqla_async_session` | function | async | An `AsyncSession` from it, closed at teardown. |

Within one test, every session from a sessionmaker shares the test's
connection: `commit()` releases a savepoint, `rollback()` rolls back to one,
and `close()` leaves the connection open. A test uses one family: the sync and
async fixtures hold separate connections and transactions, so they could not
see each other's writes, and asking for both is an error.

## Configuration

| Setting | Command line | Environment | ini key | Default |
| --- | --- | --- | --- | --- |
| Database URL | `--sqla-url` | `PYTEST_SQLA_URL` | `sqla_url` | none |
| Async URL | `--sqla-async-url` | `PYTEST_SQLA_ASYNC_URL` | `sqla_async_url` | the database URL |
| Metadata | | | `sqla_metadata = "app.models:Base"` | none |
| Schema mode | | | `sqla_schema = create` or `none` | `create` |
| Lock and DDL timeout, seconds | | | `sqla_lock_timeout` | `30` |
| Replace a crashed run's schema | `--sqla-recreate-schema` | | | off |

The command line beats the environment, which beats the ini file: the ini value
is the repository's committed default, the environment is per machine or CI
job, and the command line is per run. Overriding a fixture in a conftest
replaces the computed value, as with any pytest fixture.

- **No database, no silent fallback.** Without a URL, every test that asks for
  a database fixture fails at setup with a message naming the three ways to set
  one; tests that do not are unaffected. pytest-sqla never falls back to
  SQLite on its own, because a CI job whose URL never arrived would then test
  SQLite and look green. A variable that is set but empty is an error for the
  same reason.
- **Only its own variables.** pytest-sqla never reads your application's
  `DATABASE_URL`, so it cannot pick up a database that is not for tests.
- **Checked before the run.** The URLs, the schema mode, the timeout and the
  shape of `sqla_metadata` are checked at startup; a bad one stops the run
  with a usage error that never repeats the URL's password.
- **Inert unless asked.** A run whose tests never request an `sqla_*` fixture
  imports no application code, opens no connection, and never imports
  `sqlalchemy.ext.asyncio`.
- **The report header says which database a run used:**
  `sqla: url=postgresql+psycopg://app:***@localhost/app_test (from PYTEST_SQLA_URL), schema=create`.

`sqla_engine_options` reaches every engine pytest-sqla creates: `echo`,
`connect_args`, `json_serializer`, `execution_options` and so on. It refuses
what would make a rollback impossible (`isolation_level="AUTOCOMMIT"`,
sqlite3's `autocommit=True`) and, for async engines, `poolclass`, which
pytest-sqla picks per event loop. The same options reach the sync and the
async engines, so keep driver-specific `connect_args` to a URL whose driver
serves both. `sqla_session_options` reaches the
sessionmakers, so the test sessions can behave like the application's:
`expire_on_commit`, `autoflush`, `info`, or `class_` for sqlmodel's `Session`
and `AsyncSession`. It refuses `bind`, `binds` and `join_transaction_mode`.

The metadata can be a `MetaData`, any class whose `.metadata` is one (a
`DeclarativeBase`, `declarative_base()`, SQLModel), or a list of these:

```python
@pytest.fixture(scope="session")
def sqla_metadata():
    return [billing.Base, accounts.Base]
```

## Schema

**`sqla_schema = create`**, the default, creates the metadata's tables before
the first database test and drops them after the last one, once per run. It
records what it created in a small marker table, `pytest_sqla`, and uses it to
keep three promises:

- **It never drops a table it did not create.** If a metadata table already
  exists without the marker, every database test fails with
  SchemaConflictError naming the tables, and nothing is touched. Point
  `sqla_url` at a database used only for tests.
- **It notices a crashed run.** A run killed before its teardown leaves the
  marker behind. The next run refuses that schema, says when and where the
  other run started, and suggests `--sqla-recreate-schema`, which drops and
  recreates it.
- **It never hangs.** Creation and drop wait at most `sqla_lock_timeout`
  seconds for a lock. A test that leaked a transaction on a table makes the
  drop fail with a message saying so, instead of blocking the run forever.

On MySQL, where DDL commits on its own, a creation that fails half way drops the
tables it had already created. A SQLite database pytest-sqla made up, an
in-memory one or a per-worker file, is discarded at the end instead of having
its tables dropped.

**`sqla_schema = none`** runs no DDL at all, for a database migrated before the
run, for example by `alembic upgrade head` in a CI step.

**Overriding the `sqla_schema` fixture** replaces all of it. Set
`sqla_schema = none` as well, so the URL is used exactly as configured, and use
a file or server database: an in-memory SQLite database exists only inside
pytest-sqla's own connections.

```python
@pytest.fixture(scope="session")
def sqla_schema(sqla_url):
    config = alembic.config.Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", sqla_url.render_as_string(hide_password=False))
    alembic.command.upgrade(config, "head")
    yield
    alembic.command.downgrade(config, "base")
```

Under pytest-xdist such an override runs once per worker, so it has to
serialise itself.

## Async details

- **pytest-asyncio, strict or auto mode.** In strict mode, mark async tests
  with `@pytest.mark.asyncio`; pytest-sqla's async fixtures need no marking,
  because they are plain sync fixtures that hand you objects opened on the
  right loop.
- **Every loop scope.** `@pytest.mark.asyncio(loop_scope="module")`, the
  `asyncio_default_test_loop_scope` ini key, class, package and session loops
  all work: pytest-sqla reads the test's loop scope and opens its connection on
  that loop.
- **anyio.** `@pytest.mark.anyio` tests work with and without pytest-asyncio
  installed, with pytest-asyncio in strict mode. In auto mode pytest-asyncio
  would run an anyio test's async fixtures on its own loop, so pytest-sqla
  refuses that combination with an explanation. SQLAlchemy's asyncio extension
  runs only on asyncio, so the trio backend is refused too.
- **The loop guard.** Using a test's connection from another event loop raises
  `LoopMismatchError` before the driver sees the statement, and the connection
  stays usable. Measured without the guard: asyncpg, aiomysql and asyncmy fail
  with "attached to a different loop", and asyncpg's connection is unusable
  afterwards; psycopg happens to survive, so the guard treats every driver
  alike. The two
  usual causes are an async fixture on a different loop scope than the test
  (give it the test's loop scope), and application code that runs in its own
  thread with its own loop, such as Starlette's TestClient (use
  `httpx.AsyncClient` with `ASGITransport`).
- **Pooling.** For a per-test loop, the pytest-asyncio default, and for anyio,
  the async engine is NullPool, which SQLAlchemy's asyncio docs recommend when
  one engine serves several event loops. For a class, module, package or
  session loop, the engine has a real pool that lives exactly as long as that
  loop. The sync engine is always a normal pool.

What NullPool costs, measured with [`scripts/benchmark.py`](scripts/benchmark.py)
on localhost Docker (milliseconds per test, best of 5 runs of 200 isolation
cycles; re-measure on your own machine before relying on them):

| Driver | Pooled | NullPool |
| --- | ---: | ---: |
| asyncpg | 1.54 | 7.02 |
| psycopg async | 1.54 | 5.47 |
| aiomysql | 1.94 | 3.10 |
| asyncmy | 1.87 | 3.09 |
| psycopg (sync, for reference) | 1.31 | 4.88 |
| pymysql (sync, for reference) | 1.29 | 6.39 |

If those milliseconds matter, give the database tests a wider loop scope, for
example `asyncio_default_test_loop_scope = module`.

pytest-asyncio 1.2 is the floor: on 1.0 and 1.1, a test with a session loop
that ran after tests with a module loop was measured running on a different
loop than its own scope's fixtures.

## SQLite notes

- **The savepoint fix.** pytest-sqla installs SQLAlchemy's documented connect
  and begin listeners on every SQLite engine it creates, sync and async. With
  `connect_args={"autocommit": False}` (Python 3.12+, PEP 249 mode) sqlite3
  handles transactions correctly by itself, and pytest-sqla adds nothing. When
  a future Python makes that the default, the fix switches itself off.
- **In-memory databases** (`sqlite://`, `sqlite:///:memory:`,
  `sqlite+aiosqlite://`) become one named database on SQLite's memdb VFS for
  the whole session, held open by a keeper connection, and shared between the
  sync and async engines. That needs SQLite 3.36 (2021) or newer.
  Shared-cache mode is not used: SQLite calls it obsolete, and it fails at
  once where memdb waits for a lock like a file does. A shared-memory URL of
  your own (`mode=memory`, `vfs=memdb`) is used as given.
- **Files.** A test's transaction on a SQLite file begins `IMMEDIATE`, so
  another process waits for it instead of failing with "database is locked".
  Under pytest-xdist in create mode, each worker gets its own copy next to the
  configured file, `test-gw0.db`, which pytest-sqla deletes afterwards; it
  never deletes a file you named. In none mode the workers share the file and
  take turns.
- **A raw `COMMIT` or sqlite3's `executescript()`** inside a test ends the
  transaction, and the test fails at teardown with IsolationError.
- **An unread result in a failing test.** On Python 3.11 and later, a query
  result left unread keeps a SQLite read lock until it is garbage-collected,
  and pytest keeps the last failure's traceback. On a file you named, that can
  block the drop at the end of the run; pytest-sqla then says so.

## MySQL notes

MySQL commits implicitly on DDL (`CREATE`, `ALTER`, `DROP`, `TRUNCATE`) and on
`LOCK TABLES`, which ends the test's transaction: the rows the test wrote
before that point are now permanent. pytest-sqla notices, because its guard
savepoint is gone, and fails that test with IsolationError. Run such statements
outside the sqla fixtures. MariaDB uses the same code path but is not tested in
CI. Non-transactional tables (MyISAM, MEMORY) cannot be rolled back at all.

## pytest-xdist

All workers of a run share one database. They cannot see each other's data,
because no test ever commits. The schema is created by the first worker under
a database lock (`pg_advisory_xact_lock` on PostgreSQL, `GET_LOCK` on MySQL,
`BEGIN IMMEDIATE` on SQLite), joined by the others, and dropped by the last
one out. Measured before this design: a `create_all` per worker with `-n 4`
ended "2 passed, 6 errors" on every run against both servers.

Limits:

- Two workers inserting the same unique value wait on each other until one
  test ends.
- Sequences and AUTO_INCREMENT counters are shared, so do not assert ids.
- One run per database: a second pytest run on the same database while the
  first is going gets SchemaConflictError. Per-run databases are on the
  roadmap.
- A worker that crashes never leaves, so its run's tables stay behind and the
  next run asks for `--sqla-recreate-schema`.
- Schema locks exist for PostgreSQL, MySQL, MariaDB and SQLite. Any other
  dialect works in a single process; under xdist, create the schema beforehand
  and set `sqla_schema = none`.

## What pytest-sqla reports

pytest-sqla never warns: suites run with `filterwarnings = error`, so every
problem is an error with a message that says what to do. Setup problems (no
URL, the wrong driver family, an async fixture in a sync test) fail the test at
setup. Three exception classes, all subclasses of `pytest_sqla.PytestSqlaError`:

- **IsolationError**, at the teardown of the test that broke isolation: it
  committed the connection itself (`session.connection().commit()`), or the
  database ended the transaction on its own. A plain `connection.rollback()`
  escapes nothing and is not an error.
- **LoopMismatchError**, a `RuntimeError` too, at the first statement run on
  the wrong event loop.
- **SchemaConflictError**, from `sqla_schema`, on every database test: tables
  it did not create, another run's schema, a lock that timed out, or a drop
  blocked by a leaked transaction.

A test's own failure always wins: a breach found at teardown is reported as a
separate error and never replaces it. URLs in messages always hide the
password.

## Migrating

| From | Was | Now |
| --- | --- | --- |
| pytest-async-sqlalchemy | `--database-url`, `database_url` | `--sqla-url`, `sqla_url` |
| | `db_session`, `dbsession` | `sqla_async_session` |
| | `init_database` | `sqla_metadata` |
| | your `event_loop` fixture override | delete it |
| pytest-sqlalchemy | `--sqlalchemy-connect-url` | `--sqla-url` |
| | `connection`, `transaction` | `sqla_connection` |
| | `dbsession` | `sqla_session` |
| | `--sqlalchemy-manage-db`, `db_schema` | `sqla_schema` |
| sqlalchemyseed | your `engine` fixture, the SQLite listeners, StaticPool | `sqla_url`; delete the rest |
| | `sqlalchemyseed_session` | override it to return `sqla_session`, and keep `seed` |

## Using it with database fixtures

`sqla_url` is an ordinary session fixture, so a container or server fixture
can supply it, as a str or a URL:

```python
from testcontainers.community.postgres import PostgresContainer


@pytest.fixture(scope="session")
def sqla_url():
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        yield postgres.get_connection_url()
```

Everything that depends on `sqla_url`, the schema's drop included, is torn
down before the container stops. pytest-databases and pytest-postgresql
fixtures plug in the same way.

## Limitations

- Code that builds its own engine or sessionmaker, instead of the one you
  inject, works outside the test transaction, and pytest-sqla cannot roll it
  back.
- One connection per test: running two sessions concurrently, as with
  `asyncio.gather`, fails as it would on one connection in production.
  Requests within one test must run one after another.
- CREATE SCHEMA for schema-qualified metadata is up to you.

## Compatibility

- Python 3.10 to 3.14. Python 3.10 runs on SQLAlchemy 2.0, because 2.1
  requires 3.11.
- SQLAlchemy 2.0 and 2.1; pytest 8.2 or newer; pytest-asyncio 1.2 or newer.
- Tested in CI on PostgreSQL 17 with psycopg 3 (sync and async) and asyncpg,
  on MySQL 8.4 with pymysql, aiomysql and asyncmy, and on SQLite with pysqlite
  and aiosqlite. MariaDB uses the MySQL code path and is untested.

## Roadmap

Alembic as a schema mode, under the same lock; per-run and per-worker
databases on PostgreSQL and MySQL; data seeded once per module inside an outer
savepoint; rebinding an application's own sessionmaker; factory_boy and
polyfactory helpers; a docs site.

## License

MIT. pytest-sqla is not affiliated with the SQLAlchemy project.

[recipe]: https://docs.sqlalchemy.org/en/20/orm/session_transaction.html#joining-a-session-into-an-external-transaction-such-as-for-test-suites
