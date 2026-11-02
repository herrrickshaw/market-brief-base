#!/bin/bash
# run.sh — the ONLY local entry point.
# Resolves its own venv rather than inheriting `python3` from the caller's PATH.
# That inheritance is exactly what made the main pipeline behave differently
# under launchd than in a shell on 2026-07-21.
#
# Every argument is forwarded unchanged to brief.py, so the flag names launchd
# and the README use (--email, --out, --self-test) are brief.py's, not ours.
set -euo pipefail

readonly VENV_DIR=".venv"
readonly REQUIREMENTS="requirements.txt"
readonly ENV_FILE=".env"
readonly ENTRY="brief.py"
# launchd gives every job a 256-descriptor soft limit; see raise_fd_limit().
readonly FD_SOFT_LIMIT=10240
readonly FD_SOFT_LIMIT_FALLBACK=4096

usage() {
  cat <<USAGE
usage: ./run.sh [brief.py options]

Bootstraps ./$VENV_DIR from $REQUIREMENTS on first use, loads $ENV_FILE if present,
then runs: ./$VENV_DIR/bin/python $ENTRY "\$@"

  ./run.sh                 write brief.html
  ./run.sh --email         also send it (needs SMTP_* in $ENV_FILE)
  ./run.sh --self-test     offline; verifies the signal logic
  ./run.sh --out PATH      write the HTML somewhere else
  ./run.sh -h | --help     this text (brief.py --help for the full option list)
USAGE
}

warn() { printf 'run.sh: %s\n' "$*" >&2; }
die()  { warn "$*"; exit 1; }

raise_fd_limit() {
  # brief.py downloads via yfinance, which holds a socket per in-flight request;
  # past ~250 the next descriptor request fails, and it is usually sqlite,
  # surfacing as the misleading "unable to open database file".
  # -S raises only the soft limit, leaving the hard limit unlimited. Never fatal:
  # `|| true` because set -e is on here and a low limit must degrade, not abort.
  ulimit -S -n "$FD_SOFT_LIMIT" 2>/dev/null \
    || ulimit -S -n "$FD_SOFT_LIMIT_FALLBACK" 2>/dev/null \
    || true
}

ensure_venv() {
  if [ ! -d "$VENV_DIR" ]; then
    command -v python3 >/dev/null 2>&1 \
      || die "python3 not found on PATH; it is needed once to create $VENV_DIR"
    python3 -m venv "$VENV_DIR"
  fi
  "./$VENV_DIR/bin/pip" install -q -r "$REQUIREMENTS"
}

load_env() {
  # Exports every assignment in .env for brief.py. A missing file is normal
  # (CI passes secrets through the environment instead); a file that fails to
  # source is reported but, as before, does not stop the brief.
  if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090,SC1091
    . "./$ENV_FILE" || warn "$ENV_FILE could not be sourced; continuing without it"
    set +a
  fi
}

main() {
  case "${1:-}" in
    -h|--help) usage; exit 0 ;;
  esac
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  cd "$here"
  raise_fd_limit
  ensure_venv
  load_env
  exec "./$VENV_DIR/bin/python" "$ENTRY" "$@"
}

main "$@"
