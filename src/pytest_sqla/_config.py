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

import hashlib
import importlib
import math
import os
import re
import socket
import uuid
from dataclasses import dataclass
from typing import Literal

import pytest
from sqlalchemy import MetaData
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

from . import _sqlite

SCHEMA_MODES = ("create", "none")
DATABASE_MODES = ("configured", "run", "worker")
DEFAULT_LOCK_TIMEOUT = 30.0
RUN_ID_KEY = "pytest_sqla_run_id"
"""The key under which an xdist controller hands its run id to every worker."""
# pytest 9's native TOML ([tool.pytest], pytest.toml) type-checks values: a
# string option refused sqla_lock_timeout = 30 with an INTERNALERROR. pytest
# 8.4 added the float type, which takes a number there and parses one in ini
# files; 8.2 and 8.3 only know strings.
_LOCK_TIMEOUT_TYPE: Literal["float", "string"] = (
    "float" if pytest.version_tuple >= (8, 4) else "string"
)

_METADATA_PATH = re.compile(r"[A-Za-z_][\w.]*:[A-Za-z_][\w.]*")
_DEFAULT_PORTS = {"postgresql": 5432, "mysql": 3306}
_HEX = re.compile(r"[0-9a-f]{8}")
# How long a database name may be. PostgreSQL counts bytes and shortens a
# longer name with only a NOTICE (NAMEDATALEN - 1, "longer names ... will be
# truncated", PostgreSQL 17 docs); MySQL counts characters and refuses one.
_NAME_LIMITS = {"postgresql": 63, "mysql": 64, "mariadb": 64}
_NAME_UNITS = {"postgresql": "bytes", "mysql": "characters", "mariadb": "characters"}
_SERVERS = {"postgresql": "PostgreSQL", "mysql": "MySQL", "mariadb": "MariaDB"}


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
    """Which of the three places it came from, so the report header can say:
    the option, the variable, or "the sqla_url ini key", never to be read as
    the sqla_url fixture, which an override can replace."""


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
    database_mode: str
    """``configured``, or ``run`` and ``worker``: a database of pytest-sqla's own."""
    keep_database: bool

    @property
    def configured(self) -> ConfiguredURL | None:
        """The URL a database of pytest-sqla's own is named after: the sync one if set."""
        return self.url or self.async_url


SETTINGS = pytest.StashKey[Settings]()


def owner_of(worker: str | None) -> str:
    """Who created a marker or a database: ``host:pid/worker``."""
    return f"{socket.gethostname()}:{os.getpid()}/{worker or 'main'}"


class Refused(ValueError):
    """A database pytest-sqla cannot create for this URL; the message says why."""


@dataclass(frozen=True)
class Owned:
    """A database ``sqla_database = run`` or ``worker`` creates, uses and drops.

    Only one built here is ever dropped, and only after the three checks in
    ``_database``: its name derives from this run's id, it carries
    pytest-sqla's mark, and the mark says this run created it.
    """

    admin: URL
    """The configured URL: the database pytest-sqla connects to for CREATE and DROP."""
    name: str
    mode: str
    run_id: str
    worker: str | None
    lock_timeout: float
    keep: bool

    @property
    def url(self) -> URL:
        return self.admin.set(database=self.name)


def run8(run_id: str) -> str:
    """The run id's first 8 hex digits, or 8 of its SHA-256 when it does not start with them."""
    head = run_id[:8]
    return head if _HEX.fullmatch(head) else hashlib.sha256(run_id.encode()).hexdigest()[:8]


def derived_name(database: str, mode: str, run_id: str, worker: str | None) -> str:
    """``app_1a2b3c4d`` for the run, ``app_1a2b3c4d_gw0`` for one xdist worker."""
    suffix = f"_{worker}" if mode == "worker" and worker else ""
    return f"{database}_{run8(run_id)}{suffix}"


def owned_name(url: URL, settings: Settings, worker: str | None) -> str | None:
    """The database ``sqla_database`` creates instead of ``url``'s, or None.

    None in configured mode and on SQLite, where every database pytest-sqla
    makes up is one per process or per worker already. Raises Refused for any
    other dialect, for a URL that names no database, and for a name the server
    would not take whole.
    """
    mode = settings.database_mode
    if mode == "configured" or _sqlite.is_sqlite(url):
        return None
    backend = url.get_backend_name()
    if backend not in _NAME_LIMITS:
        raise Refused(
            f"pytest-sqla: sqla_database = {mode} creates databases on PostgreSQL, MySQL and "
            f"MariaDB, and has no effect on SQLite; {render(url)} is {backend}. Set sqla_database "
            "= configured and create the database before the run."
        )
    if not url.database:
        raise Refused(
            f"pytest-sqla: sqla_database = {mode} names its database after the one in the URL, "
            f"but {render(url)} names none. Add the database pytest-sqla should connect to, for "
            "example /postgres on PostgreSQL."
        )
    name = derived_name(url.database, mode, settings.run_id, worker)
    length = len(name.encode()) if backend == "postgresql" else len(name)
    if length > _NAME_LIMITS[backend]:
        raise Refused(
            f"pytest-sqla: sqla_database = {mode} would name the database {name}, which is "
            f"{length} {_NAME_UNITS[backend]} long, but {_SERVERS[backend]} takes at most "
            f"{_NAME_LIMITS[backend]}. Shorten the database name in the URL."
        )
    return name


def longest_worker(config: pytest.Config) -> str | None:
    """The longest xdist worker id in this run's database names, for the length check.

    A worker knows its own. The controller knows how many it starts: xdist has
    turned ``-n`` into a number before any plugin's pytest_configure. A worker
    that fails its own check is replaced by xdist rather than reported
    (measured), so the controller is where a usage error can stop the run.
    """
    processes = config.getoption("numprocesses", None)
    started = f"gw{processes - 1}" if isinstance(processes, int) and processes > 0 else None
    return os.environ.get("PYTEST_XDIST_WORKER") or started


def describe_database(settings: Settings, *, distributed: bool) -> str:
    """The report header's ``database=`` value: the mode, and the name when it is known."""
    mode = settings.database_mode
    configured = settings.configured
    if mode == "configured" or configured is None:
        return mode
    if _sqlite.is_sqlite(configured.url):
        return f"{mode} (no effect on SQLite)"
    database = configured.url.database
    assert database is not None, "owned_name refused a URL without a database at configure time"
    worker = settings.worker or ("gwN" if distributed else None)
    return f"{mode} ({derived_name(database, mode, settings.run_id, worker)})"


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
    group.addoption(
        "--sqla-keep-database",
        action="store_true",
        default=False,
        help="Keep the databases sqla_database = run or worker created, and print their names.",
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
        "Seconds to wait for the schema lock, for DDL locks and for a test's rollback. Default 30.",
        type=_LOCK_TIMEOUT_TYPE,
        default=DEFAULT_LOCK_TIMEOUT,
    )
    parser.addini(
        "sqla_database",
        "'configured' (default): use the configured database. 'run': create one for the run. "
        "'worker': create one per xdist worker. PostgreSQL and MySQL.",
        default="configured",
    )


def read_settings(config: pytest.Config) -> Settings:
    """Everything from the command line, environment and ini, checked; raises UsageError."""
    schema_mode = str(config.getini("sqla_schema"))
    if schema_mode not in SCHEMA_MODES:
        raise pytest.UsageError(
            f"pytest-sqla: sqla_schema must be 'create' or 'none', got {schema_mode!r}."
        )
    lock_timeout = _lock_timeout(config)
    metadata_path = str(config.getini("sqla_metadata")) or None
    if metadata_path is not None and not _METADATA_PATH.fullmatch(metadata_path):
        raise pytest.UsageError(
            "pytest-sqla: sqla_metadata must be an import path like 'app.models:Base', "
            f"got {metadata_path!r}."
        )
    url = _configured_url(config, URL_ORIGIN)
    async_url = _configured_url(config, ASYNC_URL_ORIGIN)
    if url is not None and async_url is not None:
        mismatch = _mismatch(url.url, async_url.url, (url.source, async_url.source))
        if mismatch is not None:
            raise pytest.UsageError(mismatch)
    settings = Settings(
        url=url,
        async_url=async_url,
        metadata_path=metadata_path,
        schema_mode=schema_mode,
        lock_timeout=lock_timeout,
        recreate_schema=bool(config.getoption("--sqla-recreate-schema")),
        # A worker takes the id its xdist controller handed it, so the
        # controller's report header can name the databases the workers use.
        run_id=getattr(config, "workerinput", {}).get(RUN_ID_KEY)
        or os.environ.get("PYTEST_XDIST_TESTRUNUID")
        or uuid.uuid4().hex,
        worker=os.environ.get("PYTEST_XDIST_WORKER") or None,
        memory_name=f"pytest-sqla-{uuid.uuid4().hex}",
        database_mode=_database_mode(config),
        keep_database=bool(config.getoption("--sqla-keep-database")),
    )
    _check_database(config, settings)
    _check_memory(settings)
    return settings


def _database_mode(config: pytest.Config) -> str:
    mode = str(config.getini("sqla_database"))
    if mode not in DATABASE_MODES:
        raise pytest.UsageError(
            f"pytest-sqla: sqla_database must be 'configured', 'run' or 'worker', got {mode!r}."
        )
    return mode


def _check_database(config: pytest.Config, settings: Settings) -> None:
    """The configured URL must admit a database of pytest-sqla's own, if one is asked for."""
    if settings.keep_database and settings.database_mode == "configured":
        raise pytest.UsageError(
            "pytest-sqla: --sqla-keep-database keeps the databases sqla_database = run and worker "
            "create, and sqla_database = configured creates none."
        )
    configured = settings.configured
    if configured is None:
        return
    try:
        owned_name(configured.url, settings, longest_worker(config))
    except Refused as refused:
        raise pytest.UsageError(str(refused)) from None


def _check_memory(settings: Settings) -> None:
    """Neither configured URL may give each connection a database of its own, if the schema
    is pytest-sqla's to build."""
    for configured in (settings.url, settings.async_url):
        refusal = None if configured is None else _private_memory(configured.url, settings)
        if refusal is not None:
            raise pytest.UsageError(refusal)


def _private_memory(url: URL, settings: Settings) -> str | None:
    """Why the schema cannot be built in ``url``'s database, or None when it can.

    pytest-sqla builds the schema on a connection of its own and runs every
    test on another, so an in-memory URL that gives each connection a new,
    empty database leaves the tests without tables. Measured before this
    check: every test that touched one failed with "no such table", and so did
    the schema's teardown. With ``sqla_schema = none`` the tests own the
    schema, and a test's connection can build one in its own database.
    """
    if settings.schema_mode == "none" or not _sqlite.is_private_memory(url):
        return None
    return (
        f"pytest-sqla: sqla_schema = {settings.schema_mode} creates the tables on one connection "
        f"and runs every test on another, but each connection to {render(url)} gets its own "
        "empty database. Use sqlite://, which pytest-sqla shares as one memdb database for the "
        "session, or a URI that SQLite shares between connections: file:/NAME?vfs=memdb "
        "without mode=memory, or one with cache=shared."
    )


def _lock_timeout(config: pytest.Config) -> float:
    """Seconds, from a number or a numeric string; pytest may already have parsed it."""
    try:
        seconds = float(config.getini("sqla_lock_timeout"))
    # pytest 9.2 raises its own UsageError when the value does not convert, with
    # a message that, from an ini file, does not name the option; earlier
    # versions raise the ValueError or TypeError itself.
    except (TypeError, ValueError, pytest.UsageError) as error:
        raise pytest.UsageError(
            f"pytest-sqla: sqla_lock_timeout must be a positive number of seconds: {error}."
        ) from None
    if not (math.isfinite(seconds) and seconds > 0):
        raise pytest.UsageError(
            f"pytest-sqla: sqla_lock_timeout must be a positive number of seconds, got {seconds:g}."
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
        (f"the {origin.ini} ini key", str(config.getini(origin.ini)) or None),
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
        if _sqlite.is_plain_memory(url):
            return (family, None)
        # sqlite3 opens a relative path from the working directory, as this
        # does; one file reached through a symlink is still one database.
        return (family, os.path.normcase(os.path.realpath(str(url.database))))
    port = url.port if url.port is not None else _DEFAULT_PORTS.get(family)
    return (family, url.host, port, url.database, url.username)


def _mismatch(url: URL, async_url: URL, sources: tuple[str, str] | None = None) -> str | None:
    """Why the two URLs name different databases, or None when they name one.

    Backend, host, port, database and user must match; the driver may differ.
    The sources, when known, show the usual cause: a database URL set in the
    environment for CI, and an async URL still coming from the ini file.
    """
    if _identity(url) == _identity(async_url):
        return None
    one, other = render(url), render(async_url)
    if sources is not None:
        one, other = f"{one} (from {sources[0]})", f"{other} (from {sources[1]})"
    return (
        "pytest-sqla: sqla_url and sqla_async_url must point at the same database, but one "
        f"is {one} and the other {other}."
    )


@dataclass(frozen=True)
class Targets:
    """What pytest-sqla actually connects to, derived from the configured URLs.

    The configured values are never changed. An in-memory SQLite URL becomes a
    memdb name, in create mode under xdist a SQLite file gains a ``-gwN``
    suffix, and ``sqla_database = run`` or ``worker`` names a database of its
    own on the same server; the result is ``sqla_database_url``.
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
    """The database is one pytest-sqla made up - a memdb name, a per-worker
    file, or a database of its own - so it is discarded at the end instead of
    having its tables dropped."""
    owned: Owned | None = None
    """The database ``sqla_database = run`` or ``worker`` creates and drops."""

    @property
    def ddl(self) -> URL | None:
        """The URL schema DDL runs on: the sync one when there is one."""
        return self.sync or self.asynchronous


def resolve_targets(sync_value: object, async_value: object, settings: Settings) -> Targets:
    sync = _fixture_url(sync_value, "sqla_url")
    asynchronous = _fixture_url(async_value, "sqla_async_url")
    mismatch = None if sync is None or asynchronous is None else _mismatch(sync, asynchronous)
    if mismatch is not None:
        pytest.fail(mismatch, pytrace=False)
    async_setting = "sqla_url" if asynchronous == sync else "sqla_async_url"
    owned = _owned(sync or asynchronous, settings)
    memory = any(_sqlite.is_plain_memory(url) for url in (sync, asynchronous) if url is not None)
    sync = _effective(sync, settings, owned)
    asynchronous = _effective(asynchronous, settings, owned)
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
        disposable=memory or bool(files) or owned is not None,
        owned=owned,
    )


def _owned(url: URL | None, settings: Settings) -> Owned | None:
    """The database of its own this run or worker uses instead of ``url``'s, if any.

    ``url`` is the fixture's value: an override of sqla_url, such as a
    container's URL, is the base the name derives from. The same checks ran
    on the configured URL at startup; here they fail the test that asked.
    """
    try:
        name = None if url is None else owned_name(url, settings, settings.worker)
    except Refused as refused:
        pytest.fail(str(refused), pytrace=False)
    if url is None or name is None:
        return None
    return Owned(
        admin=url,
        name=name,
        mode=settings.database_mode,
        run_id=settings.run_id,
        worker=settings.worker,
        lock_timeout=settings.lock_timeout,
        keep=settings.keep_database,
    )


def _fixture_url(value: object, setting: str) -> URL | None:
    if value is None:
        return None
    url = _parse(value)
    if url is None:
        pytest.fail(invalid_url(f"the {setting} fixture"), pytrace=False)
    return url


def _effective(url: URL | None, settings: Settings, owned: Owned | None) -> URL | None:
    if url is None:
        return None
    if owned is not None:
        return url.set(database=owned.name)
    if _sqlite.is_plain_memory(url):
        _sqlite.require_shared_memory()
        return _sqlite.shared_memory_url(url, settings.memory_name)
    # The configured URLs were checked at startup; an override is known only now.
    refusal = _private_memory(url, settings)
    if refusal is not None:
        pytest.fail(refusal, pytrace=False)
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
