#!/usr/bin/env bash
# Example local D1 fill-review runner; not scheduled by this package.
set -euo pipefail

: "${REVIEW_WORKDIR:?Set REVIEW_WORKDIR to a writable data/workspace root}"
: "${REVIEW_PYTHON:=python3}"
: "${REVIEW_WAREHOUSE_MODE:=require}" # require | update
export REVIEW_WORKDIR
cd "$REVIEW_WORKDIR"
TODAY="$(date '+%Y-%m-%d')"
STOCK_PATH="$REVIEW_WORKDIR/股票历史数据库/stocks/${TODAY:0:4}/${TODAY}.parquet"
if [[ ! -f "$STOCK_PATH" ]]; then
  if [[ "$REVIEW_WAREHOUSE_MODE" == update ]]; then
    "$REVIEW_PYTHON" "${REVIEW_PACKAGE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}/scripts/local_stock_warehouse.py" update --start "$TODAY" --end "$TODAY" --force
  else
    printf 'Missing today stock parquet: %s (provide data or set REVIEW_WAREHOUSE_MODE=update with downloader installed)\n' "$STOCK_PATH" >&2
    exit 2
  fi
fi
PACKAGE_DIR="${REVIEW_PACKAGE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# Original runner sent files to a personal Feishu recipient; intentionally omitted.
"$REVIEW_PYTHON" "$PACKAGE_DIR/scripts/opening_signal_fill_review.py" --date "$TODAY"
