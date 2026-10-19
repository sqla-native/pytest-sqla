"""The public surface: twelve fixtures with docstrings, four exceptions, one version."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest_sqla
from tests.helpers import Project

PUBLIC_FIXTURES = [
    "sqla_url",
    "sqla_async_url",
    "sqla_metadata",
    "sqla_engine_options",
    "sqla_session_options",
    "sqla_schema",
    "sqla_connection",
    "sqla_sessionmaker",
    "sqla_session",
    "sqla_async_connection",
    "sqla_async_sessionmaker",
    "sqla_async_session",
]


def test_every_public_fixture_is_listed_with_its_docstring(project: Project) -> None:
    result = project.run("--fixtures")
    output = result.stdout.str()
    for name in PUBLIC_FIXTURES:
        result.stdout.fnmatch_lines([f"{name} *", "    [A-Z]*"])
        assert f"\n{name} " in output, name
    assert "_sqla_" not in output, "internal fixtures stay out of --fixtures"


def test_the_exceptions_are_exported() -> None:
    assert sorted(pytest_sqla.__all__) == [
        "IsolationError",
        "LoopMismatchError",
        "PytestSqlaError",
        "SchemaConflictError",
    ]
    for name in ("IsolationError", "LoopMismatchError", "SchemaConflictError"):
        assert issubclass(getattr(pytest_sqla, name), pytest_sqla.PytestSqlaError)
    assert issubclass(pytest_sqla.LoopMismatchError, RuntimeError)


def test_the_version_matches_pyproject() -> None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text())["project"]["version"]
    assert pytest_sqla.__version__ == declared
