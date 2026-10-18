"""Where each setting comes from, and what is checked before any test runs.

Precedence is the command line, then the environment, then the ini file, which
is the order pytest-django documents for its own settings. The ini value is the
repository's committed default, the environment is per machine or per CI job,
and the command line is per invocation. Overriding a fixture in a conftest
replaces the computed value entirely, as with any pytest fixture.

Everything checked at configure time is cheap: no application imports, no
dialects, no connections. A run whose tests never request a database fixture
pays for nothing and cannot fail on a database it does not use. The URLs are
parsed without ever echoing the raw value, because SQLAlchemy's parse error
repeats it, password included.
"""

from __future__ import annotations

import importlib
import math
import os
import re
import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import MetaData
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

from . import _sqlite

SCHEMA_MODES = ("create", "none")
DEFAULT_LOCK_TIMEOUT = "30"

_METADATA_PATH = re.compile(r"[A-Za-z_][\w.]*:[A-Za-z_][\w.]*")
_DEFAULT_PORTS = {"postgresql": 5432, "mysql": 3306}


@dataclass(frozen=True)
class Origin:
    """The three places one setting can come from, highest precedence first."""

    option: str
    variable: str
    ini: str


URL_ORIGIN = Origin("--sqla-url", "PYTEST_SQLA_URL", "sqla_url")
ASYNC_URL_ORIGIN = Origin("--sqla-async-url", "PYTEST_SQLA_ASYNC_URL", "sqla_async_url")


@dataclass(frozen=True)
class ConfiguredURL:
    url: URL
    source: str
    """Which of the three places it came from, so the report header can say."""


@dataclass(frozen=True)
class Settings:
    url: ConfiguredURL | None
    async_url: ConfiguredURL | None
    metadata_path: str | None
    schema_mode: str
    lock_timeout: float
    recreate_schema: bool
    run_id: str
    """Shared by every xdist worker of one run, so siblings recognise each other."""
    worker: str | None
    memory_name: str
    """The memdb database this session's in-memory SQLite URLs become."""


SETTINGS = pytest.StashKey[Settings]()


def add_options(parser: pytest.Parser) -> None:
    group = parser.getgroup("sqla", "pytest-sqla")
    group.addoption(
        URL_ORIGIN.option,
        metavar="URL",
        default=None,
        help="Database URL for the sqla fixtures. Beats PYTEST_SQLA_URL and the sqla_url ini key.",
    )
    group.addoption(
        ASYNC_URL_ORIGIN.option,
        metavar="URL",
        default=None,
        help="URL for the async fixtures, when sqla_url's driver has no async twin. "
        "It must name the same database.",
    )
    group.addoption(
        "--sqla-recreate-schema",
        action="store_true",
        default=False,
        help="Replace the schema a crashed pytest-sqla run left behind instead of refusing it.",
    )
    parser.addini(URL_ORIGIN.ini, "Database URL for the sqla fixtures.", default="")
    parser.addini(ASYNC_URL_ORIGIN.ini, "URL for the async sqla fixtures.", default="")
    parser.addini(
        "sqla_metadata",
        "Import path of the metadata to create, such as 'app.models:Base'.",
        default="",
    )
    parser.addini(
        "sqla_schema",
        "'create' (default): create and drop the metadata's tables. 'none': no DDL at all.",
        default="create",
    )
    parser.addini(
        "sqla_lock_timeout",
        "Seconds to wait for the schema lock and for DDL locks. Default 30.",
        default=DEFAULT_LOCK_TIMEOUT,
    )


def read_settings(config: pytest.Config) -> Settings:
    """Everything from the command line, environment and ini, checked; raises UsageError."""
    schema_mode = str(config.getini("sqla_schema"))
    if schema_mode not in SCHEMA_MODES:
        raise pytest.UsageError(
            f"pytest-sqla: sqla_schema must be 'create' or 'none', got {schema_mode!r}."
        )
    lock_timeout = _lock_timeout(str(config.getini("sqla_lock_timeout")))
    metadata_path = str(config.getini("sqla_metadata")) or None
    if metadata_path is not None and not _METADATA_PATH.fullmatch(metadata_path):
        raise pytest.UsageError(
            "pytest-sqla: sqla_metadata must be an import path like 'app.models:Base', "
            f"got {metadata_path!r}."
        )
    url = _configured_url(config, URL_ORIGIN)
    async_url = _configured_url(config, ASYNC_URL_ORIGIN)
    if url is not None and async_url is not None:
        _require_same_database(url.url, async_url.url, pytest.UsageError)
    return Settings(
        url=url,
        async_url=async_url,
        metadata_path=metadata_path,
        schema_mode=schema_mode,
        lock_timeout=lock_timeout,
        recreate_schema=bool(config.getoption("--sqla-recreate-schema")),
        run_id=os.environ.get("PYTEST_XDIST_TESTRUNUID") or uuid.uuid4().hex,
        worker=os.environ.get("PYTEST_XDIST_WORKER") or None,
        memory_name=f"pytest-sqla-{uuid.uuid4().hex}",
    )


def _lock_timeout(raw: str) -> float:
    try:
        seconds = float(raw)
    except ValueError:
        seconds = math.nan
    if not (math.isfinite(seconds) and seconds > 0):
        raise pytest.UsageError(
            f"pytest-sqla: sqla_lock_timeout must be a positive number of seconds, got {raw!r}."
        )
    return seconds


def _configured_url(config: pytest.Config, origin: Origin) -> ConfiguredURL | None:
    """The first of the three sources that is set. An empty variable counts as set.

    ``PYTEST_SQLA_URL=""`` is usually a CI secret that did not arrive. Treating
    it as unset would fall back to the ini default, typically SQLite, and a run
    meant for PostgreSQL would quietly test something else.
    """
    candidates = (
        (origin.option, config.getoption(origin.option)),
        (origin.variable, os.environ.get(origin.variable)),
        (origin.ini, str(config.getini(origin.ini)) or None),
    )
    for source, raw in candidates:
        if raw is not None:
            url = _parse(raw)
            if url is None:
                raise pytest.UsageError(invalid_url(source))
            return ConfiguredURL(url, source)
    return None


def _parse(raw: object) -> URL | None:
    if isinstance(raw, URL):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        return make_url(raw)
    except (ArgumentError, ValueError):
        return None


def invalid_url(source: str) -> str:
    return (
        f"pytest-sqla: the value of {source} is not a valid SQLAlchemy URL; "
        "expected dialect+driver://user:password@host/database."
    )


def render(url: URL) -> str:
    return url.render_as_string(hide_password=True)


def _identity(url: URL) -> tuple[object, ...]:
    backend = url.get_backend_name()
    family = "mysql" if backend == "mariadb" else backend
    if family == "sqlite":
        memory = _sqlite.is_plain_memory(url)
        return (family, None if memory else os.path.normpath(str(url.database)))
    port = url.port if url.port is not None else _DEFAULT_PORTS.get(family)
    return (family, url.host, port, url.database, url.username)


def _require_same_database(
    url: URL, async_url: URL, error: type[pytest.UsageError] | None = None
) -> None:
    """Backend, host, port, database and user must match; the driver may differ."""
    if _identity(url) == _identity(async_url):
        return
    message = (
        "pytest-sqla: sqla_url and sqla_async_url must point at the same database, but one "
        f"is {render(url)} and the other {render(async_url)}."
    )
    if error is not None:
        raise error(message)
    pytest.fail(message, pytrace=False)


@dataclass(frozen=True)
class Targets:
    """What pytest-sqla actually connects to, derived from the configured URLs.

    The configured values are never changed. An in-memory SQLite URL becomes a
    memdb name, and in create mode under xdist a SQLite file gains a ``-gwN``
    suffix; the result is what ``sqla_connection.engine.url`` shows.
    """

    sync: URL | None
    asynchronous: URL | None
    async_setting: str
    """``sqla_async_url``, or ``sqla_url`` when the async URL fell back to it."""
    keeper: str | None
    """The sqlite3 filename to hold open for the session, for shared memory."""
    worker_file: str | None
    """A per-worker SQLite file this run creates and deletes."""
    disposable: bool
    """The database is one pytest-sqla made up, a memdb name or a per-worker
    file, so it is discarded at the end instead of having its tables dropped."""

    @property
    def ddl(self) -> URL | None:
        """The URL schema DDL runs on: the sync one when there is one."""
        return self.sync or self.asynchronous


def resolve_targets(sync_value: object, async_value: object, settings: Settings) -> Targets:
    sync = _fixture_url(sync_value, "sqla_url")
    asynchronous = _fixture_url(async_value, "sqla_async_url")
    if sync is not None and asynchronous is not None:
        _require_same_database(sync, asynchronous)
    async_setting = "sqla_url" if asynchronous == sync else "sqla_async_url"
    memory = any(_sqlite.is_plain_memory(url) for url in (sync, asynchronous) if url is not None)
    sync = _effective(sync, settings)
    asynchronous = _effective(asynchronous, settings)
    connected = [url for url in (sync, asynchronous) if url is not None]
    shared = [url for url in connected if _sqlite.is_shared_memory(url)]
    derived = settings.schema_mode == "create" and settings.worker is not None
    files = [url for url in connected if derived and _sqlite.is_plain_file(url)]
    return Targets(
        sync=sync,
        asynchronous=asynchronous,
        async_setting=async_setting,
        keeper=_sqlite.keeper_target(shared[0]) if shared else None,
        worker_file=str(files[0].database) if files else None,
        disposable=memory or bool(files),
    )


def _fixture_url(value: object, setting: str) -> URL | None:
    if value is None:
        return None
    url = _parse(value)
    if url is None:
        pytest.fail(invalid_url(f"the {setting} fixture"), pytrace=False)
    return url


def _effective(url: URL | None, settings: Settings) -> URL | None:
    if url is None:
        return None
    if _sqlite.is_plain_memory(url):
        _sqlite.require_shared_memory()
        return _sqlite.shared_memory_url(url, settings.memory_name)
    if settings.schema_mode == "create" and settings.worker and _sqlite.is_plain_file(url):
        return _sqlite.per_worker_url(url, settings.worker)
    return url


def load_metadata(path: str) -> object:
    """Import ``package.module:attribute``, where the attribute may be dotted."""
    module_name, _, attribute = path.partition(":")
    try:
        target: object = importlib.import_module(module_name)
        for name in attribute.split("."):
            target = getattr(target, name)
    except Exception as error:
        pytest.fail(
            f"pytest-sqla: cannot import sqla_metadata = {path!r}: {type(error).__name__}: {error}",
            pytrace=False,
        )
    return target


def metadatas(value: object, described: str) -> tuple[MetaData, ...]:
    """A MetaData, a class whose ``.metadata`` is one, or a list or tuple of these.

    ``described`` names where the value came from, for the error message.
    """
    items = value if isinstance(value, (list, tuple)) else (value,)
    found: list[MetaData] = []
    for item in items:
        metadata = item if isinstance(item, MetaData) else _class_metadata(item)
        if metadata is None:
            pytest.fail(
                f"pytest-sqla: {described} is a {type(item).__name__}, not a MetaData, "
                "a declarative base, or a list of them.",
                pytrace=False,
            )
        if metadata not in found:
            found.append(metadata)
    return tuple(found)


def _class_metadata(item: object) -> MetaData | None:
    candidate = getattr(item, "metadata", None) if isinstance(item, type) else None
    return candidate if isinstance(candidate, MetaData) else None
