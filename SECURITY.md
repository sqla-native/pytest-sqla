# Security Policy

## Supported versions

The latest released minor version receives security fixes.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's
[security advisory](https://github.com/sqla-native/pytest-sqla/security/advisories/new)
form rather than opening a public issue.

You can expect an acknowledgement within a few days and an assessment of
severity and remediation timeline shortly after.

## Scope

`pytest-sqla` is a testing plugin. It connects only to the URL you configure,
through `--sqla-url`, `PYTEST_SQLA_URL`, the `sqla_url` ini key or a fixture
override; it never reads an application's own `DATABASE_URL`. In
`sqla_schema = create` mode it creates and drops only the tables of the
metadata you give it, plus its own `pytest_sqla` marker table, and it refuses
to touch tables it did not create. Every URL it prints, in the report header
and in error messages, has its password hidden.

Point it at a database used only for tests. A test's writes are rolled back,
but DDL that MySQL commits implicitly, and code that connects through an
engine of its own, are outside its control.
