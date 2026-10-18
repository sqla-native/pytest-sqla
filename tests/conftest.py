"""Shared harness: inner pytest projects that use the plugin the way a user does.

Plugin behaviour is tested through pytester because it runs a real pytest
session, with an ini file, command-line options, environment variables and
markers. In-process runs keep coverage; xdist runs use a subprocess.
"""

from __future__ import annotations

import contextlib
import importlib
import os

import pytest

from tests.helpers import Project

# pytester restores sys.modules after every in-process run, so a module first
# imported inside one run is imported again by the next. SQLAlchemy warns when
# a dialect registers its SQL functions twice, and a C extension imported twice
# can crash the process: asyncpg segfaulted in BaseProtocol.__init__ on the
# second inner run that connected with it. Import them all up front.
for _module in (
    "sqlalchemy.dialects.mssql",
    "sqlalchemy.dialects.mysql",
    "sqlalchemy.dialects.postgresql",
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.ext.asyncio",
    "aiosqlite",
    "asyncpg",
    "psycopg",
    "pymysql",
    "aiomysql",
    "asyncmy",
):
    with contextlib.suppress(ImportError):  # the sync-only CI job lacks the async ones
        importlib.import_module(_module)

pytest_plugins = ["pytester"]


@pytest.fixture(autouse=True)
def _no_outer_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The outer environment must never reach an inner run."""
    for name in list(os.environ):
        if name.startswith("PYTEST_SQLA_"):
            monkeypatch.delenv(name)


@pytest.fixture
def project(pytester: pytest.Pytester) -> Project:
    return Project(pytester)
