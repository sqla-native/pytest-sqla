"""sqla_engine_options and sqla_session_options: passed through, or refused loudly."""

from __future__ import annotations

import pytest

from tests.helpers import Project


def with_options(project: Project, fixture: str, value: str) -> Project:
    project.models()
    project.sqlite()
    project.conftest(
        f"""
        import pytest
        from sqlalchemy.orm import Session
        from sqlalchemy.pool import NullPool

        class AppSession(Session):
            pass

        @pytest.fixture(scope="session")
        def {fixture}():
            return {value}
        """
    )
    return project


def test_engine_options_reach_the_engine(project: Project) -> None:
    with_options(project, "sqla_engine_options", '{"echo": True, "poolclass": NullPool}')
    project.tests(
        """
        from sqlalchemy.pool import NullPool

        def test_engine(sqla_connection):
            assert sqla_connection.engine.echo is True
            assert isinstance(sqla_connection.engine.pool, NullPool)
        """
    )
    project.run().assert_outcomes(passed=1)


def test_session_options_reach_the_sessions(project: Project) -> None:
    with_options(
        project, "sqla_session_options", '{"class_": AppSession, "expire_on_commit": False}'
    )
    project.tests(
        """
        from conftest import AppSession
        from models import User

        def test_session(sqla_session, sqla_sessionmaker):
            assert isinstance(sqla_session, AppSession)
            assert isinstance(sqla_sessionmaker(), AppSession)
            user = User(email="ada@example.com")
            sqla_session.add(user)
            sqla_session.commit()
            assert "email" in user.__dict__  # not expired
        """
    )
    project.run().assert_outcomes(passed=1)


@pytest.mark.parametrize(
    "options",
    [
        '{"isolation_level": "AUTOCOMMIT"}',
        '{"execution_options": {"isolation_level": "AUTOCOMMIT"}}',
    ],
    ids=["isolation_level", "execution_options"],
)
def test_autocommit_is_refused(project: Project, options: str) -> None:
    with_options(project, "sqla_engine_options", options)
    project.tests("def test_db(sqla_connection):\n    pass\n")
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            "*pytest-sqla: isolation_level='AUTOCOMMIT' commits every statement, so pytest-sqla "
            "could not roll a test back. Remove it from sqla_engine_options."
        ]
    )


def test_sqlite3_autocommit_is_refused(project: Project) -> None:
    with_options(project, "sqla_engine_options", '{"connect_args": {"autocommit": True}}')
    project.tests("def test_db(sqla_connection):\n    pass\n")
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            "*pytest-sqla: connect_args autocommit=True makes sqlite3 commit every statement, so "
            "pytest-sqla could not roll a test back. Leave it unset, or set it to False."
        ]
    )


@pytest.mark.parametrize("key", ["bind", "binds", "join_transaction_mode"])
def test_session_options_cannot_move_a_session_off_the_test_connection(
    project: Project, key: str
) -> None:
    with_options(project, "sqla_session_options", f"{{{key!r}: None}}")
    project.tests("def test_db(sqla_session):\n    pass\n")
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            f"*pytest-sqla: sqla_session_options cannot set {key!r}; every session is bound to "
            "the test's connection with join_transaction_mode='create_savepoint'."
        ]
    )
