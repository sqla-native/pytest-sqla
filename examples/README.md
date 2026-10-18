# Examples

Two applications in the shapes pytest-sqla is for, with tests that assert its
promise against them. CI runs them on SQLite, PostgreSQL and MySQL; they are
validation, not decoration.

From the repository root, so the examples test this checkout rather than a
pytest-sqla from PyPI:

```bash
pip install -e . -r examples/requirements.txt
pytest examples -q
```

## What is in here

| Path | Shows |
| --- | --- |
| `fastapi_app/app.py` | An async FastAPI service with its own engine and a `get_session` dependency |
| `fastapi_app/conftest.py` | The dependency override with `sqla_async_sessionmaker`, and `sqla_session_options` matching the app's `expire_on_commit=False` |
| `fastapi_app/test_api.py` | Create, a 409 on a duplicate through the app's own rollback, the test reading the app's commit, and the next test seeing none of it; also on a module loop |
| `fastapi_app/test_api_anyio.py` | The same under anyio, with a plain async fixture |
| `sync_app/services.py` | A sync service layer that opens its own sessions from a sessionmaker and commits or rolls back itself |
| `sync_app/test_services.py` | A transfer that commits, a refused one that rolls back only itself, and the next test seeing an empty table |
| `conftest.py` | `sqla_metadata` as a fixture: two declarative bases, one test schema |
| `pytest.ini` | `sqla_url = sqlite://` and `sqla_async_url = sqlite+aiosqlite://`, the committed defaults |

## Against a real server

```bash
PYTEST_SQLA_URL=postgresql+psycopg://sqla:sqla@127.0.0.1:55434/sqla \
PYTEST_SQLA_ASYNC_URL=postgresql+asyncpg://sqla:sqla@127.0.0.1:55434/sqla \
  pytest examples -q
```

Set both variables. The ini file sets `sqla_async_url` to SQLite, and
pytest-sqla refuses a sync URL and an async URL that name different databases
rather than run half the suite against the wrong one.

The `pytest.ini` lives here, not inside an app directory: `pytest examples`
uses the first ini file found from `examples/` upwards, and one nested deeper
would lose to the repository's `pyproject.toml`.

This directory sits outside the core package's strictness scope; see
`GUIDELINES_PYTEST_SQLA.md` section 8.
