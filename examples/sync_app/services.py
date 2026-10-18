"""A sync service layer that commits, and gets its sessions from a sessionmaker.

The shape of a Flask or plain-script application: each operation opens its own
session from a factory it was given, and commits or rolls back itself.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "account"
    __table_args__ = (CheckConstraint("balance >= 0", name="non_negative_balance"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner: Mapped[str] = mapped_column(String(100), unique=True)
    balance: Mapped[int] = mapped_column(default=0)


class Transfer(Base):
    __tablename__ = "transfer"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("account.id"))
    target_id: Mapped[int] = mapped_column(ForeignKey("account.id"))
    amount: Mapped[int]


class InsufficientFunds(Exception):
    pass


def open_account(session: Session, owner: str, balance: int = 0) -> Account:
    account = Account(owner=owner, balance=balance)
    session.add(account)
    session.commit()
    return account


def transfer(sessions: sessionmaker[Session], source: str, target: str, amount: int) -> None:
    """Move money in one transaction of its own; roll it back when it would overdraw."""
    with sessions() as session:
        payer = session.scalars(select(Account).where(Account.owner == source)).one()
        payee = session.scalars(select(Account).where(Account.owner == target)).one()
        if payer.balance < amount:
            session.rollback()
            raise InsufficientFunds(source)
        payer.balance -= amount
        payee.balance += amount
        session.add(Transfer(source_id=payer.id, target_id=payee.id, amount=amount))
        session.commit()


def balances(session: Session) -> dict[str, int]:
    return {
        owner: balance for owner, balance in session.execute(select(Account.owner, Account.balance))
    }
