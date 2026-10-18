#!/usr/bin/env bash
# The local verification ladder. See TESTING.md for what each layer proves.
#
#   ./scripts/verify.sh          lint, types, complexity, the hermetic suite,
#                                the examples, packaging. No Docker, ~1 minute.
#   ./scripts/verify.sh --all    adds the compose databases: the whole suite on
#                                every backend under coverage (100% branch, which
#                                only the real-database layer can reach), the
#                                no-skip gate, and the examples on PostgreSQL
#                                and MySQL.
#
# Mutation testing is deliberately not here: it is an occasional, targeted
# audit, never something to run on every change, and never in CI.
set -uo pipefail

PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if [ -x .venv/bin/python ]; then PYTHON=.venv/bin/python
  else PYTHON="$(command -v python || command -v python3)"; fi
fi
BIN="$(dirname "$PYTHON")"

WITH_DB=0
[ "${1:-}" = "--all" ] && WITH_DB=1

FAILED=()
step() {
  local name="$1"; shift
  printf '\n\033[1m▶ %s\033[0m\n' "$name"
  if "$@"; then
    printf '\033[32m  ✓ %s\033[0m\n' "$name"
  else
    printf '\033[31m  ✗ %s\033[0m\n' "$name"
    FAILED+=("$name")
  fi
}

step "lint"              "$BIN/ruff" check .
step "format"            "$BIN/ruff" format --check .
step "types"             "$BIN/mypy"
step "complexity"        "$BIN/complexipy" src/pytest_sqla --max-complexity-allowed 15
step "hermetic suite"    "$PYTHON" -m pytest -q -p no:cacheprovider
step "examples"          "$PYTHON" -m pytest examples -q
step "packaged consumer" "$PYTHON" scripts/check_packed_consumer.py
step "supply chain"      "$PYTHON" scripts/audit_production_surface.py

if [ "$WITH_DB" = "1" ]; then
  step "databases: up" docker compose up -d --wait
  step "every backend, coverage, no skips" bash -c "
    source scripts/local-databases.env
    export SQLA_TEST_REQUIRE_ALL=1
    '$PYTHON' -m coverage run scripts/check_no_skips.py -q -p no:cacheprovider \
      && '$PYTHON' -m coverage report"
  step "examples on PostgreSQL" bash -c "
    source scripts/local-databases.env
    PYTEST_SQLA_URL=\$SQLA_TEST_POSTGRES_PSYCOPG_URL \
    PYTEST_SQLA_ASYNC_URL=\$SQLA_TEST_POSTGRES_ASYNCPG_URL \
      '$PYTHON' -m pytest examples -q"
  step "examples on MySQL" bash -c "
    source scripts/local-databases.env
    PYTEST_SQLA_URL=\$SQLA_TEST_MYSQL_PYMYSQL_URL \
    PYTEST_SQLA_ASYNC_URL=\$SQLA_TEST_MYSQL_AIOMYSQL_URL \
      '$PYTHON' -m pytest examples -q"
  step "databases: down" docker compose down -v
else
  printf '\n\033[33m  ~ real databases skipped (pass --all); the 100%% coverage gate needs them\033[0m\n'
fi

printf '\n'
if [ ${#FAILED[@]} -eq 0 ]; then
  printf '\033[32m▬▬ all layers passed ▬▬\033[0m\n'
  exit 0
fi
printf '\033[31m▬▬ %d failed: %s ▬▬\033[0m\n' "${#FAILED[@]}" "${FAILED[*]}"
exit 1
