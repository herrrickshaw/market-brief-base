#!/bin/bash
# tests/smoke.sh — the cheap checks: syntax, --help, and the offline self-test.
# No network, no SMTP, no venv bootstrap. Run from anywhere:
#   bash tests/smoke.sh            # uses python3 on PATH
#   PYTHON=.venv/bin/python bash tests/smoke.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
cd "$ROOT"

step() { printf '\n== %s\n' "$*"; }

step "bash -n on every shell script"
for f in run.sh tests/smoke.sh; do bash -n "$f"; echo "  ok  $f"; done

if command -v shellcheck >/dev/null 2>&1; then
  step "shellcheck"
  shellcheck run.sh tests/smoke.sh
else
  step "shellcheck not installed — skipped"
fi

step "run.sh --help (must not create a venv)"
[ -d .venv ] && had_venv=1 || had_venv=0
./run.sh --help >/dev/null
if [ "$had_venv" = 0 ] && [ -d .venv ]; then
  echo "run.sh --help created .venv" >&2; exit 1
fi
echo "  ok"

step "python -m py_compile brief.py"
"$PYTHON" -m py_compile brief.py && echo "  ok"

step "brief.py --help"
"$PYTHON" brief.py --help | grep -q -- '--self-test' && echo "  ok"

step "brief.py --self-test (offline)"
"$PYTHON" brief.py --self-test

printf '\nsmoke: OK\n'
