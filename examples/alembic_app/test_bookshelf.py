"""The migrations build the schema, and every test's commits are rolled back."""

from __future__ import annotations

from bookshelf import add_book, authors, count_books
from sqlalchemy import Connection, text
from sqlalchemy.orm import Session


def test_adding_a_book_commits(sqla_session: Session) -> None:
    add_book(sqla_session, "Dune", author="Frank Herbert")
    assert count_books(sqla_session) == 1
    assert authors(sqla_session) == ["Frank Herbert"]


def test_the_previous_tests_book_was_rolled_back(sqla_session: Session) -> None:
    assert count_books(sqla_session) == 0


def test_the_database_is_at_the_head_revision(sqla_connection: Connection) -> None:
    assert sqla_connection.scalar(text("SELECT version_num FROM alembic_version")) == "0002"
