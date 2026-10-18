"""The sync fixtures against a service layer that commits on its own."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from sync_app.services import InsufficientFunds, balances, open_account, transfer


def test_a_transfer_commits_through_its_own_session(
    sqla_session: Session, sqla_sessionmaker: sessionmaker[Session]
) -> None:
    open_account(sqla_session, "ada", balance=100)
    open_account(sqla_session, "grace")

    transfer(sqla_sessionmaker, "ada", "grace", 30)

    sqla_session.expire_all()
    assert balances(sqla_session) == {"ada": 70, "grace": 30}


def test_a_refused_transfer_rolls_back_only_itself(
    sqla_session: Session, sqla_sessionmaker: sessionmaker[Session]
) -> None:
    open_account(sqla_session, "ada", balance=10)
    open_account(sqla_session, "grace")

    with pytest.raises(InsufficientFunds):
        transfer(sqla_sessionmaker, "ada", "grace", 30)

    # The service's rollback went back to its own savepoint: the accounts the
    # test opened are still here.
    assert balances(sqla_session) == {"ada": 10, "grace": 0}


def test_every_commit_of_the_previous_tests_was_rolled_back(sqla_session: Session) -> None:
    assert balances(sqla_session) == {}
