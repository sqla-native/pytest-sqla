# CLAUDE.md

@AGENTS.md

@GUIDELINES_PYTEST_SQLA.md

The imported guidelines are binding. Three always-on rules:

- `join_transaction_mode="create_savepoint"` and the guard SAVEPOINT, always; a commit that escapes fails the test that caused it.
- Every async object is opened on the test's own event loop, and `sqlalchemy.ext.asyncio` is imported lazily.
- Mutation testing is a local, occasional, targeted audit — never wire it into CI.
