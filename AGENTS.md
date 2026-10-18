# Agent Guidance

`pytest-sqla` is a pytest plugin: every test gets a SQLAlchemy connection,
session and sessionmaker whose work is rolled back, sync and async, even when
the code under test commits.

**[GUIDELINES_PYTEST_SQLA.md](GUIDELINES_PYTEST_SQLA.md) is binding.** Read it
before changing anything under `src/pytest_sqla`. Most of its rules encode
behaviour that was measured against real databases and drivers, is easy to get
wrong, and fails silently when it is wrong.

## The three that bite hardest

1. **`create_savepoint` plus the guard SAVEPOINT; never let a commit escape
   silently.** The default join mode lets an application's rollback undo the
   test's seed and its next commit survive the test. A breach must fail the
   test that caused it, at its teardown, and nothing that is not a breach may.
2. **Async objects live on the test's own event loop.** The public async
   fixtures are sync dispatchers; an engine shared across loops is NullPool;
   `sqlalchemy.ext.asyncio` is never imported at module level.
3. **Never drop a table pytest-sqla did not create, and never skip
   silently.** No default database, no SQLite fallback, no skipped
   real-database suite that reports green.

## Before opening a PR

```bash
./scripts/verify.sh          # lint, types, complexity, hermetic suite, examples, packaging
./scripts/verify.sh --all    # adds PostgreSQL and MySQL: every backend, 100% coverage, no skips
```

[TESTING.md](TESTING.md) explains the layers, what each one proves, and which
to run for what you changed. Coverage is 100% including branches, measured
over the whole suite with the real databases; do not lower `fail_under`, do not
add `# pragma: no cover`, and do not weaken a gate to make a change fit.

Mutation testing (`mutmut`) is an occasional local audit, never a per-PR step
and never in CI; see TESTING.md.

## Real databases

```bash
docker compose up -d --wait        # PostgreSQL 17 on 55434, MySQL 8.4 on 53308
./scripts/test-integration.sh      # tests/integration, every driver, fails on any skip
docker compose down -v
```

Run them before any PR that touches `_isolation.py`, `_schema.py`,
`_asyncio.py` or `_engines.py`, and stop the containers when you are done.

## Measure, don't reason from memory

The design rests on probes, and every claim in the README has a test or a
control behind it. When you need to know how SQLAlchemy, a driver, sqlite3 or
pytest-asyncio actually behaves, write a throwaway script and run it: which
transaction events fire, whether a savepoint survives, which loop a fixture
runs on, whether a lock is held. Several facts here contradict a plausible
reading of the docs (for example, sqlite3's `executescript()` commits even with
`isolation_level = None`, and an unread result keeps a SQLite read lock after
its connection closes on Python 3.11+).

The inner pytest sessions in `tests/` run in-process through pytester, which
restores `sys.modules` after each run. Import any new dialect or C-extension
driver in `tests/conftest.py` first, or the second inner run that uses it can
warn or crash.

## Environment

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e . --group dev
uv pip install --python .venv/bin/python -r examples/requirements.txt
```

Commit messages are plain sentences describing the change, with no trailers.
