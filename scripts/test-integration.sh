#!/usr/bin/env bash
# Run the gated real-database suite against the compose databases.
#
#   docker compose up -d --wait
#   ./scripts/test-integration.sh            # tests/integration, -v
#   ./scripts/test-integration.sh -x -q      # extra pytest arguments pass through
#   docker compose down -v
#
# SQLA_TEST_REQUIRE_ALL=1 turns a missing URL or driver into a failure, the way
# CI runs it: a skipped real-database suite reporting green proves nothing.
set -euo pipefail

export SQLA_TEST_POSTGRES_PSYCOPG_URL="postgresql+psycopg://sqla:sqla@127.0.0.1:55434/sqla"
export SQLA_TEST_POSTGRES_ASYNCPG_URL="postgresql+asyncpg://sqla:sqla@127.0.0.1:55434/sqla"
export SQLA_TEST_MYSQL_PYMYSQL_URL="mysql+pymysql://sqla:sqla@127.0.0.1:53308/sqla"
export SQLA_TEST_MYSQL_AIOMYSQL_URL="mysql+aiomysql://sqla:sqla@127.0.0.1:53308/sqla"
export SQLA_TEST_MYSQL_ASYNCMY_URL="mysql+asyncmy://sqla:sqla@127.0.0.1:53308/sqla"
export SQLA_TEST_REQUIRE_ALL="${SQLA_TEST_REQUIRE_ALL:-1}"

# `python` is not always on PATH outside an activated virtualenv or CI, and a
# script that only works in one of those is a script that gets abandoned.
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if [ -x .venv/bin/python ]; then
    PYTHON=.venv/bin/python
  else
    PYTHON="$(command -v python || command -v python3)"
  fi
fi

if [ "$#" -eq 0 ]; then
  set -- tests/integration -v
fi
exec "$PYTHON" scripts/check_no_skips.py "$@"
