#!/usr/bin/env bash
# Example local fill-review runner; not scheduled by this package.
set -euo pipefail
: "${REVIEW_WORKDIR:?Set REVIEW_WORKDIR to a writable workspace/data root}"
: "${REVIEW_PYTHON:=python3}"
PACKAGE_DIR="${REVIEW_PACKAGE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
mkdir -p "$REVIEW_WORKDIR/logs/cron"
export REVIEW_WORKDIR PYTHONPATH="$REVIEW_WORKDIR${PYTHONPATH:+:$PYTHONPATH}"
# No personal shell profile, credentials, external message, or recipient.
"$REVIEW_PYTHON" "$PACKAGE_DIR/scripts/opening_signal_fill_review.py" \
  --date "$(date '+%Y-%m-%d')" >> "$REVIEW_WORKDIR/logs/cron/opening_signal_fill_review.log" 2>&1
