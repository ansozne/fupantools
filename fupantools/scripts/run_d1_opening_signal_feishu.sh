#!/usr/bin/env bash
# Example local opening signal runner; messaging opt-in only.
set -euo pipefail
: "${REVIEW_WORKDIR:?Set REVIEW_WORKDIR to a writable workspace/data root}"
: "${REVIEW_PYTHON:=python3}"
PACKAGE_DIR="${REVIEW_PACKAGE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
TODAY="$(date '+%Y-%m-%d')"
STAMP="$(date '+%Y%m%d')"
BASE="$REVIEW_WORKDIR/output/opening_signal"
MARKER="$BASE/sent_markers/d1_sent_${STAMP}.ok"
LOGDIR="$REVIEW_WORKDIR/logs/cron"
mkdir -p "$BASE" "$LOGDIR" "$BASE/sent_markers"
export REVIEW_WORKDIR PYTHONPATH="$REVIEW_WORKDIR${PYTHONPATH:+:$PYTHONPATH}"
# Atomic mkdir lock: no automatic stale-lock deletion (avoids racing a live run).
LOCK="$BASE/.d1_send_lock_${STAMP}"
if ! mkdir "$LOCK" 2>/dev/null; then
  printf 'D1 lock exists; inspect manually before rerunning: %s\n' "$LOCK" >&2
  exit 2
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT
if [[ -f "$MARKER" && "${D1_FORCE_SEND:-0}" != 1 ]]; then
  printf 'D1 hook success marker exists, skipping: %s\n' "$MARKER"
  exit 0
fi
# Default local-only: no hook, no delivery, no sent marker.
"$REVIEW_PYTHON" "$PACKAGE_DIR/scripts/run_d1_opening_signal_backtest_style.py" \
  --date "$TODAY" --marker "$MARKER" >> "$LOGDIR/opening_signal_d1.log" 2>&1
