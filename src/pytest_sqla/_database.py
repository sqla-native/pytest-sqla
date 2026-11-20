"""A database of pytest-sqla's own: ``sqla_database = run`` and ``worker``.

**Why.** The usual way to give a test run a database of its own is a session
fixture that drops and recreates one fixed name. Under pytest-xdist every
worker runs it, and drops the database its siblings are using (the control in
``tests/integration/test_databases.py``). ``run`` creates one database per
pytest run, ``<db>_<run8>``, shared by its workers; ``worker`` one per xdist
worker, ``<db>_<run8>_<gwN>``. Both live on the configured server, created
from an AUTOCOMMIT NullPool connection to the configured database with its
credentials, because PostgreSQL runs neither CREATE nor DROP DATABASE inside
a transaction block.

**The protocol.** Under a session-level lock on the configured database -
``pg_advisory_lock`` on PostgreSQL, ``GET_LOCK`` on MySQL, both released when
the connection closes, a crashed one included - a worker creates the database
when it is missing, or joins it, and the mark counts the workers inside.
Leaving, a worker counts itself out under the same lock, and the last one out
drops the database. A late worker therefore either joins a live database or
creates it again, never one that is being dropped. The lock key derives from
the database's name, so separate runs never queue for one lock.

**Ownership.** A test plugin must never destroy what it did not create. The
mark is written right after CREATE, under the lock. On PostgreSQL it is the
database's comment, which a connection to any database of the server can
read; MySQL has no comment on a database (CREATE DATABASE takes a character
set, a collation and encryption, and SCHEMATA has no comment column), so there
it is a one-row table, ``pytest_sqla_database``, read across databases from
the same connection. A database is dropped only when all three hold, checked
under the lock right before the DROP: its name derives from this run's id (it
is an ``Owned``, never a name from elsewhere), it carries the mark, and the
mark's run id is this run's. A database with the name but not the rest is
refused at creation and left alone at the end; one that could not be marked
is dropped again, being this process's own. Inside it, ``sqla_schema =
create`` keeps its own marker and protocol; the tables go with the database.

**Alembic.** With ``sqla_schema = alembic`` the worker that creates the
database migrates it right there, under the lock, before anyone may join: the
others wait on the lock, bounded by ``sqla_lock_timeout``, and then join a
migrated database. A migration that fails drops the database again, after the
same three checks, so no worker ever joins one half migrated: the next one to
take the lock creates it again and reports the same error itself.

**Never hang.** ``sqla_lock_timeout`` bounds the lock and the lock waits of
CREATE and DROP (``lock_timeout``, ``lock_wait_timeout``), and a timeout
becomes SchemaConflictError naming the cause. PostgreSQL 13 and later drop
``WITH (FORCE)``, which ends the sessions a leaked connection keeps open;
pg_terminate_backend ends them first on every version, the only way before 13.
On MySQL a leaked connection's open transaction holds the DROP, bounded.

**Left behind.** A worker killed with ``kill -9`` never leaves, so its run's
count never reaches 0 and the database stays. Creating one, pytest-sqla lists
the databases named after the same configured database that carry another
run's mark, and the run's summary names them: one catalog query on
PostgreSQL, under the lock it already holds. On MySQL the marks are tables,
and one that another run's DROP keeps locked is skipped after a second.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any, TypeVar

from sqlalchemy import Column, Integer, MetaData, String, Table, func, inspect, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from . import SchemaConflictError
from ._config import Owned, owner_of, render
from ._engines import admin_engine, async_admin_engine
from ._schema import DATABASE_MARK_NAME, in_thread, lock_timed_out

MARK_NAME = DATABASE_MARK_NAME
_COMMENT = re.compile(r"pytest-sqla run_id=(\S+) workers=(\d+) started_at=(\S+) owner=(\S+)")

_T = TypeVar("_T")


@dataclass(frozen=True)
class Mark:
    """What pytest-sqla writes on a database it created, and reads back before a DROP."""

    run_id: str
    workers: int
    """The workers of the run inside the database; the one that brings it to 0 drops it."""
    started_at: str
    owner: str


def new_mark(owned: Owned) -> Mark:
    return Mark(
        run_id=owned.run_id,
        workers=1,
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        owner=owner_of(owned.worker),
    )


def comment(mark: Mark) -> str:
    return (
        f"pytest-sqla run_id={mark.run_id} workers={mark.workers} "
        f"started_at={mark.started_at} owner={mark.owner}"
    )


def parse_comment(value: str | None) -> Mark | None:
    """The mark in a PostgreSQL comment, or None when the comment is not pytest-sqla's."""
    match = _COMMENT.fullmatch(value or "")
    return None if match is None else Mark(match[1], int(match[2]), match[3], match[4])


def lock_key(name: str) -> int:
    """The advisory lock key for ``name``: a signed bigint from its SHA-256."""
    digest = hashlib.sha256(f"pytest-sqla:{name}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def lock_name(name: str) -> str:
    """The GET_LOCK name for ``name``: server-wide, so prefixed, and at most 64 characters."""
    return "pytest_sqla_database:" + hashlib.sha256(name.encode()).hexdigest()[:40]


def mark_table(name: str) -> Table:
    """MySQL's mark, inside the database ``name``, reached from the configured one."""
    return Table(
        MARK_NAME,
        MetaData(),
        Column("run_id", String(64), primary_key=True),
        Column("workers", Integer, nullable=False),
        Column("started_at", String(32), nullable=False),
        Column("owner", String(255), nullable=False),
        schema=name,
    )


def _quoted(connection: Connection, name: str) -> str:
    return connection.dialect.identifier_preparer.quote_identifier(name)


def _literal(value: str) -> str:
    """A SQL string literal: COMMENT takes no bound parameter."""
    return "'" + value.replace("'", "''") + "'"


def name_pattern(owned: Owned) -> str:
    """Every name derived from the configured database's, as a LIKE or GRANT pattern."""
    return re.sub(r"([\\_%])", r"\\\1", str(owned.admin.database)) + r"\_%"


# ------------------------------------------------------------ per dialect


class _Server:
    """What the protocol needs from a server: a lock, the mark, CREATE and DROP."""

    def lock(self, connection: Connection, owned: Owned) -> None:
        raise NotImplementedError

    def find(self, connection: Connection, name: str) -> tuple[bool, Mark | None]:
        """Whether the database exists, and its mark if it carries pytest-sqla's."""
        raise NotImplementedError

    def create(self, connection: Connection, owned: Owned) -> None:
        raise NotImplementedError

    def first_mark(self, connection: Connection, name: str, mark: Mark) -> None:
        raise NotImplementedError

    def mark(self, connection: Connection, name: str, mark: Mark) -> None:
        raise NotImplementedError

    def drop(self, connection: Connection, owned: Owned) -> None:
        raise NotImplementedError

    def others(self, connection: Connection, owned: Owned) -> list[tuple[str, Mark | None]]:
        """Every database named like ``owned``'s, with its mark."""
        raise NotImplementedError


_FIND_POSTGRESQL = text(
    "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = :name"
)
# Client sessions only: DROP DATABASE ends autovacuum workers itself, and a role
# that is not a superuser may not signal them.
_END_SESSIONS = text(
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
    "WHERE datname = :name AND pid <> pg_backend_pid() AND backend_type = 'client backend'"
)
_OTHERS_POSTGRESQL = text(
    "SELECT datname, shobj_description(oid, 'pg_database') FROM pg_database "
    "WHERE datname LIKE :pattern ORDER BY datname"
)


class _PostgreSQL(_Server):
    def lock(self, connection: Connection, owned: Owned) -> None:
        milliseconds = max(1, round(owned.lock_timeout * 1000))
        # A setting of this connection only: it is pytest-sqla's own, and closes with the lock.
        connection.exec_driver_sql(f"SET lock_timeout = '{milliseconds}ms'")
        try:
            connection.scalar(select(func.pg_advisory_lock(lock_key(owned.name))))
        except DBAPIError as error:
            raise _waited_too_long(owned, error) from error

    def find(self, connection: Connection, name: str) -> tuple[bool, Mark | None]:
        row = connection.execute(_FIND_POSTGRESQL, {"name": name}).first()
        return row is not None, parse_comment(None if row is None else row[0])

    def create(self, connection: Connection, owned: Owned) -> None:
        connection.exec_driver_sql(f"CREATE DATABASE {_quoted(connection, owned.name)}")

    def first_mark(self, connection: Connection, name: str, mark: Mark) -> None:
        self.mark(connection, name, mark)

    def mark(self, connection: Connection, name: str, mark: Mark) -> None:
        connection.exec_driver_sql(
            f"COMMENT ON DATABASE {_quoted(connection, name)} IS {_literal(comment(mark))}"
        )

    def drop(
        self, connection: Connection, owned: Owned, version: tuple[int | str, ...] | None = None
    ) -> None:
        """End the database's sessions, then drop it; ``version`` is for the tests."""
        server = version or connection.dialect.server_version_info or (0,)
        connection.execute(_END_SESSIONS, {"name": owned.name}).all()
        # FORCE (13+) also ends a session that connected after the line above.
        force = " WITH (FORCE)" if int(server[0]) >= 13 else ""
        connection.exec_driver_sql(f"DROP DATABASE {_quoted(connection, owned.name)}{force}")

    def others(self, connection: Connection, owned: Owned) -> list[tuple[str, Mark | None]]:
        rows = connection.execute(_OTHERS_POSTGRESQL, {"pattern": name_pattern(owned)})
        return [(name, parse_comment(value)) for name, value in rows]


_FIND_MYSQL = text("SELECT schema_name FROM information_schema.schemata WHERE schema_name = :name")
_OTHERS_MYSQL = text(
    "SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE :pattern "
    "ORDER BY schema_name"
)


class _MySQL(_Server):
    def lock(self, connection: Connection, owned: Owned) -> None:
        seconds = max(1, math.ceil(owned.lock_timeout))
        # Bounds the metadata lock waits of CREATE and DROP; this connection only.
        connection.exec_driver_sql(f"SET SESSION lock_wait_timeout = {seconds}")
        if connection.scalar(select(func.get_lock(lock_name(owned.name), seconds))) != 1:
            raise _waited_too_long(owned)

    def find(self, connection: Connection, name: str) -> tuple[bool, Mark | None]:
        exists = connection.scalar(_FIND_MYSQL, {"name": name}) is not None
        marked = exists and inspect(connection).has_table(MARK_NAME, schema=name)
        row = connection.execute(select(mark_table(name))).first() if marked else None
        return exists, None if row is None else Mark(*row)

    def create(self, connection: Connection, owned: Owned) -> None:
        connection.exec_driver_sql(f"CREATE DATABASE {_quoted(connection, owned.name)}")

    def first_mark(self, connection: Connection, name: str, mark: Mark) -> None:
        table = mark_table(name)
        table.create(connection)
        connection.execute(table.insert().values(**asdict(mark)))

    def mark(self, connection: Connection, name: str, mark: Mark) -> None:
        connection.execute(mark_table(name).update().values(workers=mark.workers))

    def drop(self, connection: Connection, owned: Owned) -> None:
        connection.exec_driver_sql(f"DROP DATABASE {_quoted(connection, owned.name)}")

    def others(self, connection: Connection, owned: Owned) -> list[tuple[str, Mark | None]]:
        # Another run's DROP holds the locks on its tables until it ends: a mark not
        # readable within a second is skipped, never a reason for this run to wait or fail.
        connection.exec_driver_sql("SET SESSION lock_wait_timeout = 1")
        names = connection.scalars(_OTHERS_MYSQL, {"pattern": name_pattern(owned)}).all()
        return [(name, self._readable_mark(connection, name)) for name in names]

    def _readable_mark(self, connection: Connection, name: str) -> Mark | None:
        try:
            return self.find(connection, name)[1]
        except DBAPIError:
            return None


_SERVERS: Mapping[str, _Server] = {
    "postgresql": _PostgreSQL(),
    "mysql": _MySQL(),
    "mariadb": _MySQL(),
}


# ------------------------------------------------------------- the protocol


def join(
    owned: Owned, engine_options: Mapping[str, Any], build: Callable[[], None] | None = None
) -> tuple[str, list[str]]:
    """Create the database, or join the workers already in it.

    ``build`` runs right after creating it, under the lock: the Alembic
    migration of ``sqla_schema = alembic``. Returns what happened, and, when
    it created the database, a line for the summary about each database
    another run left on the server.
    """
    return _on_admin(owned, engine_options, lambda connection, _: _join(connection, owned, build))


def leave(owned: Owned, engine_options: Mapping[str, Any]) -> str:
    """Count this worker out; the last one out drops the database. Returns what happened."""
    return _on_admin(owned, engine_options, _leave)


def _join(
    connection: Connection, owned: Owned, build: Callable[[], None] | None
) -> tuple[str, list[str]]:
    server = _SERVERS[connection.dialect.name]
    server.lock(connection, owned)
    exists, mark = server.find(connection, owned.name)
    if not exists:
        left_behind = _create(connection, server, owned)
        if build is not None:
            _build(connection, server, owned, build)
        return "created", left_behind
    if mark is None or mark.run_id != owned.run_id:
        raise SchemaConflictError(_not_ours(owned, mark))
    server.mark(connection, owned.name, replace(mark, workers=mark.workers + 1))
    return "joined", []


def _build(
    connection: Connection, server: _Server, owned: Owned, build: Callable[[], None]
) -> None:
    """Migrate the database just created; one that fails is dropped again, under the lock.

    Nobody can have joined it yet, and nobody will join it half migrated: a
    worker waiting on the lock finds no database, creates it again, and
    reports the same error itself. The DROP takes the three checks every DROP
    takes, because the migration ran in between and could have changed the
    mark.
    """
    try:
        build()
    except BaseException:
        if _our_mark(connection, server, owned) is not None:
            server.drop(connection, owned)
        raise


def _our_mark(connection: Connection, server: _Server, owned: Owned) -> Mark | None:
    """The database's mark when it is this run's: the second and third checks before a DROP."""
    _, mark = server.find(connection, owned.name)
    return mark if mark is not None and mark.run_id == owned.run_id else None


def _create(connection: Connection, server: _Server, owned: Owned) -> list[str]:
    """CREATE DATABASE and the mark; the summary's lines about databases other runs left.

    A database that could not be marked is dropped again: it is this
    process's, created a moment ago under the lock, and unmarked it would stay
    behind for good, refused by every later run.
    """
    try:
        server.create(connection, owned)
    except DBAPIError as error:
        raise _could_not(connection, owned, "create", error) from error
    try:
        server.first_mark(connection, owned.name, new_mark(owned))
    except DBAPIError as error:
        server.drop(connection, owned)
        raise _unmarked(owned, error) from error
    return [
        _left_behind(owned, name, other)
        for name, other in server.others(connection, owned)
        if other is not None and other.run_id != owned.run_id
    ]


def _leave(connection: Connection, owned: Owned) -> str:
    server = _SERVERS[connection.dialect.name]
    server.lock(connection, owned)
    mark = _our_mark(connection, server, owned)
    if mark is None:
        # Dropped, or marked by someone else since: not this run's to drop any more.
        return "gone"
    workers = mark.workers - 1
    if workers > 0 or owned.keep:
        server.mark(connection, owned.name, replace(mark, workers=workers))
        return "left" if workers > 0 else "kept"
    try:
        server.drop(connection, owned)
    except DBAPIError as error:
        raise _could_not(connection, owned, "drop", error) from error
    return "dropped"


def _on_admin(
    owned: Owned, engine_options: Mapping[str, Any], work: Callable[[Connection, Owned], _T]
) -> _T:
    """Run ``work`` on an AUTOCOMMIT connection to the configured database, then close it.

    Closing the connection releases the lock. An async-only URL runs on a
    thread of its own, as the schema's DDL does, away from the test loops.
    """
    if owned.admin.get_dialect().is_async:
        return in_thread(lambda: asyncio.run(_on_async_admin(owned, engine_options, work)))
    engine = admin_engine(owned.admin, engine_options)
    try:
        with engine.connect() as connection:
            return work(connection.execution_options(isolation_level="AUTOCOMMIT"), owned)
    finally:
        engine.dispose()


async def _on_async_admin(
    owned: Owned, engine_options: Mapping[str, Any], work: Callable[[Connection, Owned], _T]
) -> _T:
    engine = async_admin_engine(owned.admin, engine_options)
    try:
        async with engine.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            return await connection.run_sync(work, owned)
    finally:
        await engine.dispose()


# ----------------------------------------------------------------- messages


def _waited_too_long(owned: Owned, error: DBAPIError | None = None) -> SchemaConflictError:
    detail = "" if error is None else f" The driver reported: {error.orig}"
    migrating = (
        " With sqla_schema = alembic, the worker that creates the database runs the migrations "
        "while it holds the lock: raise sqla_lock_timeout above the time they take, or set "
        "sqla_database = worker, so that each worker migrates a database of its own."
        if owned.migrates
        else ""
    )
    return SchemaConflictError(
        f"pytest-sqla: gave up after {owned.lock_timeout:g} s waiting for the lock on the "
        f"database {render(owned.url)}; another pytest-sqla process, usually a worker of this "
        "run, is creating, joining or dropping it. A crashed process releases the lock when its "
        f"connection closes.{migrating}{detail}"
    )


def _not_ours(owned: Owned, mark: Mark | None) -> str:
    whose = (
        "does not carry pytest-sqla's mark"
        if mark is None
        else f"belongs to another pytest-sqla run (started {mark.started_at} by {mark.owner})"
    )
    return (
        f"pytest-sqla: the database {render(owned.url)} already exists and {whose}, so this run "
        "did not create it. pytest-sqla will neither use nor drop it; drop it yourself if nothing "
        "needs it."
    )


_MISSING_PRIVILEGE = {
    ("postgresql", "create", "42501"): (
        "The PostgreSQL role {user} may not create databases. Give it CREATEDB (ALTER ROLE "
        "{user} CREATEDB), connect as a role that has it, or set sqla_database = configured and "
        "create the database before the run."
    ),
    ("mysql", "create", 1044): (
        "The MySQL user {user} has no CREATE privilege on it. Grant privileges on the names "
        "pytest-sqla derives from the configured database (GRANT ALL ON `{pattern}`.* TO "
        "'{user}'@'%'), connect as a user that has them, or set sqla_database = configured and "
        "create the database before the run."
    ),
}
_MISSING_PRIVILEGE[("mariadb", "create", 1044)] = _MISSING_PRIVILEGE[("mysql", "create", 1044)]


def _code(connection: Connection, error: DBAPIError) -> object:
    """PostgreSQL's SQLSTATE, or the error number MySQL drivers put first in ``args``.

    The dialect decides which: pymysql 2 sets ``sqlstate`` too, the class
    (42000 for 1044) rather than the error.
    """
    if connection.dialect.name == "postgresql":
        return getattr(error.orig, "sqlstate", None)
    args = getattr(error.orig, "args", ())
    return args[0] if args else None


def _could_not(
    connection: Connection, owned: Owned, step: str, error: DBAPIError
) -> SchemaConflictError:
    """Why CREATE or DROP failed: a lock held too long, a missing privilege, or as the driver
    says."""
    if lock_timed_out(connection, error):
        reason = (
            f" Another connection held a lock it needs for longer than sqla_lock_timeout "
            f"({owned.lock_timeout:g} s): some code in this run opened its own connection (for "
            "example through the application's own engine) and left a transaction open. The "
            "database is left as it is."
        )
    else:
        advice = _MISSING_PRIVILEGE.get(
            (connection.dialect.name, step, _code(connection, error)), ""
        )
        reason = " " + advice.format(user=owned.admin.username, pattern=name_pattern(owned))
    return SchemaConflictError(
        f"pytest-sqla: could not {step} the database {render(owned.url)} for sqla_database = "
        f"{owned.mode}.{reason.rstrip()} The driver reported: {error.orig}"
    )


def _unmarked(owned: Owned, error: DBAPIError) -> SchemaConflictError:
    return SchemaConflictError(
        f"pytest-sqla: created the database {render(owned.url)} for sqla_database = "
        f"{owned.mode}, but could not mark it as its own, so it dropped it again. The driver "
        f"reported: {error.orig}"
    )


def _left_behind(owned: Owned, name: str, mark: Mark) -> str:
    return (
        f"{render(owned.admin.set(database=name))} is still on the server, from the pytest-sqla "
        f"run started {mark.started_at} by {mark.owner}, with {mark.workers} worker(s) counted "
        "in: that run is still going, was killed before its end, or kept it with "
        "--sqla-keep-database. Drop it if nothing uses it."
    )
