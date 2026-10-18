"""One test's transaction: open it, and end it after checking nothing escaped.

The mechanism is SQLAlchemy's documented recipe, "Joining a Session into an
External Transaction (such as for test suites)", identical in the 2.0 and 2.1
docs: one connection, ``root = conn.begin()``, and sessions bound to it with
``join_transaction_mode="create_savepoint"``. Their ``commit()`` releases a
savepoint, their ``rollback()`` rolls back to one, and ``close()`` leaves the
connection open. pytest-sqla adds to the recipe:

* a **guard** SAVEPOINT right after the root ``BEGIN``. When the database ends
  the transaction on its own - MySQL's implicit commit after DDL, TRUNCATE and
  LOCK TABLES; sqlite3's executescript(); a raw COMMIT or ROLLBACK statement -
  the guard is gone, and rolling back to it fails. Measured on MySQL 8.4:
  "(1305, 'SAVEPOINT sa_savepoint_1 does not exist')". Without the guard
  nothing notices, and the test's earlier rows stay in the database.
* **commit listeners** on the connection and on its engine. Only a root COMMIT
  fires one - measured: ``session.commit()`` fires ``release_savepoint`` and
  leaves 0 rows; ``session.connection().commit()`` fires ``commit`` and leaves
  1. The engine's listener catches a second connection of pytest-sqla's own
  engine committing, such as ``sqla_session.get_bind().engine.begin()``. A root
  ``rollback()`` alone escapes nothing and is deliberately not an error.
* on MySQL and MariaDB, a **deadlock repair**. InnoDB rolls the victim's whole
  transaction back, savepoints included (errno 1213), so nothing it wrote was
  committed. Measured on MySQL 8.4 without the repair: the next ROLLBACK TO
  SAVEPOINT failed with "(1305, 'SAVEPOINT sa_savepoint_2 does not exist')",
  which replaced the deadlock in what the application saw (a flush raised
  1305; a session's ``rollback()`` after a 1213 raised it too), and the missing
  guard read as an implicit commit. So after a 1213 the next savepoint
  operation first re-creates the savepoints SQLAlchemy still holds, guard
  first: the application sees what it would in production, and the guard
  watches the rest of the test.

Breaches are reported on the test that caused them, never on a later one, and
the connection is invalidated, so that whatever broke it cannot reach the next
test through a pool.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Connection, NestedTransaction, RootTransaction
from sqlalchemy.exc import DBAPIError

from . import IsolationError

if TYPE_CHECKING:
    from sqlalchemy.engine import ExceptionContext

_REFUSED_SESSION_OPTIONS = ("bind", "binds", "join_transaction_mode")
_DEADLOCK = 1213
"""MySQL's ER_LOCK_DEADLOCK: InnoDB rolled the whole transaction back."""
_DEADLOCKING_DIALECTS = frozenset({"mysql", "mariadb"})
_SAVEPOINT_EVENTS = ("savepoint", "rollback_savepoint", "release_savepoint")


@dataclass
class Isolation:
    connection: Connection
    root: RootTransaction
    guard: NestedTransaction
    committed: bool = False
    """Set by the commit listener: the root transaction was committed."""
    escaped: bool = False
    """Another connection of pytest-sqla's engine committed during the test."""
    lost: bool = False
    """MySQL rolled the transaction back after a deadlock, savepoints included."""

    def saw_commit(self, _connection: Connection) -> None:
        self.committed = True

    def saw_engine_commit(self, connection: Connection) -> None:
        if connection is not self.connection:
            self.escaped = True

    def saw_error(self, context: ExceptionContext) -> None:
        deadlock = _errno(context.original_exception) == _DEADLOCK
        if deadlock and context.connection is self.connection:
            self.lost = True

    def repair(self, connection: Connection, *_: object) -> None:
        """Before a savepoint operation: re-create the savepoints a deadlock rolled back."""
        if not self.lost or connection.invalidated:
            return
        self.lost = False
        for name in _savepoints(connection):
            connection.dialect.do_savepoint(connection, name)

    def watch(self) -> None:
        connection = self.connection
        event.listen(connection, "commit", self.saw_commit)
        event.listen(connection.engine, "commit", self.saw_engine_commit)
        if connection.dialect.name in _DEADLOCKING_DIALECTS:
            event.listen(connection.engine, "handle_error", self.saw_error)
            for name in _SAVEPOINT_EVENTS:
                event.listen(connection, name, self.repair)

    def unwatch(self) -> None:
        """Remove the engine's listeners; the connection's go with the connection."""
        engine = self.connection.engine
        event.remove(engine, "commit", self.saw_engine_commit)
        if self.connection.dialect.name in _DEADLOCKING_DIALECTS:
            event.remove(engine, "handle_error", self.saw_error)


def _errno(error: BaseException) -> object:
    """The driver's error code: MySQL drivers put it first in ``args``."""
    return error.args[0] if error.args else None


def _savepoints(connection: Connection) -> list[str]:
    """The savepoints SQLAlchemy holds on ``connection``, the outermost first.

    SQLAlchemy names a savepoint only after its "savepoint" event has fired, so
    the names come from its nested transactions, whose ``_savepoint`` and
    ``_previous_nested`` attributes are the same in SQLAlchemy 2.0.0 and 2.1.
    """
    names = []
    nested = connection.get_nested_transaction()
    while nested is not None:
        names.append(nested._savepoint)
        nested = nested._previous_nested
    return names[::-1]


def check_session_options(options: Mapping[str, Any]) -> None:
    """Session options must not move a session off the test's connection (F13)."""
    for key in _REFUSED_SESSION_OPTIONS:
        if key in options:
            pytest.fail(
                f"pytest-sqla: sqla_session_options cannot set {key!r}; every session is bound "
                "to the test's connection with join_transaction_mode='create_savepoint'.",
                pytrace=False,
            )


def open_isolation(connection: Connection) -> Isolation:
    """``BEGIN``, the guard SAVEPOINT, and the listeners, on ``connection``.

    The caller calls ``unwatch()`` when the test is over, whatever happens.
    """
    root = connection.begin()
    guard = connection.begin_nested()
    isolation = Isolation(connection, root, guard)
    isolation.watch()
    return isolation


def end_isolation(isolation: Isolation, *, nodeid: str, fixture: str) -> IsolationError | None:
    """Roll the test transaction back; return the breach to report, if anything escaped.

    The caller has already closed its own sessions, raises what this returns,
    and closes the connection whatever happens. Nothing here calls
    ``rollback()`` on a transaction SQLAlchemy has already deassociated from
    the connection, so its "already deassociated" SAWarning cannot reach
    filterwarnings = error. A connection with a breach is invalidated: what
    broke its transaction, such as MySQL's ``SET autocommit = 1``, must not
    reach the next test through a pool.
    """
    __tracebackhide__ = True
    gone = None if isolation.committed else _rollback(isolation)
    message = _breach(isolation, gone, nodeid=nodeid, fixture=fixture)
    if message is None:
        return None
    isolation.connection.invalidate()
    return IsolationError(message)


def _rollback(isolation: Isolation) -> DBAPIError | None:
    """Roll back to the guard, then the root; the guard's error if the guard was gone.

    Once the guard is gone the root rollback may fail too - sqlite3 in PEP 249
    mode raises "cannot rollback - no transaction is active" - and the breach
    already explains it.
    """
    __tracebackhide__ = True
    gone = None
    if isolation.root.is_active and isolation.guard.is_active:
        gone = _rollback_to_guard(isolation)
    if not isolation.root.is_active:
        # Rolled back by the test, or a commit the loop guard stopped: SQLAlchemy
        # then still holds the inactive root, and closing the connection skipped
        # the pool's reset. Measured on a module loop: the next test's BEGIN
        # failed with "cannot start a transaction within a transaction".
        isolation.connection.rollback()
        return None
    with contextlib.suppress(DBAPIError) if gone is not None else contextlib.nullcontext():
        isolation.root.rollback()
    return gone


def _breach(
    isolation: Isolation, gone: DBAPIError | None, *, nodeid: str, fixture: str
) -> str | None:
    if isolation.committed:
        return (
            f"pytest-sqla: {nodeid} committed the test transaction, so its writes are now "
            "permanent and visible to later tests. Something called commit() on the "
            f"connection behind {fixture} (for example session.connection().commit() or "
            "connection.commit()); commit through a session instead, which pytest-sqla "
            "turns into a savepoint release."
        )
    if gone is not None:
        return (
            f"pytest-sqla: the database ended {nodeid}'s transaction on its own, so rows the "
            "test wrote before that point may now be permanent and visible to later tests. A "
            "raw COMMIT or ROLLBACK statement does that on any database; on MySQL and MariaDB "
            "so do DDL (CREATE, ALTER, DROP, TRUNCATE) and LOCK TABLES, which commit "
            "implicitly, and on SQLite sqlite3's executescript(). Run such statements outside "
            f"the sqla fixtures. The driver reported: {gone.orig}"
        )
    if isolation.escaped:
        return (
            f"pytest-sqla: {nodeid} committed on a second connection of the engine behind "
            f"{fixture} (for example sqla_session.get_bind().engine.begin()), so what it wrote "
            "there is permanent and visible to later tests. Use the test's own session or "
            "connection, whose work pytest-sqla rolls back."
        )
    return None


def _rollback_to_guard(isolation: Isolation) -> DBAPIError | None:
    """Roll back every savepoint inside the guard, innermost first, then the guard.

    Sessions the test left open still hold savepoints above the guard, and
    rolling back only the current one keeps SQLAlchemy's bookkeeping straight.
    An inner savepoint that is already gone is not a breach - closing sessions
    out of order discards the later ones - so only the guard's own result
    counts. A disconnect is not one either: the server rolled back, and the
    original error is what the test should see.
    """
    __tracebackhide__ = True
    connection, guard = isolation.connection, isolation.guard
    while guard.is_active:
        innermost = connection.get_nested_transaction()
        assert innermost is not None, "an active guard is always on the savepoint stack"
        try:
            innermost.rollback()
        except DBAPIError as error:
            if error.connection_invalidated:
                raise
            if innermost is guard:
                return error
    return None
