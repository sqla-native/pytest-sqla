# GUIDELINES_PYTEST_SQLA.md

## Core Philosophy — trustworthy isolation, or a loud failure

`pytest-sqla` gives every test a SQLAlchemy connection, session and
sessionmaker whose work is rolled back, sync and async, even when the code
under test commits. It is a **testing** plugin: it never runs in production,
never rewrites queries, and never patches SQLAlchemy or the application.

A test plugin that isolates *most* of the time is worse than none: the leak
it lets through surfaces as a flaky failure in some later, unrelated test, and
nobody suspects the fixture. So the rule behind everything below is binary:
either the test's writes are provably rolled back, or the test that broke
isolation fails, at its own teardown, saying why.

---

### 1. Architecture assumptions (never break these)

- **`join_transaction_mode="create_savepoint"`, always, plus a guard
  SAVEPOINT.** The default, `conditional_savepoint`, becomes `rollback_only` on
  a connection that is already in a transaction. Measured on PostgreSQL 17,
  MySQL 8.4 and SQLite, SQLAlchemy 2.0 and 2.1 (`tests/sync/test_recipe.py`
  and its real-database twin pin it):

  | Join mode | Seeded row after the app's rollback | Rows left after teardown |
  | --- | --- | --- |
  | `conditional_savepoint` (the default) | 0: the app rolled back the test | 1: its next commit escaped |
  | `create_savepoint` | 1 | 0 |

  The guard SAVEPOINT right after `BEGIN` is what notices the database ending
  the transaction on its own, and it makes even an application's own
  `Session(bind=connection)` with the default mode create a savepoint.

- **Only the guard decides a breach, and only a root commit is one.** The
  teardown rolls back every savepoint inside the guard, innermost first, then
  the guard. An inner savepoint that is already gone is not a breach (closing
  sessions out of order discards the later ones), a disconnect is not one (the
  server rolled back), and a root `rollback()` is not one (nothing escaped).
  Never call `rollback()` on a nested transaction that is not the current one:
  SQLAlchemy warns "nested transaction already deassociated", and that warning
  is an error in every suite that uses `filterwarnings = error`.

- **SQLite gets SQLAlchemy's documented fix on every engine.** In sqlite3's
  legacy transaction control (Python's default through 3.15) a SAVEPOINT
  emitted before `BEGIN` does not join the transaction. Measured on pysqlite
  and aiosqlite, Python 3.11 to 3.13, SQLAlchemy 2.0.0, 2.0.55 and 2.1.5:

  | Fix | Committed rows left after the outer rollback |
  | --- | --- |
  | none | 1 |
  | connect/begin listeners | 0 |
  | `connect_args={"autocommit": False}` (3.12+) | 0 |

  `tests/sync/test_sqlite.py` keeps the unfixed case as a control, so the
  plugin's own tests prove something. With `autocommit=False` the plugin adds
  no listeners (a second `BEGIN` would fail), and the legacy check follows the
  runtime default, so the fix switches itself off when Python flips it.

- **Shared in-memory SQLite is memdb, never shared cache.** One memdb name per
  session, shared by the sync engine, the async engines and the schema
  connection, held open by a keeper. Shared-cache mode is "obsolete" and
  "discouraged" (sqlite.org), and measured, it fails at once where memdb waits
  out the busy timeout like a file.

- **Files begin IMMEDIATE.** A deferred transaction that has read and then
  writes while another process holds the write lock gets "database is locked"
  at once, because SQLite refuses the lock upgrade rather than deadlock
  (measured: two xdist workers sharing a file in none mode). An immediate one
  waits.

- **Every async object lives on the test's own event loop.** pytest-asyncio
  picks a fixture's loop and a test's loop independently, and nothing compares
  them. Measured on SQLAlchemy 2.1.5, a connection used from a second loop:

  | Driver | Crossing loops | The connection afterwards |
  | --- | --- | --- |
  | asyncpg 0.32 | RuntimeError "attached to a different loop" | unusable ("another operation is in progress") |
  | aiomysql 0.3.2 | RuntimeError "attached to a different loop" | usable in that probe |
  | asyncmy 0.2.16 | RuntimeError "attached to a different loop" | usable in that probe |
  | psycopg 3.3 async | works | usable |
  | aiosqlite 0.22 | works | usable |

  So the public async fixtures are **sync dispatchers**: each reads the test
  item and requests an internal fixture declared with
  `pytest_asyncio.fixture(loop_scope=S)` for the test's scope, or a plain async
  fixture that anyio runs in the test's runner. A loop guard on every
  connection raises `LoopMismatchError` before the driver sees a cross-loop
  statement, for every driver alike. Never call `getfixturevalue()` on an async
  fixture from inside a running async fixture ("Runner.run() cannot be called
  from a running event loop").

- **An engine shared across loops is NullPool; a pool lives exactly as long as
  its loop.** Per-test loops and anyio share one NullPool engine, as
  SQLAlchemy's asyncio docs recommend for several loops. Class, module, package
  and session loops get an engine of that scope, disposed on that loop before
  pytest-asyncio closes it. Measured cost of NullPool per test
  (`scripts/benchmark.py`, localhost Docker): asyncpg 1.54 → 7.02 ms, psycopg
  async 1.54 → 5.47 ms, aiomysql 1.94 → 3.10 ms, asyncmy 1.87 → 3.09 ms.
  Re-run the benchmark before quoting a number.

- **`sqlalchemy.ext.asyncio` is imported inside function bodies only.** On
  SQLAlchemy 2.1 without greenlet the import itself raises. A sync-only
  install must load the plugin and run every sync fixture; the sync-only CI job
  proves it.

- **Public pytest-asyncio API only:** `pytest_asyncio.fixture(loop_scope=)`,
  `is_async_test()`, and the `asyncio` marker and ini semantics. Its private
  `_loop_scope` and `_get_asyncio_mode` are re-derived from those, never
  imported.

- **No monkeypatching** of SQLAlchemy, pytest-asyncio, anyio or the
  application. The plugin this replaces in many projects,
  pytest-flask-sqlalchemy, patched session methods; that is how such plugins
  rot.

### 2. Never destroy what you did not create, and never hang

- **Ownership through the marker.** Schema creation writes a `pytest_sqla`
  marker row with the run id and the number of xdist workers using it. Tables
  of the metadata that exist without the marker are refused (S1); another
  run's marker is refused (S2) unless `--sqla-recreate-schema`; the worker that
  brings the count to 0 drops exactly the metadata's tables. A test plugin
  pointed at the wrong database by mistake must fail, not `drop_all`.
- **Serialised under a database lock.** Measured before this design: a naive
  `create_all` per worker with `-n 4` ended "2 passed, 6 errors" on 3 of 3 runs
  per backend. PostgreSQL uses `pg_advisory_xact_lock`, MySQL `GET_LOCK`,
  SQLite `BEGIN IMMEDIATE`; any other dialect refuses xdist (F15).
- **Every wait is bounded.** `sqla_lock_timeout` bounds the schema lock and
  the DDL lock waits. Measured: a drop behind a leaked transaction failed after
  2.0 s with sqlstate 55P03 and MySQL 1205, and without a timeout it blocked
  forever. A timeout becomes SchemaConflictError with the cause named, never a
  hang.
- **A database pytest-sqla made up is discarded, not dropped.** On Python 3.11+
  an unread result keeps its SQLite read lock until it is garbage-collected,
  even after rollback and close, and pytest keeps the last failure's
  traceback: one failing test made the session-end DROP wait out the whole
  lock timeout. In-memory databases go with their keeper; per-worker files are
  deleted; a file the user named still has its tables dropped.
- **Never delete a file the user named.** Only the derived `-gwN` files.

### 3. Breaches are errors in the test that caused them

- A root commit (the `commit` listener fired) is IsolationError I1; a guard
  that is gone is I2, with the driver's own message. Both are raised at the
  teardown of the test that caused them, so they never surface in a later test.
- **No warnings, ever.** Users run `filterwarnings = error`; a warning from
  this plugin is a crash in their suite. Every problem is an error with a
  message that says what to do.
- **No false alarms.** A root rollback, an inner savepoint lost to untidy
  session handling, and a disconnect are not breaches. A false IsolationError
  would teach people to ignore the real ones.
- **The test's own failure always wins.** Teardown errors are separate
  entries; they never replace the test body's exception.
- **Messages carry no internal traceback.** Plugin frames are hidden
  (`__tracebackhide__`) and errors are re-raised from the fixture frame, so the
  user sees the message, not the DDL machinery.

### 4. Public API

- Fixtures: `sqla_url`, `sqla_async_url`, `sqla_metadata`,
  `sqla_engine_options`, `sqla_session_options`, `sqla_schema`,
  `sqla_connection`, `sqla_sessionmaker`, `sqla_session`,
  `sqla_async_connection`, `sqla_async_sessionmaker`, `sqla_async_session`.
  Every fixture whose name starts with `_sqla_` is internal and may change.
- Options: `--sqla-url`, `--sqla-async-url`, `--sqla-recreate-schema`; the
  `PYTEST_SQLA_URL` and `PYTEST_SQLA_ASYNC_URL` variables; the `sqla_url`,
  `sqla_async_url`, `sqla_metadata`, `sqla_schema` and `sqla_lock_timeout` ini
  keys. Precedence is command line, then environment, then ini.
- Exceptions, all in `pytest_sqla.__all__`: `PytestSqlaError`,
  `SchemaConflictError`, `IsolationError`, `LoopMismatchError` (also a
  `RuntimeError`).
- The wording of the error messages is part of the contract: they are what a
  user reads instead of the source. Change them deliberately, with the tests
  that pin them.

### 5. Implementation rules

- **Exactly two runtime dependencies: `sqlalchemy>=2.0` and `pytest>=8.2`.**
  pytest-asyncio and greenlet are the `asyncio` extra. Adding a dependency
  requires an amendment to this document, argued on its own merits, in its own
  PR.
- **Never echo a password.** Every URL is rendered with
  `render_as_string(hide_password=True)`; a URL that fails to parse is reported
  by its source, never its value, because SQLAlchemy's parse error repeats it.
- **Inert unless asked.** A run whose tests never request an `sqla_*` fixture
  imports no application code, opens no connection, never imports
  `sqlalchemy.ext.asyncio`, and registers no per-test hooks.
- **No default database, no silent fallback.** A missing URL fails the tests
  that need one (F1); an empty variable is a usage error. A green run on SQLite
  that was meant for PostgreSQL is the failure this rule exists for.
- **Settings are checked at configure time when that is cheap**, and lazily at
  first database use otherwise (metadata imports, engines, connections).
- **Errors name the fixture the test asked for**, which is why the public
  fixtures dispatch to internal ones after checking.

### 6. Evidence, not assumption

A green suite on SQLite is evidence about SQLite. Every claim in the README is
backed by something that runs:

- **Real databases.** `tests/integration` repeats the behaviours against
  PostgreSQL 17 and MySQL 8.4, through psycopg (sync and async), asyncpg,
  pymysql, aiomysql and asyncmy, including pytest-xdist runs. CI fails if they
  skip: `SQLA_TEST_REQUIRE_ALL=1` turns a missing URL into a failure, and
  `scripts/check_no_skips.py` fails on a skip from any source.
- **Controls.** Where a claim is "without this, X breaks", a test shows X
  breaking without it: the SQLite leak, the default join mode, crossing loops.
- **The examples dogfood the promise.** Each example's second test asserts the
  first test's commits were rolled back, on SQLite, PostgreSQL and MySQL.
- **The floor is proven, not declared.** CI runs SQLAlchemy 2.0.0, pytest
  8.2.0 and pytest-asyncio 1.2.0 against the real servers.
- **Measure before a performance claim** (`scripts/benchmark.py`), and **watch
  a new test fail first**: a test is not proof until it has been seen red
  against the bug it is for.

Two harness traps found the hard way, and the rules they leave:

- pytester restores `sys.modules` after every in-process run. A dialect first
  imported inside one run is imported again by the next, and SQLAlchemy warns
  about its functions registering twice; a C extension imported twice can
  crash the interpreter (asyncpg segfaulted in `BaseProtocol.__init__`). Import
  every dialect and driver in `tests/conftest.py`, before any inner run.
- pytest-asyncio before 1.4 leaks an unclosed event loop from every in-process
  session. The harness collects it right after each inner run on those
  versions only, so the current stack stays fully strict.

### 7. Non-negotiable style

- **100% test coverage, branches included**, on `src/pytest_sqla`, enforced by
  `coverage` with `fail_under = 100`. The PostgreSQL and MySQL branches can
  only run against those servers, so the gate is measured over the whole
  suite with the real-database layer, in CI and in `verify.sh --all`. Do not
  lower `fail_under`, do not add `# pragma: no cover`, and do not weaken a gate
  to fit a change — restructure the code instead (one context manager turns
  every lock timeout into its error; one loop rolls back every savepoint).
- **Cognitive complexity ≤ 15 per function**, enforced by **complexipy**.
- `ruff check`, `ruff format --check` and `mypy --strict` are release gates.
- **Never `--cov-context`.** On Python 3.14 coverage's default sys.monitoring
  core cannot record contexts and warns on every switch, which
  `filterwarnings = error` turns into an error per test; the config pins
  `core = "ctrace"` for that reason.

### 8. Strictness scope

The non-negotiables above govern the **core package** (`src/pytest_sqla`).
`examples/`, `scripts/` and `tests/` run lighter rules: lint and format apply,
type annotations in examples do not, and dependency updates there may merge on
green CI without ceremony.

### 9. Security and supply chain (MANDATORY)

- Every PR includes a supply-chain pass.
- **Audit scope:** the release gate audits the *published* surface.
  `scripts/audit_production_surface.py` builds the wheel, installs it with its
  `asyncio` extra into a clean virtualenv, and runs `pip-audit` on that
  closure: SQLAlchemy, pytest and their dependencies, pytest-asyncio and
  greenlet. Advisories confined to dev tooling are Dependabot's job and do not
  block a release.
- No secrets in code, tests, examples, logs, or docs. The compose and CI
  passwords are throwaway values for throwaway containers.

### 10. Releasing

Bump `version` in `pyproject.toml` **and** `__version__` in
`src/pytest_sqla/__init__.py` to the same value, date the CHANGELOG entry,
then tag `vX.Y.Z` and push the tag. `.github/workflows/release.yml` checks the
tag against both, runs every gate against real servers, builds, smoke-tests
the wheel in a clean virtualenv, audits the closure, and publishes to PyPI
through **Trusted Publishing** (OIDC, environment `pypi`, PEP 740
attestations; no token, no secret). Then cold-verify from PyPI in a fresh
virtualenv (`uv pip install --refresh`), checking the version through the
Simple API, because the legacy JSON endpoint lags behind new releases.

### 11. Mutation testing: local only

Mutation testing is an **occasional, targeted audit — not a per-PR gate, and
never in CI.** Run it deliberately after reworking `_isolation.py` or
`_schema.py`, to find out whether their tests pin the behaviour.

- Scope it: `mutmut run --max-children 2 "src.pytest_sqla._isolation.*"`.
  Never run it across the whole package, and never two sessions at once.
- **Verify a kill without re-running mutmut.** Hand-apply the surviving
  mutation, run the plain suite, confirm the new test fails, then
  `git checkout --` to revert.
- Treat each survivor by the doctrine: add a test that kills it; delete code
  whose mutant is behaviourally equivalent (with a CHANGELOG note); or, for a
  genuine equivalent, leave a comment saying so.
