"""``sqla_schema = alembic``: the project's own Alembic migrations build the schema.

**Where.** Only in a database pytest-sqla made and discards, so that a failed
or half-done migration leaves nothing anyone uses: one that ``sqla_database =
run`` or ``worker`` created on PostgreSQL, MySQL or MariaDB, which the worker
that creates it migrates under the database lock (``_database``), and an
in-memory SQLite database, once per session, after the keeper opens and before
any test connects.

**How.** Each point below was measured before this design:

* On a fresh thread with no event loop. Alembic's async template calls
  ``asyncio.run()``, which fails inside a running loop or ``run_sync`` and
  leaves a "coroutine was never awaited" RuntimeWarning.
* env.py gets both the connection and the URL, because pytest-sqla cannot
  tell which kind of env.py it has: the cookbook env.py uses
  ``config.attributes["connection"]``, the templates build an engine from
  ``sqlalchemy.url``. The connection is a sync one, when a sync driver is
  configured: no sync connection exists outside a loop otherwise. The URL is
  the kind ``sqla_alembic_url`` picks, with every ``%`` doubled for the config
  parser.
* The connection is handed over idle, with no transaction begun, so Alembic
  manages its transactions exactly as its command line does:
  ``autocommit_block()`` (CREATE INDEX CONCURRENTLY) and
  ``transaction_per_migration`` behave as in production. A transaction of
  pytest-sqla's own around the build would buy nothing, since the database is
  pytest-sqla's and dropped when a migration fails, and it would break two
  things: inside one, ``autocommit_block()`` raises a bare AssertionError, and
  on SQLite an env.py with an engine of its own fails with "database is
  locked" once the busy timeout runs out.
* A guard. While the upgrade and its check run, class-level Engine listeners
  refuse every connection the migration's thread opens to another database,
  such as an env.py that prefers the application's own DATABASE_URL.
  ``do_connect`` reads the database from the driver's connect arguments and
  approves the connection record; ``connect``, inserted first, refuses a
  record ``do_connect`` never approved: an engine with ``creator=``, or an
  application's ``do_connect`` hook that returns a connection of its own
  (``insert=True`` is ignored for ``do_connect``). Other threads pass
  untouched. On servers only the database's name is compared: it derives from
  the run's id, so host aliases such as localhost and 127.0.0.1 cannot cause a
  false refusal.
* Logging. env.py's ``fileConfig()`` closes every handler, pytest's included,
  replaces the root logger's, and disables every logger its file does not
  name. The configuration from before is restored afterwards, and the
  FileHandlers it closed are reopened in append mode: otherwise caplog stays
  empty, and pytest's ``--log-file`` gets nothing more.
* Paths. Alembic resolves a relative ``script_location``,
  ``prepend_sys_path`` or ``version_locations`` against the working
  directory; pytest-sqla resolves them against the config file's directory,
  as ``%(here)s`` would, without ``chdir``: ``sys.path_importer_cache`` binds
  a relative entry to the directory of its first use. ``sys.path`` is restored
  afterwards, since Alembic prepends ``prepend_sys_path`` on every command.
* The check. A wrong ``version_locations`` silently migrates nothing, and an
  env.py can ignore what it is given, so an empty script directory is refused,
  and afterwards the heads are read through env.py, as ``alembic current``
  reads them (a custom ``version_table`` included): every revision a target
  names must lie at or below one of them.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import pytest
from sqlalchemy import event
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.exc import InvalidRequestError, MissingGreenlet

from . import SchemaConflictError, _sqlite
from ._config import ASYNC_DRIVERS, SYNC_DRIVERS, Settings, Targets, render
from ._engines import migration_engine
from ._schema import in_thread

if TYPE_CHECKING:
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy.engine.interfaces import DBAPIConnection, Dialect
    from sqlalchemy.pool import ConnectionPoolEntry

_APPROVED = "pytest_sqla_alembic"
"""What the guard's ``do_connect`` puts in a connection record's info, and ``connect`` takes."""
_DATABASE_KEYS = ("dbname", "database", "db")
"""Where server drivers take the database: psycopg ``dbname``; asyncpg and pymysql
``database``; aiomysql and asyncmy ``db``."""


@dataclass(frozen=True)
class Migration:
    """Everything the build needs, decided in the fixture before anything connects."""

    config: Config
    url: URL
    """``sqlalchemy.url``: the database to migrate, through the driver sqla_alembic_url picks."""
    rendered: str
    """``url`` as text, password included, checked to read back as ``url``."""
    connect: URL | None
    """Where ``config.attributes["connection"]`` connects: the database through
    sqla_url's driver, when that driver is synchronous."""
    engine_options: Mapping[str, Any]
    targets: tuple[str, ...]
    base: Path
    """What relative paths in the configuration are relative to: its file's directory."""


def default_config(path: Path) -> Config:
    """The ``sqla_alembic_config`` fixture's default: the file the ini key names.

    Next to an ini file, also the pyproject.toml beside it, whose
    ``[tool.alembic]`` table Alembic 1.16+ reads for the keys the ini lacks;
    Alembic skips one that does not exist. Measured, ``Config(ini)`` alone
    misses it: "No 'script_location' key found in configuration".
    """
    from alembic.config import Config

    if not path.is_file():
        pytest.fail(
            f"pytest-sqla: sqla_schema = alembic reads the Alembic configuration from {path}, "
            "which does not exist. Set sqla_alembic_config to your alembic.ini, or to a "
            "pyproject.toml with a [tool.alembic] table, relative to the rootdir; or override "
            "the sqla_alembic_config fixture.",
            pytrace=False,
        )
    if path.suffix == ".toml":
        return Config(toml_file=str(path))
    return Config(str(path), toml_file=str(path.with_name("pyproject.toml")))


def plan(
    config: Config,
    targets: Targets,
    settings: Settings,
    engine_options: Mapping[str, Any],
    rootdir: Path,
) -> Migration:
    """Pick the URL env.py gets, and check it survives being written out as text.

    SQLAlchemy before 2.0.24 changes ``%XX`` in a password when it renders a
    URL (``p%41b`` reads back as ``pAb``), so the rendered URL must parse back
    to the same URL, password included.
    """
    choice = settings.alembic_url or ("sync" if targets.sync is not None else "async")
    url = targets.sync if choice == "sync" else targets.asynchronous
    if url is None:
        setting = "sqla_url" if choice == "sync" else "sqla_async_url"
        pytest.fail(
            f"pytest-sqla: sqla_alembic_url = {choice} hands env.py the database through "
            f"{setting}'s driver, and {setting} is not set. Set it, or sqla_alembic_url to the "
            "other value.",
            pytrace=False,
        )
    rendered = url.render_as_string(hide_password=False)
    if make_url(rendered) != url:
        pytest.fail(
            "pytest-sqla: sqla_schema = alembic hands env.py the URL as text, in the "
            f"sqlalchemy.url option, and {render(url)} does not read back as the same URL. "
            "SQLAlchemy before 2.0.24 changes %XX in a password when it writes a URL out: "
            "upgrade SQLAlchemy to 2.0.24 or newer, or use a password without %.",
            pytrace=False,
        )
    sync = targets.sync
    name = config.config_file_name or config.toml_file_name
    return Migration(
        config=config,
        url=url,
        rendered=rendered,
        connect=sync if sync is not None and not sync.get_dialect().is_async else None,
        engine_options=engine_options,
        targets=settings.alembic_targets,
        base=rootdir if name is None else Path(name).absolute().parent,
    )


def migrate(migration: Migration) -> None:
    """Upgrade to every target, then check each was reached: on a thread with no event loop."""
    __tracebackhide__ = True
    in_thread(lambda: _migrate(migration))


def _migrate(migration: Migration) -> None:
    __tracebackhide__ = True
    # Imported before the logging snapshot: alembic adds a NullHandler to its
    # logger when it is first imported, and that is not env.py's doing.
    from alembic.script import ScriptDirectory

    config = migration.config
    with _restored():
        _resolve(config, migration.base)
        config.set_main_option("sqlalchemy.url", _escaped(migration.rendered))
        script = ScriptDirectory.from_config(config)
        expected = _expected(config, script, migration.targets)
        # The guard first: the connection handed over passes it like env.py's own.
        with _Guard(migration.url) as guard, _handed_over(migration):
            _upgrade(config, script, migration, guard, expected)


# ---------------------------------------------------------------- the upgrade


def _expected(config: Config, script: ScriptDirectory, targets: tuple[str, ...]) -> list[str]:
    """Every revision the targets name, looked up before anything runs.

    A relative target such as ``+1`` names no revision ``get_revisions`` can
    find, so the check afterwards could not work: it is refused here, with
    typos, before the database is touched.
    """
    from alembic.util import CommandError

    if not script.get_heads():
        locations = config.get_version_locations_list() or [str(Path(script.dir, "versions"))]
        raise SchemaConflictError(
            f"pytest-sqla: Alembic found no revisions in {', '.join(locations)}, so the upgrade "
            f"would build nothing. Check script_location and version_locations in "
            f"{_source(config)}: pytest-sqla reads relative paths from the directory of that "
            "file, as %(here)s would."
        )
    expected: list[str] = []
    for target in targets:
        try:
            expected += [found.revision for found in script.get_revisions(target)]
        except CommandError as error:
            raise SchemaConflictError(
                f"pytest-sqla: sqla_alembic_target names {target!r}, which Alembic cannot "
                f"resolve: {str(error).rstrip('.')}. Name heads, a revision, or a branch such "
                "as name@head; pytest-sqla checks afterwards that the database reached each "
                "target, which it cannot do for a relative one such as +1."
            ) from None
    return expected


def _upgrade(
    config: Config,
    script: ScriptDirectory,
    migration: Migration,
    guard: _Guard,
    expected: list[str],
) -> None:
    __tracebackhide__ = True
    from alembic import command

    try:
        # One call per target: "pre@head post@head" as one revision is not found.
        for target in migration.targets:
            command.upgrade(config, target)
        current = _current(config, script)
    except BaseException as error:
        guard.check()
        explained = _wrong_kind(error, migration)
        if explained is not None:
            raise explained from error
        raise
    reached = {
        found.revision for head in current for found in script.iterate_revisions(head, "base")
    }
    missing = [revision for revision in expected if revision not in reached]
    if missing:
        guard.check()
        recorded = f"the heads {', '.join(sorted(current))}" if current else "no revision"
        raise SchemaConflictError(
            f"pytest-sqla: after the upgrade to {' '.join(migration.targets)}, env.py records "
            f"{recorded}, which leaves out {', '.join(missing)}. env.py did not run the "
            "migrations on the database pytest-sqla created, or recorded them where its check "
            "cannot see them. Make env.py run context.run_migrations() on "
            'config.attributes["connection"], or on an engine built from the sqlalchemy.url '
            "option."
        )


def _current(config: Config, script: ScriptDirectory) -> set[str]:
    """The heads env.py records, read through env.py the way ``alembic current`` reads them."""
    from alembic.runtime.environment import EnvironmentContext

    heads: set[str] = set()

    def read(revision: Any, context: MigrationContext) -> list[Any]:
        heads.update(revision)
        return []

    with EnvironmentContext(config, script, fn=read, dont_mutate=True):
        script.run_env()
    return heads


def _wrong_kind(error: BaseException, migration: Migration) -> SchemaConflictError | None:
    """What to do when env.py's engine and the URL it got disagree on sync and async."""
    url = render(migration.url)
    driver = migration.url.get_driver_name()
    backend = migration.url.get_backend_name()
    raised = f"SQLAlchemy raised {type(error).__name__}: {str(error).rstrip('.')}."
    if isinstance(error, MissingGreenlet):
        example = SYNC_DRIVERS.get(backend, f"{backend}+<driver>")
        return SchemaConflictError(
            f"pytest-sqla: env.py opened a synchronous engine, and sqlalchemy.url, {url}, uses "
            f"the async driver {driver}. {raised} Set sqla_alembic_url = sync, which hands env.py "
            "sqla_url's driver, with sqla_url naming a synchronous driver for the database (for "
            f"example {example}://...) and sqla_async_url the async one; or make env.py async, "
            "like Alembic's async template."
        )
    if isinstance(error, InvalidRequestError) and "requires an async driver" in str(error):
        example = ASYNC_DRIVERS.get(backend, f"{backend}+<async driver>")
        return SchemaConflictError(
            f"pytest-sqla: env.py opened an async engine, and sqlalchemy.url, {url}, uses the "
            f"synchronous driver {driver}. {raised} Set sqla_alembic_url = async, which hands "
            "env.py sqla_async_url's driver, with sqla_async_url naming an async driver for the "
            f"database (for example {example}://...); or make env.py synchronous, like "
            "Alembic's generic template."
        )
    return None


@contextlib.contextmanager
def _handed_over(migration: Migration) -> Iterator[None]:
    """``config.attributes["connection"]``: an idle connection, or None without a sync driver.

    Set either way, so that a connection already in the Config, which the
    guard never saw open, is not what env.py uses; taken out afterwards.
    """
    attributes = migration.config.attributes
    with contextlib.ExitStack() as stack:
        connection = None
        if migration.connect is not None:
            engine = migration_engine(migration.connect, migration.engine_options)
            stack.callback(engine.dispose)
            connection = stack.enter_context(engine.connect())
        attributes["connection"] = connection
        stack.callback(attributes.pop, "connection", None)
        yield


# ------------------------------------------------------------------ the guard


class _Guard:
    """Refuses every connection the migration's thread opens to a database other than ``url``'s.

    Installed for the upgrade and the check, removed exactly once; connections
    that other threads open pass untouched.
    """

    def __init__(self, url: URL) -> None:
        self.url = url
        self.expected = _sqlite.keeper_target(url) if _sqlite.is_sqlite(url) else url.database
        self.thread = threading.get_ident()
        self.refusal: SchemaConflictError | None = None

    def __enter__(self) -> _Guard:
        event.listen(Engine, "do_connect", self.do_connect)
        event.listen(Engine, "connect", self.connect, insert=True)
        return self

    def __exit__(self, *_: object) -> None:
        event.remove(Engine, "connect", self.connect)
        event.remove(Engine, "do_connect", self.do_connect)

    def do_connect(
        self,
        dialect: Dialect,
        record: ConnectionPoolEntry,
        cargs: tuple[Any, ...],
        cparams: dict[str, Any],
    ) -> None:
        if threading.get_ident() != self.thread:
            return
        reached = _database_of(dialect, cargs, cparams)
        if reached != self.expected:
            self._refuse(
                f"pytest-sqla: env.py or a migration opened a {dialect.name}+{dialect.driver} "
                f"connection to {reached!r}, and sqla_schema = alembic migrates "
                f"{render(self.url)} only, so pytest-sqla refused it before it was used."
            )
        record.info[_APPROVED] = True

    def connect(self, dbapi_connection: DBAPIConnection, record: ConnectionPoolEntry) -> None:
        if threading.get_ident() != self.thread or record.info.pop(_APPROVED, False):
            return
        # SQLAlchemy 2.0.0 leaves a connection its connect event refused open.
        with contextlib.suppress(Exception):
            dbapi_connection.close()
        self._refuse(
            "pytest-sqla: refused a connection of the migration that SQLAlchemy did not make "
            "from its connect arguments: an engine made with creator=, or a do_connect hook "
            "that returns a connection of its own, such as an application's. pytest-sqla could "
            "not tell which database it reaches, and sqla_schema = alembic migrates "
            f"{render(self.url)} only."
        )

    def _refuse(self, message: str) -> NoReturn:
        self.refusal = SchemaConflictError(
            f'{message} Make env.py use config.attributes["connection"], or build its engine '
            "from the sqlalchemy.url option, which pytest-sqla sets to that database."
        )
        raise self.refusal

    def check(self) -> None:
        """Raise the refusal again, when env.py or Alembic wrapped or swallowed it."""
        if self.refusal is not None:
            raise self.refusal


def _database_of(dialect: Dialect, cargs: tuple[Any, ...], cparams: Mapping[str, Any]) -> object:
    """The database a driver is about to open, read from its connect arguments."""
    if dialect.name == "sqlite":
        return cargs[0] if cargs else None
    return next((cparams[key] for key in _DATABASE_KEYS if key in cparams), None)


# ------------------------------------------------------------------- paths


def _resolve(config: Config, base: Path) -> None:
    """Make relative paths in the configuration absolute against ``base``, as %(here)s would.

    Written back into the ini section, which Alembic reads before
    ``[tool.alembic]``, the lists with ``path_separator = newline``, which no
    path contains. The lists are read with Alembic's own getters, so a legacy
    ini without ``path_separator`` gets Alembic's own DeprecationWarning, whose
    text says what to add, as on its command line.
    """
    location = config.get_alembic_option("script_location")
    if not isinstance(location, str):
        raise SchemaConflictError(
            f"pytest-sqla: {_source(config)} sets no script_location, the directory of your "
            "migrations. Point sqla_alembic_config at the alembic.ini or pyproject.toml that "
            "sets it."
        )
    config.set_main_option("script_location", _escaped(_absolute(location, base)))
    lists = (
        ("prepend_sys_path", config.get_prepend_sys_paths_list()),
        ("version_locations", config.get_version_locations_list()),
    )
    for name, entries in lists:
        if entries:
            config.set_main_option("path_separator", "newline")
            config.set_main_option(name, _escaped("\n".join(_absolute(e, base) for e in entries)))


def _absolute(entry: str, base: Path) -> str:
    """Alembic's own rule: an entry with a colon that is not absolute is a package resource."""
    return entry if os.path.isabs(entry) or ":" in entry else str(base / entry)


def _escaped(value: str) -> str:
    """For ``set_main_option``, which reads ``%`` as interpolation."""
    return value.replace("%", "%%")


def _source(config: Config) -> str:
    return (
        config.config_file_name
        or config.toml_file_name
        or "the Config of the sqla_alembic_config fixture"
    )


# ----------------------------------------------------------------- restoring


@dataclass(frozen=True)
class _LoggerState:
    level: int
    handlers: tuple[logging.Handler, ...]
    filters: tuple[Any, ...]
    propagate: bool
    disabled: bool


_FRESH = _LoggerState(logging.NOTSET, (), (), True, False)
"""A logger created since the snapshot: as ``getLogger`` makes one."""


def _loggers() -> dict[str, logging.Logger]:
    """Every logger that exists, the root under ""."""
    found = {
        name: logger
        for name, logger in list(logging.root.manager.loggerDict.items())
        if isinstance(logger, logging.Logger)
    }
    found[""] = logging.getLogger()
    return found


def _state(logger: logging.Logger) -> _LoggerState:
    return _LoggerState(
        logger.level,
        tuple(logger.handlers),
        tuple(logger.filters),
        logger.propagate,
        logger.disabled,
    )


def _apply(logger: logging.Logger, state: _LoggerState) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    for handler in state.handlers:
        logger.addHandler(handler)
    for kept in list(logger.filters):
        logger.removeFilter(kept)
    for kept in state.filters:
        logger.addFilter(kept)
    logger.setLevel(state.level)
    logger.propagate = state.propagate
    logger.disabled = state.disabled


@contextlib.contextmanager
def _restored() -> Iterator[None]:
    """``sys.path`` and the logging configuration as they were before env.py ran.

    Public logging API only, plus reading ``logging.root.manager.loggerDict``
    to find the loggers, as ``fileConfig()`` itself does.
    """
    path = list(sys.path)
    saved = {name: _state(logger) for name, logger in _loggers().items()}
    open_files = [
        handler
        for state in saved.values()
        for handler in state.handlers
        if isinstance(handler, logging.FileHandler) and handler.stream is not None
    ]
    try:
        yield
    finally:
        sys.path[:] = path
        _restore(saved, open_files)


def _restore(saved: Mapping[str, _LoggerState], open_files: list[logging.FileHandler]) -> None:
    before = {id(handler) for state in saved.values() for handler in state.handlers}
    since: dict[int, logging.Handler] = {}
    for name, logger in _loggers().items():
        since.update((id(handler), handler) for handler in logger.handlers)
        _apply(logger, saved.get(name, _FRESH))
    for key, handler in since.items():
        if key not in before:
            handler.close()
    # fileConfig() closed them, and a FileHandler in mode "w" never reopens itself.
    for handler in open_files:
        if handler.stream is None:
            name, encoding, errors = handler.baseFilename, handler.encoding, handler.errors
            # The handler owns the stream, and closes it with itself.
            handler.setStream(open(name, "a", encoding=encoding, errors=errors))  # noqa: SIM115
