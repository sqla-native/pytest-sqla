"""Settings: precedence, configure-time checks, the report header, inertness."""

from __future__ import annotations

import sys

import pytest

from tests.helpers import HAS_PYTEST_ASYNCIO, Project

PRINT_URLS = """
def test_urls(sqla_url, sqla_async_url):
    print()
    print("sqla_url:", sqla_url)
    print("sqla_async_url:", sqla_async_url)
"""


def urls(project: Project, *args: str) -> pytest.RunResult:
    project.tests(PRINT_URLS)
    result = project.run("-s", *args)
    result.assert_outcomes(passed=1)
    return result


# ------------------------------------------------------------------ precedence


def test_the_command_line_beats_the_environment_and_the_ini(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    project.ini(sqla_url="sqlite:///ini.db")
    monkeypatch.setenv("PYTEST_SQLA_URL", "sqlite:///env.db")
    result = urls(project, "--sqla-url", "sqlite:///cli.db")
    result.stdout.fnmatch_lines(["sqla_url: sqlite:///cli.db", "sqla_async_url: sqlite:///cli.db"])
    result.stdout.fnmatch_lines(["sqla: url=sqlite:///cli.db (from --sqla-url), schema=create"])


def test_the_environment_beats_the_ini(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    project.ini(sqla_url="sqlite:///ini.db")
    monkeypatch.setenv("PYTEST_SQLA_URL", "sqlite:///env.db")
    result = urls(project)
    result.stdout.fnmatch_lines(["sqla_url: sqlite:///env.db"])
    result.stdout.fnmatch_lines(
        ["sqla: url=sqlite:///env.db (from PYTEST_SQLA_URL), schema=create"]
    )


def test_the_ini_is_the_committed_default(project: Project) -> None:
    project.ini(sqla_url="sqlite:///ini.db", sqla_schema="none")
    result = urls(project)
    result.stdout.fnmatch_lines(["sqla_url: sqlite:///ini.db"])
    result.stdout.fnmatch_lines(
        ["sqla: url=sqlite:///ini.db (from the sqla_url ini key), schema=none"]
    )


def test_the_async_url_has_the_same_precedence(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    project.ini(sqla_url="sqlite:///a.db", sqla_async_url="sqlite+aiosqlite:///ini.db")
    monkeypatch.setenv("PYTEST_SQLA_ASYNC_URL", "sqlite+aiosqlite:///a.db")
    result = urls(project, "--sqla-async-url", "sqlite+aiosqlite:///./a.db")
    result.stdout.fnmatch_lines(
        ["sqla_url: sqlite:///a.db", "sqla_async_url: sqlite+aiosqlite:///./a.db"]
    )
    result.stdout.fnmatch_lines(
        [
            "sqla: url=sqlite:///a.db (from the sqla_url ini key), "
            "async_url=sqlite+aiosqlite:///./a.db (from --sqla-async-url), schema=create"
        ]
    )


def test_the_async_url_comes_from_the_environment_before_the_ini(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    project.ini(sqla_async_url="sqlite+aiosqlite:///ini.db")
    monkeypatch.setenv("PYTEST_SQLA_ASYNC_URL", "sqlite+aiosqlite:///env.db")
    result = urls(project)
    result.stdout.fnmatch_lines(["sqla_url: None", "sqla_async_url: sqlite+aiosqlite:///env.db"])
    result.stdout.fnmatch_lines(
        ["sqla: async_url=sqlite+aiosqlite:///env.db (from PYTEST_SQLA_ASYNC_URL), schema=create"]
    )


def test_nothing_configured_means_none_and_no_header(project: Project) -> None:
    result = urls(project)
    result.stdout.fnmatch_lines(["sqla_url: None", "sqla_async_url: None"])
    result.stdout.no_fnmatch_line("sqla: *")


def test_the_header_hides_the_password(project: Project) -> None:
    project.tests("def test_nothing():\n    pass\n")
    result = project.run("--sqla-url", "postgresql+psycopg://ada:s3cret@db.example/app")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(
        ["sqla: url=postgresql+psycopg://ada:***@db.example/app (from --sqla-url), schema=create"]
    )
    assert "s3cret" not in result.stdout.str()


# ------------------------------------------------------------ fixture overrides


@pytest.mark.parametrize(
    "value",
    ['"sqlite:///{path}"', 'make_url("sqlite:///{path}")'],
    ids=["str", "URL"],
)
def test_sqla_url_can_be_overridden_with_a_str_or_a_url(project: Project, value: str) -> None:
    path = project.path / "override.db"
    project.models()
    project.conftest(
        f"""
        import pytest
        from sqlalchemy import make_url

        @pytest.fixture(scope="session")
        def sqla_url():
            return {value.format(path=path)}
        """
    )
    project.tests(
        f"""
        def test_connects_where_the_override_says(sqla_connection):
            assert sqla_connection.engine.url.database == {str(path)!r}
        """
    )
    project.run().assert_outcomes(passed=1)


def test_sqla_async_url_can_be_overridden(project: Project) -> None:
    project.ini(sqla_url="sqlite:///data.db")
    project.conftest(
        """
        import pytest

        @pytest.fixture(scope="session")
        def sqla_async_url():
            return "sqlite+aiosqlite:///data.db"
        """
    )
    project.tests(PRINT_URLS)
    result = project.run("-s")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(
        ["sqla_url: sqlite:///data.db", "sqla_async_url: sqlite+aiosqlite:///data.db"]
    )


def test_an_override_that_is_not_a_url_fails_the_database_tests(project: Project) -> None:
    project.models()
    project.conftest(
        """
        import pytest

        @pytest.fixture(scope="session")
        def sqla_url():
            return 42
        """
    )
    project.tests("def test_db(sqla_session):\n    pass\n")
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            "*pytest-sqla: the value of the sqla_url fixture is not a valid SQLAlchemy URL; "
            "expected dialect+driver://user:password@host/database."
        ]
    )


def test_overrides_must_name_the_same_database(project: Project) -> None:
    project.models()
    project.conftest(
        """
        import pytest

        @pytest.fixture(scope="session")
        def sqla_url():
            return "sqlite:///one.db"

        @pytest.fixture(scope="session")
        def sqla_async_url():
            return "sqlite+aiosqlite:///two.db"
        """
    )
    project.tests("def test_db(sqla_session):\n    pass\n")
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            "*pytest-sqla: sqla_url and sqla_async_url must point at the same database, but "
            "one is sqlite:///one.db and the other sqlite+aiosqlite:///two.db."
        ]
    )


# ------------------------------------------------- configure-time usage errors


def usage_error(project: Project, *args: str) -> pytest.RunResult:
    project.tests("def test_nothing():\n    pass\n")
    result = project.run(*args)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    return result


@pytest.mark.parametrize("value", ["sometimes", "Create"])
def test_sqla_schema_must_be_create_or_none(project: Project, value: str) -> None:
    project.ini(sqla_schema=value)
    result = usage_error(project)
    result.stderr.fnmatch_lines(
        [f"*pytest-sqla: sqla_schema must be 'create' or 'none', got {value!r}."]
    )


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("0", ", got 0."),
        ("-1", ", got -1."),
        ("nan", ", got nan."),
        ("inf", ", got inf."),
        ("soon", ": could not convert string to float: 'soon'."),
    ],
)
def test_the_lock_timeout_must_be_a_positive_number(
    project: Project, value: str, reason: str
) -> None:
    project.ini(sqla_lock_timeout=value)
    result = usage_error(project)
    result.stderr.fnmatch_lines(
        [f"*pytest-sqla: sqla_lock_timeout must be a positive number of seconds{reason}"]
    )


def test_a_fractional_lock_timeout_is_fine(project: Project) -> None:
    project.ini(sqla_lock_timeout="0.25")
    project.tests("def test_nothing():\n    pass\n")
    project.run().assert_outcomes(passed=1)


@pytest.mark.parametrize(
    ("source", "args", "variable", "ini"),
    [
        ("--sqla-url", ("--sqla-url", "postgresql://ada:s3cret@host:port/db"), None, None),
        ("PYTEST_SQLA_URL", (), ("PYTEST_SQLA_URL", "not a url, s3cret"), None),
        ("PYTEST_SQLA_URL", (), ("PYTEST_SQLA_URL", ""), None),
        ("the sqla_url ini key", (), None, ("sqla_url", "postgresql://ada:s3cret@host:port/db")),
        ("--sqla-async-url", ("--sqla-async-url", "://s3cret"), None, None),
        ("PYTEST_SQLA_ASYNC_URL", (), ("PYTEST_SQLA_ASYNC_URL", "s3cret"), None),
        ("the sqla_async_url ini key", (), None, ("sqla_async_url", "mysql://u:s3cret@h:x/d")),
    ],
)
def test_an_invalid_url_names_its_source_and_never_its_value(
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    args: tuple[str, ...],
    variable: tuple[str, str] | None,
    ini: tuple[str, str] | None,
) -> None:
    if variable is not None:
        monkeypatch.setenv(*variable)
    if ini is not None:
        project.ini(**{ini[0]: ini[1]})
    result = usage_error(project, *args)
    result.stderr.fnmatch_lines(
        [
            f"*pytest-sqla: the value of {source} is not a valid SQLAlchemy URL; "
            "expected dialect+driver://user:password@host/database."
        ]
    )
    assert "s3cret" not in result.stdout.str() + result.stderr.str()


@pytest.mark.parametrize("value", ["models.Base", "models:", ":Base", "models:Base:x", "1x:Base"])
def test_sqla_metadata_must_be_an_import_path(project: Project, value: str) -> None:
    project.ini(sqla_metadata=value)
    result = usage_error(project)
    result.stderr.fnmatch_lines(
        [
            "*pytest-sqla: sqla_metadata must be an import path like 'app.models:Base', "
            f"got {value!r}."
        ]
    )


def test_the_two_urls_must_name_the_same_database(project: Project) -> None:
    project.ini(sqla_url="postgresql+psycopg://ada:s3cret@one/app")
    result = usage_error(project, "--sqla-async-url", "postgresql+asyncpg://ada:s3cret@two/app")
    result.stderr.fnmatch_lines(
        [
            "*pytest-sqla: sqla_url and sqla_async_url must point at the same database, but "
            "one is postgresql+psycopg://ada:***@one/app (from the sqla_url ini key) and the other "
            "postgresql+asyncpg://ada:***@two/app (from --sqla-async-url)."
        ]
    )
    assert "s3cret" not in result.stderr.str()


@pytest.mark.parametrize(
    ("url", "async_url"),
    [
        ("postgresql+psycopg://ada@db/app", "postgresql+asyncpg://ada@db:5432/app"),
        ("mysql+pymysql://ada@db/app", "mariadb+asyncmy://ada@db:3306/app"),
        ("sqlite://", "sqlite+aiosqlite:///:memory:"),
        ("sqlite:///data/app.db", "sqlite+aiosqlite:///data/./app.db"),
    ],
)
def test_the_same_database_through_another_driver_is_fine(
    project: Project, url: str, async_url: str
) -> None:
    project.ini(sqla_url=url, sqla_async_url=async_url)
    project.tests("def test_nothing():\n    pass\n")
    project.run().assert_outcomes(passed=1)


def test_one_file_named_relative_and_absolute_is_one_database(project: Project) -> None:
    """The sync URL relative to the working directory, the async one absolute."""
    project.ini(
        sqla_url="sqlite:///data.db",
        sqla_async_url=f"sqlite+aiosqlite:///{project.path / 'data.db'}",
    )
    project.tests("def test_nothing():\n    pass\n")
    project.run().assert_outcomes(passed=1)


@pytest.mark.skipif(pytest.version_tuple < (9,), reason="native TOML configuration is pytest 9")
@pytest.mark.parametrize("value", ["30", "0.5"])
def test_the_lock_timeout_is_a_number_in_native_toml(pytester: pytest.Pytester, value: str) -> None:
    """A string option made pytest 9 stop with an INTERNALERROR on sqla_lock_timeout = 30."""
    asyncio = 'asyncio_default_fixture_loop_scope = "function"' if HAS_PYTEST_ASYNCIO else ""
    pytester.makepyprojecttoml(
        f"""
        [tool.pytest]
        addopts = ["-p", "no:cacheprovider"]
        filterwarnings = ["error"]
        sqla_lock_timeout = {value}
        {asyncio}
        """
    )
    pytester.makepyfile(test_inner="def test_nothing():\n    pass\n")
    pytester.runpytest().assert_outcomes(passed=1)


@pytest.mark.skipif(pytest.version_tuple < (9,), reason="native TOML configuration is pytest 9")
def test_a_quoted_lock_timeout_in_native_toml_is_a_usage_error(pytester: pytest.Pytester) -> None:
    """pytest-asyncio warns at configure time when its loop scope is unset; under this
    suite's ``filterwarnings = error`` that is an INTERNALERROR instead of the usage error
    whenever its hook runs before pytest-sqla's, which the registration order decides."""
    asyncio = 'asyncio_default_fixture_loop_scope = "function"' if HAS_PYTEST_ASYNCIO else ""
    pytester.makepyprojecttoml(
        f"""
        [tool.pytest]
        addopts = ["-p", "no:cacheprovider"]
        sqla_lock_timeout = "30"
        {asyncio}
        """
    )
    pytester.makepyfile(test_inner="def test_nothing():\n    pass\n")
    result = pytester.runpytest()
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(
        [
            "*pytest-sqla: sqla_lock_timeout must be a positive number of seconds: *config "
            "option 'sqla_lock_timeout' expects a float, got str: '30'."
        ]
    )


# ------------------------------------------------------- no database configured


def test_without_a_url_database_tests_fail_and_the_rest_pass(project: Project) -> None:
    project.models()
    project.tests(
        """
        def test_db(sqla_session):
            pass

        def test_connection(sqla_connection):
            pass

        def test_unrelated():
            pass
        """
    )
    result = project.run()
    result.assert_outcomes(passed=1, errors=2)
    result.stdout.fnmatch_lines(
        [
            "*pytest-sqla: sqla_session needs a database, and none is configured. Set "
            "--sqla-url, the PYTEST_SQLA_URL environment variable, or sqla_url in your pytest "
            "configuration (for example sqla_url = sqlite://).",
        ]
    )
    result.stdout.fnmatch_lines(["*pytest-sqla: sqla_connection needs a database*"])


def test_sqla_schema_requested_directly_needs_a_database_too(project: Project) -> None:
    project.models()
    project.tests("def test_schema(sqla_schema):\n    pass\n")
    result = project.run()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*pytest-sqla: sqla_schema needs a database, and none*"])


def test_a_run_without_database_fixtures_stays_inert(project: Project) -> None:
    """No application import, no connection, no sqlalchemy.ext.asyncio.

    A subprocess, because the outer process imported everything long ago.
    """
    project.sqlite("never-created.db")
    project.ini(sqla_metadata="boom:Base")
    project.pytester.makepyfile(boom="raise RuntimeError('imported application code')")
    project.conftest(
        """
        import sys

        def pytest_sessionfinish(session):
            print("asyncio extension imported:", "sqlalchemy.ext.asyncio" in sys.modules)
        """
    )
    project.tests("def test_unrelated():\n    pass\n")
    result = project.run_subprocess("-s")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*asyncio extension imported: False"])
    assert not (project.path / "never-created.db").exists()


def test_a_python_without_sqlite3_still_loads_the_plugin(project: Project) -> None:
    """A minimal or pyenv build can lack _sqlite3; a suite that never uses SQLite must run.

    A subprocess, because sqlite3 must be missing before the plugin is imported.
    """
    project.tests("def test_unrelated():\n    pass\n")
    project.write_ini()
    result = project.pytester.run(
        sys.executable,
        "-c",
        "import sys; sys.modules['sqlite3'] = sys.modules['_sqlite3'] = None; "
        "import pytest; raise SystemExit(pytest.main([]))",
    )
    assert result.ret == pytest.ExitCode.OK
    result.stdout.fnmatch_lines(["*1 passed*"])


def test_the_plugin_can_be_disabled(project: Project) -> None:
    project.tests("def test_db(sqla_session):\n    pass\n")
    result = project.run("-p", "no:sqla")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*fixture 'sqla_session' not found*"])
