# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **A database of pytest-sqla's own, per run or per xdist worker.**
  `sqla_database = run` creates one for the pytest run on the configured
  PostgreSQL or MySQL server, named after the configured database,
  `app_test_1a2b3c4d`; its xdist workers share it, and the last one out drops
  it. `sqla_database = worker` gives each worker its own,
  `app_test_1a2b3c4d_gw0`. pytest-sqla connects to the configured database to
  create and drop it, under a lock bounded by `sqla_lock_timeout`, marks it as
  its own (the database's comment on PostgreSQL, a `pytest_sqla_database`
  table on MySQL), and drops it only when its name, its mark and the mark's
  run id are this run's; PostgreSQL 13 and later drop `WITH (FORCE)`. A
  missing CREATEDB or CREATE privilege, a name the server would not take
  whole, and a database with the run's name that is not its own each get a
  message that says what to do. `sqla_database = configured`, the default, is
  the behaviour so far; on SQLite the new modes change nothing.
- **`--sqla-keep-database`** keeps those databases and names them at the end
  of the run, from every xdist worker. When a run creates its database, its
  summary also names the databases other pytest-sqla runs left on the server,
  such as a killed run's.
- **`sqla_database_url` and `sqla_async_database_url`**, the URLs pytest-sqla
  connects to, for your own tools: `sqla_url` and `sqla_async_url` in
  configured mode on a server.
- **`sqla_schema = alembic`: your Alembic migrations build the schema**, in
  place of `create_all`, and only in a database pytest-sqla made and
  discards. With `sqla_database = run` or `worker` on PostgreSQL, MySQL and
  MariaDB, the worker that creates the database migrates it right then,
  under the database lock, and the others join it migrated; on SQLite, an
  in-memory database is migrated once per session. A migration that fails
  drops its database again, so no worker joins one half migrated, and a
  waiting worker that tries again reports the same error. A configured
  database or a SQLite file is a usage error, and so is an in-memory URI that
  gives each connection its own database, as in create mode. `sqla_metadata`
  is ignored, so one configuration serves both modes.
- **env.py runs as it is.** It gets an idle connection, as
  `config.attributes["connection"]`, when a synchronous driver is
  configured, and the database's URL as `sqlalchemy.url`; the migrations run
  on a thread with no event loop, so Alembic's async template works, and
  `autocommit_block()` runs CREATE INDEX CONCURRENTLY as in production.
  While they run, a connection to any other database, or one SQLAlchemy did
  not make from its connect arguments, is refused. Afterwards pytest-sqla
  restores logging, which env.py's `fileConfig()` breaks for caplog and
  `--log-file`, and `sys.path`, and reads the heads back through env.py to
  check that every target was reached. Relative paths in the configuration
  are read from its file's directory; a script directory without revisions,
  a target Alembic cannot resolve, and an env.py whose engine does not suit
  its URL's driver each get a message that says what to do.
- **`sqla_alembic_config`** (the config file, relative to the rootdir,
  `alembic.ini` by default, or a `pyproject.toml`), **`sqla_alembic_target`**
  (`heads` by default) and **`sqla_alembic_url`** (`sync` or `async`, the
  driver of the URL env.py gets), and the **`sqla_alembic_config` fixture**,
  to build the `Config` in Python.
- **The `alembic` extra**, `alembic>=1.16`, imported only in this mode:
  without it, `sqla_schema = alembic` is a usage error that names the extra.

### Changed

- **The report header ends with `database=`**: the mode, and the database's
  name when the configuration determines it. Under pytest-xdist the
  controller now hands its run id to every worker, so the header it prints
  names the databases they use.
- **The README's Alembic recipe migrates `sqla_database_url`**, so it works in
  every mode, and its block no longer needs `make_url`.
- `sqla_metadata` may not define a table named `pytest_sqla_database`.

### Fixed

- **An in-memory SQLite URI that gives each connection a database of its own
  is refused in create mode**, before the run, instead of failing every test
  that touches a table with "no such table". pytest-sqla took any URI with
  `mode=memory`, `vfs=memdb` or `file::memory:` for one that connections
  share, and created the schema in a database no test connection opened.
  Measured with two connections to one URI, SQLite shares
  `file:/NAME?vfs=memdb` and any of them with `cache=shared`, while
  `file:NAME?mode=memory`, `file::memory:`, `file:NAME?vfs=memdb` and
  `mode=memory` next to `vfs=memdb`, leading slash or not, give each
  connection a new, empty one. The usage error names the forms that work:
  `sqlite://`, `file:/NAME?vfs=memdb` without `mode=memory`, or
  `cache=shared`. An overridden `sqla_url` fixture gets the same message, in
  the tests that ask for it. With `sqla_schema = none` such a URL still works,
  each test's connection with a database of its own, and pytest-sqla no
  longer holds one more open for the whole session.

## [0.0.3] - 2026-11-20

### Fixed

- **A `sqla_lock_timeout` that is not a number names the option again on
  pytest 9.2.** pytest 9.2.0 converts the value itself and raises its own
  usage error, before pytest-sqla reads it, and from an ini file that error
  did not say which option it was about: `ERROR: could not convert string to
  float: 'soon'`. pytest-sqla now puts its own message around it, as on
  earlier pytest versions: `pytest-sqla: sqla_lock_timeout must be a positive
  number of seconds: could not convert string to float: 'soon'.`

## [0.0.2] - 2026-10-21

### Added

- **Python 3.15**, in the test matrix with 3.11 to 3.14.

### Removed

- **Python 3.10**, which reached its end of life on 2026-10-01.
  `requires-python` is now `>=3.11`, so pip keeps a Python 3.10 project on
  pytest-sqla 0.0.1.

## [0.0.1] - 2026-10-18

Initial release.

### Added

- **Per-test isolation, sync and async.** `sqla_connection`,
  `sqla_sessionmaker` and `sqla_session`, and their async twins
  `sqla_async_connection`, `sqla_async_sessionmaker` and `sqla_async_session`.
  Each test gets one connection inside an outer transaction and a guard
  SAVEPOINT; sessions join it with `join_transaction_mode="create_savepoint"`,
  so the code under test can commit, roll back and close freely, and everything
  is rolled back at teardown. The connection is a DBAPI connection of its own,
  NullPool unless `sqla_engine_options` choose a pool, so session state that
  ROLLBACK does not undo (`SET FOREIGN_KEY_CHECKS = 0`, advisory locks,
  temporary tables, PRAGMAs) cannot reach the next test.
- **Breach detection.** A commit of the test's connection itself, a commit on
  a second connection of pytest-sqla's own engine, and a transaction the
  database ended on its own (MySQL's implicit commits after DDL, TRUNCATE,
  LOCK TABLES and `SET autocommit = 1`; sqlite3's `executescript()`; a raw
  `COMMIT` or `ROLLBACK`), fail the test that caused them with
  `IsolationError`, and the connection is discarded rather than pooled. A
  plain `connection.rollback()` escapes nothing and is not an error, and
  neither is a MySQL deadlock: the savepoints InnoDB rolled back are re-created,
  so the application sees the deadlock (1213), as in production.
- **The test's own event loop.** The async fixtures open their connection on
  the loop the test runs on, under every pytest-asyncio loop scope, in strict
  and auto mode, and under anyio; a pytest-asyncio test stays pytest-asyncio's
  even with `anyio_backend` among its fixtures. A per-test loop gets a NullPool
  engine; a class, module, package or session loop gets a pool that lives
  exactly as long as that loop. Using a connection from another loop raises
  `LoopMismatchError` before the driver sees the statement, COMMIT or ROLLBACK.
- **Tasks the test leaves running.** A task that used the test's connection
  and is still running at teardown is reported, and the connection is closed
  instead of rolled back next to it; the teardown is bounded by
  `sqla_lock_timeout`. Measured before: asyncmy hung the run.
- **The schema, once per run.** `sqla_schema` creates the tables of
  `sqla_metadata` before the first database test and drops them after the
  last, under a database-level lock shared by pytest-xdist workers
  (`pg_advisory_xact_lock`, `GET_LOCK`, `BEGIN IMMEDIATE`), owned through a
  `pytest_sqla` marker table. It refuses tables it did not create and the
  schema of another run (`SchemaConflictError`), recovers a crashed run's with
  `--sqla-recreate-schema`, and bounds every lock wait with
  `sqla_lock_timeout`, creation included. `sqla_schema = none` runs no DDL.
- **SQLite done right.** SQLAlchemy's documented savepoint fix on every SQLite
  engine, unless sqlite3 runs in PEP 249 mode; in-memory URLs shared between
  the sync and async engines through the memdb VFS; one file per xdist worker
  in create mode; `BEGIN IMMEDIATE` on files, in PEP 249 mode too, so another
  process waits instead of failing. A Python built without `sqlite3` still
  loads the plugin.
- **Configuration** through `--sqla-url` / `PYTEST_SQLA_URL` / `sqla_url` and
  the async equivalents, in that precedence, checked at startup without ever
  echoing a password, plus `sqla_engine_options` and `sqla_session_options`.
  No URL means the database tests fail with instructions; there is no silent
  SQLite fallback. `sqla_engine_options` refuses `AUTOCOMMIT` in any spelling,
  `autocommit` in `connect_args` or a server URL, and `READ UNCOMMITTED`;
  pool sizing options reach only pooled engines. `sqla_lock_timeout` takes a
  number in pytest 9's native TOML configuration.

[0.0.3]: https://github.com/sqla-native/pytest-sqla/releases/tag/v0.0.3
[0.0.2]: https://github.com/sqla-native/pytest-sqla/releases/tag/v0.0.2
[0.0.1]: https://github.com/sqla-native/pytest-sqla/releases/tag/v0.0.1
