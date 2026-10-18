# Testing procedure

Seven layers, each answering a question the one before it cannot. Writing them
down is what makes "the tests pass" a claim: which tests, on what, proving
what.

| # | Layer | Question it answers | Where it runs |
| --- | --- | --- | --- |
| 1 | Hermetic suite | Does the plugin do what it says, on SQLite, as a user meets it? | every run, every Python |
| 2 | Real databases | Does it hold on PostgreSQL and MySQL, through every driver? | CI + `verify.sh --all` |
| 3 | The no-skip gate | Did layer 2 actually run? | CI + `verify.sh --all` |
| 4 | Coverage (100% branch) | Did every branch execute, on the backend it is for? | CI + `verify.sh --all` |
| 5 | Examples | Does it work behind a real FastAPI app and a service layer? | CI + on demand |
| 6 | Packaging | Does what we publish work for someone who installs it? | CI + release |
| 7 | Mutation | Are the tests any good, or merely numerous? | occasional, local |

Plus the static gates on every run: `ruff check`, `ruff format --check`,
`mypy` (strict on `src/pytest_sqla`), `complexipy` (at most 15 per function).

## The short version

```bash
./scripts/verify.sh          # static gates, layers 1, 5, 6. No Docker, about a minute.
./scripts/verify.sh --all    # adds layers 2-4 with the compose databases.
```

## What to run for what you changed

| You changed | Run |
| --- | --- |
| `_isolation.py`, `_schema.py`, `_asyncio.py`, `_engines.py` | `verify.sh --all`, then a scoped mutation run |
| `_config.py`, `_sqlite.py`, `plugin.py` | `verify.sh`; `--all` if a database path changed |
| An error message | `verify.sh`: the tests pin the wording |
| Anything with a performance claim | `python scripts/benchmark.py` before and after |
| Docs only | nothing, but every claim in them needs a test or a control |

---

## 1. The hermetic suite

```bash
pytest -q
```

SQLite through pysqlite and aiosqlite; no Docker, no network. Plugin behaviour
is tested through **pytester**: each test writes a small project (an ini file,
models, test files) and runs a real pytest session on it, the way a user meets
the plugin, with ini keys, command-line options, environment variables and
markers. In-process runs keep coverage; the pytest-xdist runs and the
inertness check use a subprocess.

- `tests/sync/` needs nothing async: configuration and precedence, every
  usage error, sync isolation and breaches, the schema and its locks, SQLite,
  the engine and session options, the public surface, what a sync-only
  install reports, and the README's Alembic recipe, run as its code block is
  written. The sync-only CI job runs exactly this directory with no
  pytest-asyncio, anyio, greenlet, aiosqlite or alembic installed.
- `tests/test_isolation_async.py`, `test_loops.py`, `test_async_sqlite.py`:
  the async family under pytest-asyncio strict and auto mode, every loop
  scope, anyio and both plugin orders, the loop guard, tasks a test leaves
  running, NullPool and per-loop pools.
- `tests/test_xdist.py`: per-worker SQLite files, per-process memory databases.

Controls keep the suite honest: `tests/sync/test_sqlite.py` shows the SQLite
savepoint leak without the fix, and `tests/sync/test_recipe.py` shows the
default join mode losing the seed and leaking the next commit. If a control
stops failing the way it should, the corresponding feature has nothing left
to prove.

The harness itself has two traps, both handled in `tests/`: pytester restores
`sys.modules` after each in-process run, so every dialect and driver is
imported up front in `tests/conftest.py` (a C extension imported twice
crashed the interpreter); and pytest-asyncio before 1.4 leaks an event loop
per in-process session, which `tests/helpers.py` collects on those versions.

## 2-4. Real databases, no skips, coverage

```bash
docker compose up -d --wait
./scripts/test-integration.sh
docker compose down -v
```

PostgreSQL 17 and MySQL 8.4 from `compose.yaml` (host ports 55434 and 53308,
so a server already on 5432 or 3306 is untouched), through psycopg (sync and
async), asyncpg, pymysql, aiomysql and asyncmy. The same behaviours as layer
1, plus what only real servers show: transactional DDL on PostgreSQL, implicit
commits and deadlocks on MySQL, session state that ROLLBACK keeps, cross-loop
breakage on asyncpg, advisory locks and `GET_LOCK` timing out, a leaked
transaction blocking the drop and a live run blocking a recreation, a task left
running on every async driver, the Alembic recipe with a password that needs
escaping, and pytest-xdist workers sharing one database.

These specs skip when their URL or driver is missing, so a clone stays green.
**CI fails if they skip**: `SQLA_TEST_REQUIRE_ALL=1` turns each such skip into
a failure, and `scripts/check_no_skips.py` fails the run on a skip from any
source. A skipped suite reporting green looks like coverage that is not there.

Coverage is 100% including branches, measured over the whole suite with this
layer included, because the PostgreSQL and MySQL branches of the schema locks
cannot run on SQLite:

```bash
source scripts/local-databases.env
SQLA_TEST_REQUIRE_ALL=1 coverage run scripts/check_no_skips.py -q
coverage report
```

The hermetic suite alone covers everything except those branches. Do not
lower `fail_under`, do not add `# pragma: no cover`, and do not weaken a gate
to fit a change: restructure the code so the branch does not exist.

## 5. Examples

```bash
pip install -e . -r examples/requirements.txt   # from the repository root
pytest examples -q
```

A FastAPI app tested through `httpx.AsyncClient` with a dependency override,
under pytest-asyncio and under anyio, and a sync service layer that commits
through its own sessions. Each example's last test asserts the earlier tests'
commits were rolled back, so the examples dogfood the plugin's promise. CI
runs them on SQLite, then on PostgreSQL and MySQL by setting
`PYTEST_SQLA_URL` and `PYTEST_SQLA_ASYNC_URL`.

## 6. Packaging

```bash
python -m build && twine check --strict dist/*   # what PyPI will render
python scripts/check_packed_consumer.py           # wheel -> clean venv -> real use
python scripts/audit_production_surface.py        # pip-audit the published closure
```

The first builds the wheel and installs it into a throwaway virtualenv with no
dev tooling. First the plain install, which on SQLAlchemy 2.1 has no greenlet:
the entry point must resolve, `py.typed` must ship, a sync project must pass,
and an async fixture must fail with pytest-sqla's own message. Then the
`asyncio` extra: an async project must pass. The second audits what
`pip install "pytest-sqla[asyncio]"` pulls in, not the development tree.

## 7. Mutation testing

**An occasional, targeted audit. Not a per-PR gate, and never in CI.**

100% coverage proves every line ran. Mutation testing asks whether an
assertion would notice the line changing.

```bash
mutmut run --max-children 2 "src.pytest_sqla._isolation.*"
mutmut results
mutmut show <mutant>
```

Scope every run to the one or two files whose logic you reworked, usually
`_isolation.py` or `_schema.py`; the runner re-executes the suite per mutant.
`--max-children 2` is deliberate, and so is never running two at once: each
mutant re-runs the suite. `mutmut results` lists only survivors.

**Verify a kill without re-running mutmut**: hand-apply the surviving
mutation, run the plain suite, confirm your new test fails, then
`git checkout --` to revert. A test written for a survivor is not a kill until
you have watched it fail.

## Benchmarks

```bash
docker compose up -d --wait
source scripts/local-databases.env
python scripts/benchmark.py
```

One isolation cycle per test, pooled and with NullPool, best of 5 runs of 200,
for every driver with a URL set. Re-run it before changing a number in the
docs; a single run is too noisy to publish.
