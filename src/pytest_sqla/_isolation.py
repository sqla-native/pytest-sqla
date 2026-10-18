"""One test's transaction: open it, and end it after checking nothing escaped.

The mechanism is SQLAlchemy's documented recipe, "Joining a Session into an
External Transaction (such as for test suites)", identical in the 2.0 and 2.1
docs: one connection, ``root = conn.begin()``, and sessions bound to it with
``join_transaction_mode="create_savepoint"``. Their ``commit()`` releases a
savepoint, their ``rollback()`` rolls back to one, and ``close()`` leaves the
connection open. pytest-sqla adds two things to the recipe:

* a **guard** SAVEPOINT right after the root ``BEGIN``. When the database ends
  the transaction on its own - MySQL's implicit commit after DDL, TRUNCATE and
  LOCK TABLES; sqlite3's executescript() and a raw COMMIT - the guard is gone,
  and rolling back to it fails. Measured on MySQL 8.4: "(1305, 'SAVEPOINT
  sa_savepoint_1 does not exist')". Without the guard nothing notices, and the
  test's earlier rows stay in the database.
* a **commit listener** on the connection. Only a root COMMIT fires it -
  measured: ``session.commit()`` fires ``release_savepoint`` and leaves 0
  rows; ``session.connection().commit()`` fires ``commit`` and leaves 1. A
  root ``rollback()`` alone escapes nothing and is deliberately not an error.

Both report on the test that caused the breach, never on a later one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Connection, NestedTransaction, RootTransaction
from sqlalchemy.exc import DBAPIError

from . import IsolationError

_REFUSED_SESSION_OPTIONS = ("bind", "binds", "join_transaction_mode")


@dataclass
class Isolation:
    connection: Connection
    root: RootTransaction
    guard: NestedTransaction
    committed: bool = False
    """Set by the commit listener: the root transaction was committed."""

    def saw_commit(self, _connection: Connection) -> None:
        self.committed = True


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
    """``BEGIN``, the guard SAVEPOINT, and the commit listener, on ``connection``."""
    root = connection.begin()
    guard = connection.begin_nested()
    isolation = Isolation(connection, root, guard)
    event.listen(connection, "commit", isolation.saw_commit)
    return isolation


def end_isolation(isolation: Isolation, *, nodeid: str, fixture: str) -> IsolationError | None:
    """Roll the test transaction back; return the breach to report, if anything escaped.

    The caller has already closed its own sessions, raises what this returns,
    and closes the connection whatever happens. Nothing here calls
    ``rollback()`` on a transaction SQLAlchemy has already deassociated from
    the connection, so its "already deassociated" SAWarning cannot reach
    filterwarnings = error.
    """
    __tracebackhide__ = True
    if isolation.committed:
        return IsolationError(
            f"pytest-sqla: {nodeid} committed the test transaction, so its writes are now "
            "permanent and visible to later tests. Something called commit() on the "
            f"connection behind {fixture} (for example session.connection().commit() or "
            "connection.commit()); commit through a session instead, which pytest-sqla "
            "turns into a savepoint release."
        )
    breach = None
    if isolation.root.is_active and isolation.guard.is_active:
        breach = _rollback_to_guard(isolation)
    if isolation.root.is_active:
        isolation.root.rollback()
    if breach is None:
        return None
    return IsolationError(
        f"pytest-sqla: the database ended {nodeid}'s transaction on its own, so rows the "
        "test wrote before that point are now permanent and visible to later tests. On "
        "MySQL and MariaDB, DDL (CREATE, ALTER, DROP, TRUNCATE) and LOCK TABLES commit "
        "implicitly, and on SQLite so do sqlite3's executescript() and a raw COMMIT; run "
        f"such statements outside the sqla fixtures. The driver reported: {breach.orig}"
    )


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
