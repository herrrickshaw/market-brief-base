#!/bin/bash
# run.sh — the ONLY local entry point.
# Resolves its own venv rather than inheriting `python3` from the caller's PATH.
# That inheritance is exactly what made the main pipeline behave differently
# under launchd than in a shell on 2026-07-21.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install -q -r requirements.txt
[ -f .env ] && set -a && . ./.env && set +a || true
exec ./.venv/bin/python brief.py "$@"
