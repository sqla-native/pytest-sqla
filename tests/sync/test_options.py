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
        from sqlalchemy.pool import NullPool, QueuePool

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
        '{"isolation_level": "autocommit"}',
        '{"execution_options": {"isolation_level": "AutoCommit"}}',
    ],
    ids=["isolation_level", "execution_options", "lowercase", "mixed case"],
)
def test_autocommit_is_refused(project: Project, options: str) -> None:
    """In any case: SQLAlchemy reads the level as level.replace("_", " ").upper()."""
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
            "*pytest-sqla: connect_args autocommit=True makes the driver commit every statement, "
            "so pytest-sqla could not roll a test back. Leave it unset, or set it to False."
        ]
    )


@pytest.mark.parametrize("value", ["True", "1"])
def test_any_drivers_autocommit_is_refused(project: Project, value: str) -> None:
    """psycopg, pymysql, aiomysql and asyncmy all take autocommit; refused before connecting."""
    with_options(project, "sqla_engine_options", f'{{"connect_args": {{"autocommit": {value}}}}}')
    project.ini(sqla_url="postgresql+psycopg://ada@localhost:1/app", sqla_schema="none")
    project.tests("def test_db(sqla_connection):\n    pass\n")
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [f"*pytest-sqla: connect_args autocommit={value} makes the driver commit every statement*"]
    )


@pytest.mark.parametrize("value", ["true", "false"])
def test_autocommit_in_the_url_is_refused(project: Project, value: str) -> None:
    """Measured: pymysql reads autocommit=false in a URL as true."""
    project.models()
    project.ini(sqla_url=f"mysql+pymysql://ada:s3cret@localhost:1/app?autocommit={value}")
    project.tests("def test_db(sqla_connection):\n    pass\n")
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            "*pytest-sqla: the autocommit query parameter of "
            f"mysql+pymysql://ada:***@localhost:1/app?autocommit={value} reaches the driver, "
            "which then commits every statement, so pytest-sqla could not roll a test back. "
            "Remove it from the URL."
        ]
    )
    assert "s3cret" not in result.stdout.str()


@pytest.mark.parametrize(
    "options",
    [
        '{"isolation_level": "READ UNCOMMITTED"}',
        '{"execution_options": {"isolation_level": "read_uncommitted"}}',
    ],
)
def test_dirty_reads_are_refused(project: Project, options: str) -> None:
    """Measured on MySQL: one xdist worker read another's uncommitted row."""
    with_options(project, "sqla_engine_options", options)
    project.tests("def test_db(sqla_connection):\n    pass\n")
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            "*pytest-sqla: isolation_level='READ UNCOMMITTED' lets a test read rows other tests "
            "have not committed, such as another pytest-xdist worker's, so pytest-sqla could not "
            "keep tests apart. Remove it from sqla_engine_options."
        ]
    )


QUEUE_POOL_OPTIONS = (
    '{"pool_size": 10, "max_overflow": 5, "pool_timeout": 3, "pool_use_lifo": True}'
)


def test_queue_pool_options_reach_only_pooled_engines(project: Project) -> None:
    """Copied from the application's engine, they used to make every database test error.

    "Invalid argument(s) 'pool_size' sent to create_engine()": the sync, schema
    and per-test async engines are NullPool, which takes no pool sizing.
    """
    with_options(project, "sqla_engine_options", QUEUE_POOL_OPTIONS)
    project.tests(
        """
        from models import User, count

        def test_writes(sqla_session):
            sqla_session.add(User(email="ada@example.com"))
            sqla_session.commit()
            assert count(sqla_session) == 1
        """
    )
    project.run().assert_outcomes(passed=1)


def test_queue_pool_options_reach_a_pool_of_your_own(project: Project) -> None:
    with_options(
        project, "sqla_engine_options", QUEUE_POOL_OPTIONS.replace("{", '{"poolclass": QueuePool, ')
    )
    project.tests(
        """
        def test_sized(sqla_connection):
            assert sqla_connection.engine.pool.size() == 10
        """
    )
    project.run().assert_outcomes(passed=1)


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
