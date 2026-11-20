"""A small catalogue whose schema Alembic owns: nothing here calls create_all."""

from __future__ import annotations

from sqlalchemy import String, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class Base(DeclarativeBase):
    pass


class Book(Base):
    __tablename__ = "book"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    # Added by revision 0002, after the table: the tests use it, so they prove
    # the whole chain of migrations ran.
    author: Mapped[str | None] = mapped_column(String(200))


def add_book(session: Session, title: str, author: str | None = None) -> Book:
    book = Book(title=title, author=author)
    session.add(book)
    session.commit()
    return book


def authors(session: Session) -> list[str | None]:
    return list(session.scalars(select(Book.author).order_by(Book.id)))


def count_books(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Book)) or 0
