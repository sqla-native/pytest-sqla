"""Per-test SQLAlchemy 2.x sessions that roll back, sync and async, for pytest.

Every test gets one connection inside an outer transaction and a guard
SAVEPOINT. Sessions join it with ``join_transaction_mode="create_savepoint"``,
so the code under test can call ``commit()``, ``rollback()`` and ``close()``
freely, and everything is rolled back when the test ends::

    def test_signup(sqla_session):
        signup(sqla_session, "ada@example.com")  # commits
        assert sqla_session.scalar(select(func.count(User.id))) == 1

The fixtures live in :mod:`pytest_sqla.plugin`, which pytest loads through the
``sqla`` entry point. This module holds only the version and the exceptions.
"""

from __future__ import annotations

__all__ = [
    "IsolationError",
    "LoopMismatchError",
    "PytestSqlaError",
    "SchemaConflictError",
]

__version__ = "0.0.1"


class PytestSqlaError(Exception):
    """Base class of every error pytest-sqla raises itself."""


class SchemaConflictError(PytestSqlaError):
    """The test schema cannot be created or dropped safely.

    Raised by ``sqla_schema``: the database holds tables pytest-sqla did not
    create, or another run's schema, or a lock could not be taken in time.
    The fixture is session-scoped, so a conflict at creation fails every
    database test, and one at the drop fails the last teardown of the run.
    """


class IsolationError(PytestSqlaError):
    """A test broke the transaction meant to roll its writes back.

    Its writes escaped through a commit, or the database ended the
    transaction on its own; or it left a task using the connection, which
    pytest-sqla then closed instead of rolling back. Raised at the teardown of
    the test that caused it, so the breach is reported where it happened, not
    discovered in a later test.
    """


class LoopMismatchError(PytestSqlaError, RuntimeError):
    """An async connection was used from an event loop it does not belong to.

    Raised before the driver sees the statement, COMMIT or ROLLBACK, which
    keeps the connection usable: asyncpg's connection is left broken by a
    cross-loop call.
    """
