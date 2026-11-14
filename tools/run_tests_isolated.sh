#!/usr/bin/env bash
# Runs pytest against a SEPARATE Postgres database (`dba2_test`) so the integration
# suite never touches the dev database that holds the league you are playing with.
#
# Why: the integration tests share the dev database and delete leagues as part of
# their own cleanup, which wiped a live dev league (2026-09-21). `get_config()` reads
# DATABASE_URL from the environment before .env, so exporting a different URL for the
# test process is enough; this script builds that URL from the configured one by
# swapping the database name, never printing it.
#
# Usage (from Projects/dba2):
#   bash tools/run_tests_isolated.sh tests/integration -q -p no:cacheprovider
#   bash tools/run_tests_isolated.sh tests/integration/test_watch_v2.py -q
# The first call creates `dba2_test` in the dev container (dba2-db-1) and migrates it;
# later calls only re-run migrations (a no-op when current).
set -euo pipefail

cd "$(dirname "$0")/.."
TEST_DB="dba2_test"
CONTAINER="dba2-db-1"
DB_USER="dba2"

if ! docker exec "$CONTAINER" psql -U "$DB_USER" -d postgres -tAc \
  "select 1 from pg_database where datname = '$TEST_DB'" | grep -q 1; then
  docker exec "$CONTAINER" psql -U "$DB_USER" -d postgres -c "create database $TEST_DB" >/dev/null
  echo "created database $TEST_DB" >&2
fi

TEST_URL="$(python - <<'PY'
import re
from dba2.core.config import get_config

url = get_config().database_url
print(re.sub(r"/[^/?]+(\?|$)", r"/dba2_test\1", url, count=1))
PY
)"
export DATABASE_URL="$TEST_URL"

python -m alembic upgrade head >/dev/null
exec python -m pytest "$@"
