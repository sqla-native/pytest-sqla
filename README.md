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
SQLite workaround to you, or monkeypatch SQLAlchemy. pytest-sqla gets eight hard
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
   teardown, so the leak is reported where it happened instead of surfacing
   as a mystery in some later test.
7. **A database of its own, when you want one.** With `sqla_database = run`,
   each pytest run creates its own database on PostgreSQL or MySQL, shared by
   its xdist workers and dropped by the last one out, or one per worker with
   `worker`. The usual fixture that drops and recreates one test database
   breaks under xdist, because every worker drops the one the others use.
8. **Your Alembic migrations, if you have them.** With `sqla_schema =
   alembic` they build the schema instead of `create_all`, once per database,
   with your env.py as it is, and pytest-sqla checks afterwards that they
   reached your heads. A connection the migrations open to any other
   database, such as the application's own `DATABASE_URL`, is refused.

## Install

```bash
pip install pytest-sqla                # sync fixtures
pip install "pytest-sqla[asyncio]"     # adds pytest-asyncio and greenlet
pip install "pytest-sqla[alembic]"     # adds Alembic, for sqla_schema = alembic
```

The two runtime dependencies are `sqlalchemy>=2.0` and `pytest>=8.2`. The
`asyncio` extra adds `pytest-asyncio>=1.2` and `sqlalchemy[asyncio]`: SQLAlchemy
2.1 no longer installs greenlet, which its asyncio extension cannot import
without. anyio users already have anyio through Starlette, so they need
`pip install pytest-sqla "sqlalchemy[asyncio]"`. The `alembic` extra adds
`alembic>=1.16`, which only `sqla_schema = alembic` imports.

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
and under anyio: see
[`examples/`](https://github.com/sqla-native/pytest-sqla/tree/main/examples).

## Fixtures

| Fixture | Scope | Family | What it gives you |
| --- | --- | --- | --- |
| `sqla_url` | session | config | The configured database URL, or None. Override it to compute one. |
| `sqla_async_url` | session | config | The async fixtures' URL; `sqla_url` unless set separately. |
| `sqla_database_url` | session | config | The URL pytest-sqla connects to, for your own tools: `sqla_url`, or the database `sqla_database` created. |
| `sqla_async_database_url` | session | config | Its async twin, through `sqla_async_url`'s driver. |
| `sqla_metadata` | session | config | The metadata to create, from the `sqla_metadata` ini key or an override. |
| `sqla_engine_options` | session | config | Keyword arguments for every engine. Default `{}`. |
| `sqla_session_options` | session | config | Keyword arguments for the sessionmakers. Default `{}`. |
| `sqla_schema` | session | config | Creates the tables once, drops them at the end; with `sqla_schema = alembic`, your migrations build them. |
| `sqla_alembic_config` | session | config | The `alembic.config.Config` that `sqla_schema = alembic` runs, from the `sqla_alembic_config` ini key or an override. |
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
| Schema mode | | | `sqla_schema = create`, `none` or `alembic` | `create` |
| Alembic config file, from the rootdir | | | `sqla_alembic_config` | `alembic.ini` |
| Alembic revisions to upgrade to | | | `sqla_alembic_target` | `heads` |
| The URL env.py gets, `sync` or `async` | | | `sqla_alembic_url` | `sync` when `sqla_url` is set |
| A database of its own | | | `sqla_database = configured`, `run` or `worker` | `configured` |
| Lock, DDL and teardown timeout, seconds | | | `sqla_lock_timeout` | `30` |
| Replace a crashed run's schema | `--sqla-recreate-schema` | | | off |
| Keep the databases `run` and `worker` create | `--sqla-keep-database` | | | off |

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
- **Checked before the run.** The URLs, the schema and database modes, the
  timeout, the shape of `sqla_metadata`, the names `sqla_database` derives
  and whether `sqla_schema = alembic` may migrate the database are checked at
  startup; a bad one stops the run with a usage error that
  never repeats the URL's password.
- **Inert unless asked.** A run whose tests never request an `sqla_*` fixture
  imports no application code, opens no connection, and never imports
  `sqlalchemy.ext.asyncio` or Alembic.
- **The report header says which database the run is configured for:**
  `sqla: url=postgresql+psycopg://app:***@localhost/app_test (from PYTEST_SQLA_URL), schema=create, database=configured`,
  and with `sqla_database = run`, `database=run (app_test_1a2b3c4d)`. It
  shows the configuration, read before any test runs, so an override of the
  `sqla_url` fixture, such as a container's URL, is not in it; the URL
  pytest-sqla connects to is `sqla_database_url`.

`sqla_engine_options` reaches every engine pytest-sqla creates: `echo`,
`connect_args`, `json_serializer`, `execution_options` and so on. It refuses
what would make a rollback impossible: `isolation_level="AUTOCOMMIT"` in any
spelling SQLAlchemy accepts, `autocommit=True` in `connect_args` for any driver,
and an `autocommit` query parameter in a server URL, which pymysql reads as
true even when it says `false`. It refuses
`isolation_level="READ UNCOMMITTED"`, which lets a test read another xdist
worker's uncommitted rows, and, for async engines, `poolclass` and `pool`,
which pytest-sqla picks per event loop. The same options reach the sync and the
async engines, so keep driver-specific `connect_args` to a URL whose driver
serves both.

Each test gets a DBAPI connection of its own: the sync engine is NullPool,
because ROLLBACK undoes rows but not a connection's session state, and a
pooled connection hands that state to the next test (measured: MySQL's `SET
FOREIGN_KEY_CHECKS = 0`, temporary tables and `sql_mode`, PostgreSQL's
session advisory locks and PREPAREd statements, SQLite's `PRAGMA
query_only`). Give `sqla_engine_options` a `poolclass` to pool the sync
engine anyway, and accept that state then carries over; the async engines of
wider loop scopes are pooled the same way (see Pooling). `pool_size`,
`max_overflow`, `pool_timeout` and `pool_use_lifo` reach only pooled engines;
the others leave them out.

`sqla_session_options` reaches the sessionmakers, so the test sessions can
behave like the application's: `expire_on_commit`, `autoflush`, `info`, or
`class_` for sqlmodel's `Session` and `AsyncSession`. It refuses `bind`,
`binds` and `join_transaction_mode`.

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
- **It notices a crashed run, or one still going.** A run killed before its
  teardown leaves the marker behind, and so does a run that is still running.
  The next run refuses that schema, says when and where the other run
  started, and suggests `--sqla-recreate-schema`, which drops and recreates
  it.
- **It never hangs** on PostgreSQL, MySQL, MariaDB and SQLite. Creation and
  drop wait at most `sqla_lock_timeout` seconds for a lock. A test that leaked
  a transaction on a table makes the drop fail with a message saying so, and
  recreating the schema of a run that is still going fails the same way,
  instead of blocking the run forever.

On MySQL, where DDL commits on its own, a creation that fails half way drops the
tables it had already created. A SQLite database pytest-sqla made up, an
in-memory one or a per-worker file, is discarded at the end instead of having
its tables dropped.

**`sqla_schema = none`** runs no DDL at all, for a database migrated before the
run, for example by `alembic upgrade head` in a CI step.

**`sqla_schema = alembic`** runs your Alembic migrations instead of
`create_all`, in a database pytest-sqla creates and drops, or an in-memory
SQLite one: see [Alembic](#alembic).

**Overriding the `sqla_schema` fixture** replaces all of it. Set
`sqla_schema = none` as well, so the URL is used exactly as configured, and use
a file or server database: an in-memory SQLite database exists only inside
pytest-sqla's own connections. Migrate `sqla_database_url`, the database
pytest-sqla connects to: the configured one, or the one `sqla_database = run`
created.

```python
import alembic.command
import alembic.config
import pytest


@pytest.fixture(scope="session")
def sqla_schema(sqla_database_url):
    config = alembic.config.Config("alembic.ini")
    # Alembic's config reads % as interpolation, and a rendered URL
    # percent-encodes its password, so every % is doubled.
    url = sqla_database_url.render_as_string(hide_password=False)
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    alembic.command.upgrade(config, "head")
    yield
    alembic.command.downgrade(config, "base")
```

The suite runs this block as written, against SQLite, against PostgreSQL with
a password that needs escaping, and into a database of its own on PostgreSQL,
which is dropped at the end, so there the downgrade only costs time. Under
pytest-xdist such an override runs once per worker, so it has to serialise
itself, unless `sqla_database = worker` gives each worker a database to
migrate on its own. `sqla_schema = alembic` does all of that for you, and
checks the result.

## A database of its own

**`sqla_database = run`** creates a database for the pytest run on the server
`sqla_url` points at, named after the database it names: for `.../app_test`,
`app_test_1a2b3c4d`, where `1a2b3c4d` are the first 8 hex digits of the run's
id. pytest-xdist workers share it: the first that needs it creates it, the
others join it, and the last one out drops it. **`sqla_database = worker`**
gives each xdist worker a database of its own, `app_test_1a2b3c4d_gw0`; without
xdist it is the run's. **`sqla_database = configured`**, the default, uses the
configured database itself.

```toml
[tool.pytest.ini_options]
sqla_database = "run"
sqla_metadata = "app.models:Base"
```

The configured database must exist: pytest-sqla connects there, with the URL's
credentials, to create and drop the run's, so on PostgreSQL it can be
`postgres`. The schema, its marker and every test's transaction then live in
the run's database, and `sqla_database_url` is its URL; the configured one is
never touched.

- **Privileges.** On PostgreSQL the role needs CREATEDB (`ALTER ROLE app
  CREATEDB`). On MySQL the user needs privileges on the names derived from the
  configured database: ``GRANT ALL ON `app\_test\_%`.* TO 'app'@'%'``. MySQL 8.4
  still applies the wildcard in such a grant, and deprecates it. Without them,
  every database test fails with a message that says what to grant.
- **It drops only what it created.** Right after CREATE, pytest-sqla marks the
  database as its own: on PostgreSQL with the database's comment; on MySQL,
  whose databases have no comment, with a one-row table inside it,
  `pytest_sqla_database`. It drops a database only when its name derives from
  this run's id, it carries the mark, and the mark names this run. A database
  with the run's name but not the mark is refused and left alone, and so is
  one whose mark changed during the run.
- **It never hangs.** Creating, joining and leaving run under a lock on the
  configured database, `pg_advisory_lock` or `GET_LOCK`, which waits at most
  `sqla_lock_timeout`, and so does the drop. Before it, pytest-sqla has closed
  every connection of its own, pooled ones included. On PostgreSQL 13 and
  later the drop is `WITH (FORCE)`, which also ends the connections of its
  role that your application leaked, and on older servers pytest-sqla ends
  them first. On
  MySQL a leaked connection in a transaction holds the drop for
  `sqla_lock_timeout`; the run then fails with SchemaConflictError, saying so,
  and the database stays.
- **Names are checked before the run.** PostgreSQL takes 63 bytes and
  truncates a longer name without an error, so two names could become one;
  MySQL takes 64 characters. A derived name that is too long is a usage error.
- **Keep it for a look.** `--sqla-keep-database` keeps the databases, tables
  included, and the end of the run names them, under xdist too.
- **A killed run leaves its database.** A worker killed with `kill -9` never
  leaves, so the database stays. When the next run creates its own, its
  summary names every database another pytest-sqla run left on the server, so
  you can drop them.
- **Two runs on one server at once** each get their own and both pass, where in
  configured mode the second fails with SchemaConflictError.
- **The cost** is one CREATE and one DROP per run, or per worker; `python
  scripts/benchmark.py --databases` measures it on your server.
- **No effect on SQLite.** In-memory databases are one per process already, and
  create mode gives each xdist worker a file of its own; the header says
  `database=run (no effect on SQLite)`. Any other dialect is refused.

The usual way to do this by hand, a session fixture that drops and recreates
one test database, breaks under pytest-xdist: every worker runs it and drops
the database the others are using. The suite runs that fixture as a control,
with four workers whose tests overlap: it fails, and the same tests pass with
`sqla_database = run`.

## Alembic

**`sqla_schema = alembic`** builds the schema with your Alembic migrations,
the way production gets it, instead of `create_all`. Install the `alembic`
extra. The migrations run only in a database pytest-sqla made and discards,
so one that fails, or stops half way, leaves nothing that anything uses:

- **PostgreSQL, MySQL and MariaDB, with `sqla_database = run` or `worker`.**
  The worker that creates the database migrates it right after `CREATE
  DATABASE`, under the database lock. The others wait on the lock, at most
  `sqla_lock_timeout`, and join a migrated database. A migration that fails
  drops the database again, so a worker that was waiting creates it once
  more, migrates it, and reports the same error itself. One that will not
  drop, such as on MySQL behind a transaction the migration left open, is
  marked as a failed migration's instead, and every worker refuses it, the
  error saying so. With `run`, raise `sqla_lock_timeout` above the time your
  migrations take, or use `worker`, where each worker migrates a database of
  its own.
- **SQLite in memory**, such as `sqla_url = sqlite://`, once per session. A
  file you named is refused: pytest-sqla never deletes it, so it could not
  give it back empty. So is `sqla_database = configured` on a server, for the
  same reason.

```toml
[tool.pytest.ini_options]
sqla_url = "sqlite://"
sqla_schema = "alembic"
sqla_database = "run"   # when PYTEST_SQLA_URL points at PostgreSQL or MySQL in CI
```

`sqla_metadata` is not needed, and ignored if set, so one configuration can
serve both modes: `-o sqla_schema=alembic` in a CI job runs the migrations,
and `create_all` stays the quick path on a laptop.

- **Your env.py as it is.** It gets what env.py files use:
  `config.attributes["connection"]`, a connection to the database through
  `sqla_url`'s driver when that driver is synchronous, and the
  `sqlalchemy.url` option, the same database. Alembic's templates build an
  engine from the URL; the cookbook's env.py uses the connection. The
  connection is handed over idle, with no transaction begun, so Alembic runs
  its transactions as on its command line, and `autocommit_block()` (CREATE
  INDEX CONCURRENTLY) works. The migrations run on a thread with no event
  loop, so the async template's `asyncio.run()` works too.
- **`sqla_alembic_url`** picks the URL env.py gets: `sync`, through
  `sqla_url`'s driver, the default when `sqla_url` is set, or `async`,
  through `sqla_async_url`'s. A synchronous env.py needs a synchronous driver
  and the async template an async one; the wrong pair fails with a message
  that says which value to set. With only an async driver configured, env.py
  gets the URL alone.
- **Nothing else gets migrated.** While the migrations and the check run, a
  connection they open to any other database is refused before it is used,
  such as an env.py that prefers the application's own `DATABASE_URL`, and
  so is one SQLAlchemy did not make from its connect arguments, such as an
  engine with `creator=`, or one whose arguments a `do_connect` hook changed
  after pytest-sqla read them. Connections other threads open meanwhile pass.
  What it cannot see is a `do_connect` hook that runs after its own and
  returns a connection it made itself.
- **The configuration** is `sqla_alembic_config`, relative to the rootdir,
  `alembic.ini` by default; the `pyproject.toml` next to it is read too, for
  what its `[tool.alembic]` table sets, and a `.toml` path is a
  pyproject.toml alone. Relative `script_location`, `prepend_sys_path` and
  `version_locations` are read from that file's directory, as `%(here)s`
  would be, from whatever directory pytest runs; package resources
  (`package:dir`) and absolute paths stay as they are. To build the `Config`
  in Python, override the `sqla_alembic_config` fixture: pytest-sqla sets the
  URL and the connection on whatever it returns, and reads relative paths
  from the rootdir when it has no file.
- **Targets.** `sqla_alembic_target`, `heads` by default, is one or more
  revisions separated by spaces, upgraded to one after another: `pre@head
  post@head` for two branches. Afterwards pytest-sqla reads the heads back
  through env.py, as `alembic current` does, a custom `version_table`
  included, and fails when a target was not reached. A script directory
  without revisions is refused, since a wrong `version_locations` would
  otherwise migrate nothing at all, and so is a relative target such as `+1`,
  which that check cannot follow.
- **Logging and `sys.path` as they were.** env.py's `fileConfig()` closes
  pytest's log handlers and disables every logger its file does not name.
  pytest-sqla puts the configuration back afterwards and reopens the log
  files, so caplog and `--log-file` go on working, and takes the
  `prepend_sys_path` entries out of `sys.path` again.
- **Errors** say what to do. A migration that raises reaches you as it
  raised, with its own traceback. A legacy `alembic.ini` without
  `path_separator` gets Alembic's own DeprecationWarning, which says what to
  add, as on its command line; under `filterwarnings = error` it fails the
  run. SQLAlchemy before 2.0.24 changes `%XX` in a password when it writes a
  URL out, so there such a URL is refused rather than handed to env.py with
  another password.
- **The rest stays the same.** `sqla_database_url` is the migrated database.
  `--sqla-keep-database` keeps it, migrated; a database whose migration
  failed is dropped all the same, because a waiting worker would otherwise
  join it half migrated. `--sqla-recreate-schema` changes nothing, the
  database being new every time. There is no downgrade: the database goes at
  the end.

On MySQL the database also holds pytest-sqla's mark, the
`pytest_sqla_database` table, next to your migrations' tables and
`alembic_version`: a test that compares your models with the migrated
database, such as one built on `compare_metadata`, should leave it out with
its `include_object`. [The example](https://github.com/sqla-native/pytest-sqla/tree/main/examples/alembic_app)
runs in CI on SQLite, PostgreSQL and MySQL.

## Async details

- **pytest-asyncio, strict or auto mode.** In strict mode, mark async tests
  with `@pytest.mark.asyncio`; pytest-sqla's async fixtures need no marking,
  because they are plain sync fixtures that hand you objects opened on the
  right loop. A plain `def` test cannot use them, even through an async
  fixture: there is no test loop to open the connection on.
- **Every loop scope.** `@pytest.mark.asyncio(loop_scope="module")`, the
  `asyncio_default_test_loop_scope` ini key, class, package and session loops
  all work: pytest-sqla reads the test's loop scope and opens its connection on
  that loop.
- **anyio.** `@pytest.mark.anyio` tests work with and without pytest-asyncio
  installed, with pytest-asyncio in strict mode. In auto mode pytest-asyncio
  would run an anyio test's async fixtures on its own loop, so pytest-sqla
  refuses that combination with an explanation. SQLAlchemy's asyncio extension
  runs only on asyncio, so the trio backend is refused too. A pytest-asyncio
  test that has `anyio_backend` among its fixtures, typically through a helper
  fixture written for anyio tests, is still a pytest-asyncio test. Whether
  anyio then runs its async fixtures, pytest-sqla's included, on a loop of its
  own depends on which of the two plugins registered first; when it does, the
  loop guard says so. Keep `anyio_backend` out of such tests.
- **The loop guard.** Using a test's connection from another event loop raises
  `LoopMismatchError` before the driver sees the statement, a COMMIT or a
  ROLLBACK, and the connection stays usable. Measured without the guard:
  asyncpg, aiomysql and asyncmy fail with "attached to a different loop", and
  asyncpg's connection is unusable afterwards; psycopg happens to survive, so
  the guard treats every driver alike. The two usual causes are an async
  fixture on a different loop scope than the test (give it the test's loop
  scope), and application code that runs in its own thread with its own loop,
  such as Starlette's TestClient (use `httpx.AsyncClient` with
  `ASGITransport`).
- **Tasks the test leaves running.** A connection serves one operation at a
  time, and a task the test started and never awaited races the teardown on
  it. Measured: asyncmy hung the run forever, and the other drivers failed the
  teardown with their own errors. pytest-sqla notices a task that used the
  connection and is still running, closes the connection instead of rolling
  it back next to that task (the database then rolls the test back), and fails
  the test with IsolationError naming it. The teardown as a whole is bounded
  by `sqla_lock_timeout` the same way. Await or cancel such tasks before the
  test ends.
- **Pooling.** For a per-test loop, the pytest-asyncio default, and for anyio,
  the async engine is NullPool, which SQLAlchemy's asyncio docs recommend when
  one engine serves several event loops. For a class, module, package or
  session loop, the engine has a real pool that lives exactly as long as that
  loop, and the tests on that loop share its connections: session state one of
  them sets, such as `SET FOREIGN_KEY_CHECKS = 0` or a session advisory lock,
  reaches the next. A connection whose transaction broke (an IsolationError) is
  discarded rather than handed on. The sync engine is NullPool too, unless you
  give it a `poolclass`.

What NullPool costs, measured with
[`scripts/benchmark.py`](https://github.com/sqla-native/pytest-sqla/blob/main/scripts/benchmark.py)
on localhost Docker (milliseconds per test, best of 5 runs of 200 isolation
cycles; re-measure on your own machine before relying on them):

| Driver | Pooled | NullPool |
| --- | ---: | ---: |
| asyncpg | 1.56 | 6.95 |
| psycopg async | 1.57 | 5.15 |
| aiomysql | 1.97 | 3.12 |
| asyncmy | 1.98 | 3.18 |
| psycopg (sync) | 1.33 | 5.43 |
| pymysql (sync) | 1.24 | 6.54 |

If those milliseconds matter more than session state, give the async database
tests a wider loop scope, together with their fixtures':

```ini
asyncio_default_test_loop_scope = module
asyncio_default_fixture_loop_scope = module
```

An async fixture left on the per-test loop while its tests run on the module
loop uses their connection from another loop, and the loop guard stops it.
For sync tests, `sqla_engine_options` with a `poolclass` is the same trade.

pytest-asyncio 1.2 is the floor: on 1.0 and 1.1, a test with a session loop
that ran after tests with a module loop was measured running on a different
loop than its own scope's fixtures.

## SQLite notes

- **The savepoint fix.** pytest-sqla installs SQLAlchemy's documented connect
  and begin listeners on every SQLite engine it creates, sync and async. With
  `connect_args={"autocommit": False}` (Python 3.12+, PEP 249 mode) sqlite3
  handles transactions correctly by itself, and pytest-sqla adds only the write
  lock below. When a future Python makes that the default, the fix switches
  itself off.
- **In-memory databases** (`sqlite://`, `sqlite:///:memory:`,
  `sqlite+aiosqlite://`) become one named database on SQLite's memdb VFS for
  the whole session, held open by a keeper connection, and shared between the
  sync and async engines. That needs SQLite 3.36 (2021) or newer.
  Shared-cache mode is not used: SQLite calls it obsolete, and it fails at
  once where memdb waits for a lock like a file does. An in-memory URI of
  your own (`uri=true`) is used as given, plus `check_same_thread=False` on
  pysqlite, so a test can use it from a thread. SQLite shares
  `file:/NAME?vfs=memdb`, and any of them with `cache=shared`, between
  connections; `file:NAME?mode=memory`, `file::memory:` and
  `file:NAME?vfs=memdb` give each connection its own empty database, as does
  `mode=memory` next to `vfs=memdb`, so `sqla_schema = create` and `alembic`
  refuse them.
  With `sqla_schema = none`, each test's connection has one of its own.
- **Files.** A test's transaction on a SQLite file begins `IMMEDIATE`, in PEP
  249 mode too, so another process waits for it instead of failing with
  "database is locked". Under pytest-xdist in create mode, each worker gets a
  file of its own next to the configured one, `test-gw0.db`, which pytest-sqla
  deletes afterwards; it never deletes a file you named. A `file:` URI URL
  (`uri=true`) is not split per worker.
- **One file, several workers.** In none mode the workers share the file, and
  since every test holds the write lock until its teardown, read-only tests
  included, they run one at a time: no parallelism. A worker waits for the lock
  at most sqlite3's busy timeout, 5 seconds by default, so set
  `connect_args={"timeout": ...}` above (workers - 1) times your longest test,
  or give each worker its own file with `sqla_schema = create`.
- **A raw `COMMIT` or sqlite3's `executescript()`** inside a test ends the
  transaction, and the test fails at teardown with IsolationError.
- **An unread result in a failing test.** On Python 3.11 and later, a query
  result left unread keeps a SQLite read lock until it is garbage-collected,
  and pytest keeps the last failure's traceback. On a file you named, that can
  block the drop at the end of the run; pytest-sqla then says so. A query that
  a test cancels, for example with `asyncio.wait_for`, kept the write lock the
  same way on aiosqlite; pytest-sqla collects it at that test's teardown.

## MySQL notes

MySQL commits implicitly on DDL (`CREATE`, `ALTER`, `DROP`, `TRUNCATE`), on
`LOCK TABLES` and on `SET autocommit = 1`, which ends the test's transaction:
the rows the test wrote before that point may now be permanent. pytest-sqla
notices, because its guard savepoint is gone, fails that test with
IsolationError, and discards the connection. Run such statements outside the
sqla fixtures.

A deadlock is not a breach. InnoDB rolls the victim's whole transaction back,
savepoints included, and with it the rows the test seeded before the
deadlock; nothing was committed. Without help, SQLAlchemy's next rollback to a
savepoint would then fail with "(1305, 'SAVEPOINT sa_savepoint_2 does not
exist')", which the application would see instead of the deadlock. So after a
deadlock (1213) pytest-sqla re-creates the savepoints SQLAlchemy still holds
before its next savepoint operation: the application sees 1213 as it would in
production, its `rollback()` works, a retry commits, and the guard watches
the rest of the test. With `innodb_rollback_on_timeout = ON`, a lock wait
timeout (1205) rolls the transaction back too, and pytest-sqla reports it as a
breach; that server setting is not tested.

MariaDB uses the same code path but is not tested in CI. Non-transactional
tables (MyISAM, MEMORY) cannot be rolled back at all.

## pytest-xdist

PostgreSQL and MySQL workers of one run share one database, the configured one
or, with `sqla_database = run`, the run's own; with `sqla_database = worker`
each has its own. SQLite workers each get their own, a file per worker in
create mode and an in-memory database per process. Under the default isolation levels a worker never reads another
worker's rows, because no test commits; pytest-sqla refuses `READ UNCOMMITTED`
for that reason. The schema is created by the first worker under a database
lock (`pg_advisory_xact_lock` on PostgreSQL, `GET_LOCK` on MySQL), joined by
the others, and dropped by the last one out; on SQLite, `BEGIN IMMEDIATE` only
orders separate runs. Measured before this design: a `create_all` per worker
with `-n 4` ended "2 passed, 6 errors" on every run against both servers.

Limits:

- Two workers inserting the same unique value wait on each other until one
  test ends, and two that insert the same values in opposite orders deadlock:
  measured on every run, with PostgreSQL's DeadlockDetected and MySQL's 1213.
  Make such values unique per test, or insert them in one order.
- One run per configured database: a second pytest run on the same database
  while the first is going gets SchemaConflictError. `sqla_database = run`
  gives each run its own.
- A worker that crashes never leaves, so its run's tables stay behind, a
  per-worker SQLite file included, and the next run asks for
  `--sqla-recreate-schema`. With `sqla_database = run` or `worker` the run's
  database stays instead, and the next run names it.
- Schema locks exist for PostgreSQL, MySQL, MariaDB and SQLite. Any other
  dialect works in a single process; under xdist, create the schema beforehand
  and set `sqla_schema = none`.

## What pytest-sqla reports

pytest-sqla never warns: suites run with `filterwarnings = error`, so every
problem is an error with a message that says what to do. Setup problems (no
URL, the wrong driver family, an async fixture in a sync test) fail the test at
setup. Three exception classes, all subclasses of `pytest_sqla.PytestSqlaError`:

- **IsolationError**, at the teardown of the test that broke isolation: it
  committed the connection itself (`session.connection().commit()`), the
  database ended the transaction on its own, or it committed on a second
  connection of pytest-sqla's engine (`sqla_session.get_bind().engine.begin()`).
  The same class reports a task the test left running on its connection, and
  a teardown that did not finish within `sqla_lock_timeout`. A plain
  `connection.rollback()` escapes nothing and is not an error, and neither is
  a MySQL deadlock.
- **LoopMismatchError**, a `RuntimeError` too, at the first statement, COMMIT
  or ROLLBACK run on the wrong event loop.
- **SchemaConflictError**, from `sqla_schema`: on every database test for
  tables it did not create, another run's schema, or a lock that timed out
  during creation; once, at the end of the run, for a drop blocked by a leaked
  transaction. With `sqla_database = run` or `worker`, also for a database it
  may not create, one with the run's name that is not its own, and its lock or
  its drop timing out. With `sqla_schema = alembic`, also for a connection of
  the migrations to another database, a target the database did not reach, a
  script directory without revisions, a target Alembic cannot resolve, and an
  env.py whose engine does not suit the URL's driver.

A test's own failure always wins: a breach found at teardown is reported as a
separate error and never replaces it. URLs in messages always hide the
password.

## Migrating

| From | Was | Now |
| --- | --- | --- |
| pytest-async-sqlalchemy | `--database-url`, `database_url` | `--sqla-url`, `sqla_url` |
| | `db_session`, `dbsession` | `sqla_async_session` |
| | `_database_url` | override `sqla_url` |
| | `init_database` | `sqla_metadata` |
| | your `event_loop` fixture override | delete it |
| pytest-sqlalchemy | `--sqlalchemy-connect-url` | `--sqla-url` |
| | `connection`, `transaction` | `sqla_connection` |
| | `dbsession` | `sqla_session` |
| | `--sqlalchemy-manage-db`, `db_schema` | `sqla_database = worker`, and `sqla_schema` for the tables |
| sqlalchemyseed | your `engine` fixture, the SQLite listeners, StaticPool | `sqla_url`; delete the rest |
| | `sqlalchemyseed_session` | override it to return `sqla_session`, and keep `seed` |

pytest-sqlalchemy's `db_schema` and pytest-async-sqlalchemy's `database`
fixture created and dropped the database, per xdist worker in the former's
case. `sqla_database = worker` does that, `run` creates one for the whole run,
and `sqla_url` then names a database the user can connect to, such as
`postgres`. In configured mode, the default, pytest-sqla never creates or drops
a database: create it before the run, or supply it from a container fixture
as below.

## Using it with database fixtures

`sqla_url` is an ordinary session fixture, so a container or server fixture
can supply it, as a str or a URL:

```python
import pytest
from testcontainers.community.postgres import PostgresContainer


@pytest.fixture(scope="session")
def sqla_url():
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        yield postgres.get_connection_url()
```

Everything that depends on `sqla_url`, the schema's drop included, is torn
down before the container stops. pytest-databases and pytest-postgresql
fixtures plug in the same way. With `sqla_database = run` or `worker`, the
override is the server and the name to derive from: pytest-sqla creates its
database next to the container's.

## Limitations

- Code that builds its own engine or sessionmaker, instead of the one you
  inject, works outside the test transaction, and pytest-sqla cannot roll it
  back; it cannot even notice. Code that opens a second connection from
  pytest-sqla's own engine commits for real too, but that is reported.
- One connection per test: running two sessions concurrently, as with
  `asyncio.gather`, fails as it would on one connection in production.
  Requests within one test must run one after another, and every task that
  uses the connection must be done before the test ends.
- Isolation levels are the engine's: application code that asks for its own
  per unit of work, as with
  `session.connection(execution_options={"isolation_level": "SERIALIZABLE"})`,
  gets SQLAlchemy's InvalidRequestError, because the test's transaction has
  already begun.
- Sequences and AUTO_INCREMENT counters are not transactional on PostgreSQL
  and MySQL: a rolled-back insert still uses up its id, so ids keep growing
  from test to test, while SQLite reuses them. Do not assert ids.
- Session state survives a rollback on a pooled connection: a `poolclass` of
  your own, or a class, module, package or session loop, shares connections
  between tests (see Pooling).
- CREATE SCHEMA for schema-qualified metadata is up to you.
- On MySQL a database `sqla_database` created holds one table more than your
  schema, its mark, `pytest_sqla_database`, and pytest-sqla refuses a metadata
  table of that name. With `sqla_schema = alembic` the mark is there while
  your migrations run.
- `sqla_schema = alembic` migrates one database, upward only: no downgrade,
  no multi-database env.py, and no SQLite file.

## Compatibility

- Python 3.11 to 3.15.
- SQLAlchemy 2.0 and 2.1; pytest 8.2 or newer; pytest-asyncio 1.2 or newer;
  Alembic 1.16 or newer, for `sqla_schema = alembic`.
- Tested in CI on PostgreSQL 17 with psycopg 3 (sync and async) and asyncpg,
  on MySQL 8.4 with pymysql, aiomysql and asyncmy, and on SQLite with pysqlite
  and aiosqlite. MariaDB uses the MySQL code path and is untested.

## Roadmap

Data seeded once per module inside an outer savepoint; rebinding an
application's own sessionmaker; factory_boy and polyfactory helpers; a docs
site.

## License

MIT. pytest-sqla is not affiliated with the SQLAlchemy project.

[recipe]: https://docs.sqlalchemy.org/en/20/orm/session_transaction.html#joining-a-session-into-an-external-transaction-such-as-for-test-suites
