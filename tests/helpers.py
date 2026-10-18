"""Helpers for the inner pytest projects the plugin tests run.

Nothing here imports an async package, so ``tests/sync`` also runs in the
sync-only CI job, where pytest-asyncio, anyio, greenlet and aiosqlite are not
installed.
"""

from __future__ import annotations

import gc
import importlib.util
import sqlite3
import textwrap
import warnings
from collections.abc import Iterable
from importlib.metadata import version
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import NullPool

HAS_PYTEST_ASYNCIO = importlib.util.find_spec("pytest_asyncio") is not None


def _leaks_event_loops() -> bool:
    """pytest-asyncio before 1.4 leaves an unclosed event loop behind every
    in-process session: measured with 1.2.0 and 1.3.0, on pytest 8.2 to 9.1,
    with no pytest-sqla involved. Its socket's ResourceWarning then surfaces in
    whichever later test the garbage collector happens to run in."""
    if not HAS_PYTEST_ASYNCIO:
        return False
    major, minor = (int(part) for part in version("pytest-asyncio").split(".")[:2])
    return (major, minor) < (1, 4)


LEAKS_EVENT_LOOPS = _leaks_event_loops()

MODELS = """
from sqlalchemy import ForeignKey, String, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "user_account"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(100), unique=True)


class Note(Base):
    __tablename__ = "note"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_account.id"))
    body: Mapped[str] = mapped_column(String(200))


def count(session, model=User):
    return session.scalar(select(func.count()).select_from(model))


async def acount(session, model=User):
    return await session.scalar(select(func.count()).select_from(model))
"""


class Project:
    """An inner pytest project: an ini file, models, tests, and ways to run them."""

    def __init__(self, pytester: pytest.Pytester) -> None:
        self.pytester = pytester
        self.path = pytester.path
        self.settings: dict[str, str] = {
            "addopts": "-p no:cacheprovider",
            "filterwarnings": "error",
        }
        if HAS_PYTEST_ASYNCIO:
            self.settings["asyncio_mode"] = "strict"
            self.settings["asyncio_default_fixture_loop_scope"] = "function"

    def ini(self, **settings: str | None) -> Project:
        """Set ini keys; None removes one."""
        for key, value in settings.items():
            if value is None:
                self.settings.pop(key, None)
            else:
                self.settings[key] = value
        return self

    def models(self, source: str = MODELS, name: str = "models") -> Project:
        self.pytester.makepyfile(**{name: source})
        self.settings.setdefault("sqla_metadata", f"{name}:Base")
        return self

    def sqlite(self, name: str = "test.db") -> str:
        """A file URL in the project directory, set as sqla_url."""
        url = f"sqlite:///{self.path / name}"
        self.settings["sqla_url"] = url
        return url

    def conftest(self, source: str) -> Project:
        self.pytester.makeconftest(textwrap.dedent(source))
        return self

    def tests(self, source: str, name: str = "test_inner") -> Project:
        self.pytester.makepyfile(**{name: textwrap.dedent(source)})
        return self

    def write_ini(self) -> None:
        lines = ["[pytest]", *(f"{key} = {value}" for key, value in self.settings.items())]
        (self.path / "pytest.ini").write_text("\n".join(lines) + "\n")

    def run(self, *args: str) -> pytest.RunResult:
        self.write_ini()
        result = self.pytester.runpytest(*args)
        if LEAKS_EVENT_LOOPS:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                gc.collect()
        return result

    def run_subprocess(self, *args: str, timeout: float | None = None) -> pytest.RunResult:
        """A pytest of its own; ``timeout`` fails the outer test instead of hanging it."""
        self.write_ini()
        return self.pytester.runpytest_subprocess(*args, timeout=timeout)


def table_names(url: str) -> list[str]:
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            return sorted(inspect(connection).get_table_names())
    finally:
        engine.dispose()


def sqlite_rows(path: Path | str, sql: str) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def sqlite_script(path: Path | str, statements: Iterable[str]) -> None:
    connection = sqlite3.connect(path)
    try:
        for statement in statements:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()
