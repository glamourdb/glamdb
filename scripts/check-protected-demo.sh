#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

export GLAMDB_DEMO_CADDY="${GLAMDB_DEMO_CADDY:-$PWD/artifacts/tools/caddy-2.11.9/caddy}"
if [[ ! -x "$GLAMDB_DEMO_CADDY" ]]; then
  echo 'Real Caddy required: python3 scripts/fetch-demo-caddy.py' >&2
  exit 1
fi
.venv/bin/python -m pytest -q tests/security --tb=short --capture=tee-sys
