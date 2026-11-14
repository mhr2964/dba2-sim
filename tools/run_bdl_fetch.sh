#!/usr/bin/env bash
# Runs the raw real-player fetch (tools/_gm_real_player_fetch_raw.py) with the
# commissioner's own BDL key, read at run time from the workspace secrets store
# (System/system-secrets.md, the "knowthew BDL key" row, env var BALLDONTLIE_KEY).
#
# Why a wrapper: the fetch reads BDL_API_KEY from the environment and never from a
# file (so the key cannot leak into the repo). This script is the one place that
# hands it over, so a single narrow permission rule -- Bash(bash tools/run_bdl_fetch.sh *)
# -- is enough to let an assistant start the fetch without ever reading, printing or
# handling the key itself. The key is exported for the python process only and is
# never echoed.
#
# Usage (from Projects/dba2):
#   bash tools/run_bdl_fetch.sh [--row "<secrets row name>"] 2009 2025 [--force]
# `--row` picks which secrets row to use (default "knowthew BDL key"; the store also
# has "bdlKey"). Alternatively `--env-file PATH VARNAME` reads VARNAME from a .env file
# (for example `--env-file ../knowthew/.env BALLDONTLIE_KEY`). A 401 from BDL means that
# key is not accepted for the endpoint (wrong key or a tier below what it needs).
set -euo pipefail

ROW="knowthew BDL key"
ENV_FILE=""
ENV_VAR=""
if [ "${1:-}" = "--row" ]; then
  ROW="${2:?--row needs a secrets row name}"
  shift 2
elif [ "${1:-}" = "--env-file" ]; then
  ENV_FILE="${2:?--env-file needs a path}"
  ENV_VAR="${3:?--env-file needs a variable name}"
  shift 3
fi

cd "$(dirname "$0")/.."
SECRETS_FILE="../../System/system-secrets.md"
if [ ! -f "$SECRETS_FILE" ]; then
  echo "Missing $SECRETS_FILE (the workspace secrets store)." >&2
  exit 1
fi
KEY="$(awk -F'|' -v row="$ROW" 'index($2, row) { gsub(/^[ \t]+|[ \t\r]+$/, "", $3); print $3; exit }' "$SECRETS_FILE")"
if [ -z "$KEY" ]; then
  echo "No '$ROW' row with a value in $SECRETS_FILE." >&2
  exit 1
fi
export BDL_API_KEY="$KEY"
exec python -m tools._gm_real_player_fetch_raw "$@"
