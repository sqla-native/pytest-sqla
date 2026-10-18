# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.0.1] - 2026-10-18

Initial release.

### Added

- **Per-test isolation, sync and async.** `sqla_connection`,
  `sqla_sessionmaker` and `sqla_session`, and their async twins
  `sqla_async_connection`, `sqla_async_sessionmaker` and `sqla_async_session`.
  Each test gets one connection inside an outer transaction and a guard
  SAVEPOINT; sessions join it with `join_transaction_mode="create_savepoint"`,
  so the code under test can commit, roll back and close freely, and everything
  is rolled back at teardown.
- **Breach detection.** A commit of the test's connection itself, and a
  transaction the database ended on its own (MySQL's implicit commits after
  DDL, TRUNCATE and LOCK TABLES; sqlite3's `executescript()` and a raw
  `COMMIT`), fail the test that caused them with `IsolationError`. A plain
  `connection.rollback()` escapes nothing and is not an error.
- **The test's own event loop.** The async fixtures open their connection on
  the loop the test runs on, under every pytest-asyncio loop scope, in strict
  and auto mode, and under anyio. A per-test loop gets a NullPool engine; a
  class, module, package or session loop gets a pool that lives exactly as
  long as that loop. Using a connection from another loop raises
  `LoopMismatchError` before the driver sees the statement.
- **The schema, once per run.** `sqla_schema` creates the tables of
  `sqla_metadata` before the first database test and drops them after the
  last, under a database-level lock shared by pytest-xdist workers
  (`pg_advisory_xact_lock`, `GET_LOCK`, `BEGIN IMMEDIATE`), owned through a
  `pytest_sqla` marker table. It refuses tables it did not create and the
  schema of another run (`SchemaConflictError`), recovers a crashed run's with
  `--sqla-recreate-schema`, and bounds every lock wait with
  `sqla_lock_timeout`. `sqla_schema = none` runs no DDL.
- **SQLite done right.** SQLAlchemy's documented savepoint fix on every SQLite
  engine, unless sqlite3 runs in PEP 249 mode; in-memory URLs shared between
  the sync and async engines through the memdb VFS; one file per xdist worker
  in create mode; `BEGIN IMMEDIATE` on files, so another process waits instead
  of failing.
- **Configuration** through `--sqla-url` / `PYTEST_SQLA_URL` / `sqla_url` and
  the async equivalents, in that precedence, checked at startup without ever
  echoing a password, plus `sqla_engine_options` and `sqla_session_options`.
  No URL means the database tests fail with instructions; there is no silent
  SQLite fallback.

[Unreleased]: https://github.com/sqla-native/pytest-sqla/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/sqla-native/pytest-sqla/releases/tag/v0.0.1
