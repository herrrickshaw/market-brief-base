#!/bin/bash
# run.sh — the ONLY local entry point.
# Resolves its own venv rather than inheriting `python3` from the caller's PATH.
# That inheritance is exactly what made the main pipeline behave differently
# under launchd than in a shell on 2026-07-21.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# launchd gives every job a 256-descriptor soft limit and this job runs under
# com.umashankar.basebrief. brief.py downloads via yfinance, which holds a socket
# per in-flight request; past ~250 the next descriptor request fails, and it is
# usually sqlite, surfacing as the misleading "unable to open database file".
# -S raises only the soft limit, leaving the hard limit unlimited. Never fatal:
# `|| true` because set -e is on here and a low limit must degrade, not abort.
ulimit -S -n 10240 2>/dev/null || ulimit -S -n 4096 2>/dev/null || true
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install -q -r requirements.txt
[ -f .env ] && set -a && . ./.env && set +a || true
exec ./.venv/bin/python brief.py "$@"
