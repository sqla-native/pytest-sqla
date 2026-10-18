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

# shellcheck source=scripts/local-databases.env
source "$(dirname "$0")/local-databases.env"
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
