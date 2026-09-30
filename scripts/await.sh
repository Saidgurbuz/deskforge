#!/usr/bin/env bash
# Wait for something, but always give up eventually.
#
# Every hand-written wait loop in this project has eventually hung, in one of
# exactly two ways, and both are silent - the shell sleeps forever while the
# work it was watching is long dead:
#
#   until ! pgrep -f "cli scene --seed 2"; do sleep 30; done
#       `pgrep -f` matches the *watching shell's own command line*, because that
#       command line contains the pattern. The condition can never become true.
#       Six shells were found stuck this way, the oldest three days old.
#
#   until grep -q "ALL DONE" some.log; do sleep 150; done
#       The job died before writing its marker, so the marker never arrives.
#
# This helper removes both. Process waits are by PID, which cannot self-match.
# Every mode has a deadline and exits 124 when it expires, so a stuck wait is
# reported instead of inherited by the next session.
#
# Usage:
#   scripts/await.sh --pid 12345            [--timeout 1800] [--interval 10]
#   scripts/await.sh --file out/done.txt    [--timeout 1800]
#   scripts/await.sh --grep "ALL DONE" run.log
#   scripts/await.sh --cmd 'test -s out.json'
#
# Exit: 0 condition met, 124 timed out, 2 usage error.

set -u

MODE=""; ARG=""; ARG2=""
TIMEOUT=1800
INTERVAL=10

while [ $# -gt 0 ]; do
  case "$1" in
    --pid)      MODE=pid;  ARG="${2:-}"; shift 2 ;;
    --file)     MODE=file; ARG="${2:-}"; shift 2 ;;
    --grep)     MODE=grep; ARG="${2:-}"; ARG2="${3:-}"; shift 3 ;;
    --cmd)      MODE=cmd;  ARG="${2:-}"; shift 2 ;;
    --timeout)  TIMEOUT="${2:-}"; shift 2 ;;
    --interval) INTERVAL="${2:-}"; shift 2 ;;
    *) echo "await.sh: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

[ -n "$MODE" ] || { echo "await.sh: need one of --pid/--file/--grep/--cmd" >&2; exit 2; }

# A hard ceiling regardless of what the caller asked for. Nothing in this
# pipeline legitimately takes longer than an hour without producing output.
if [ "$TIMEOUT" -gt 3600 ]; then TIMEOUT=3600; fi

deadline=$(( $(date +%s) + TIMEOUT ))

check() {
  case "$MODE" in
    pid)  ! kill -0 "$ARG" 2>/dev/null ;;
    file) [ -e "$ARG" ] ;;
    grep) grep -q -- "$ARG" "$ARG2" 2>/dev/null ;;
    cmd)  eval "$ARG" >/dev/null 2>&1 ;;
  esac
}

while :; do
  if check; then
    exit 0
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "await.sh: TIMED OUT after ${TIMEOUT}s waiting for $MODE '$ARG' ${ARG2}" >&2
    exit 124
  fi
  sleep "$INTERVAL"
done
