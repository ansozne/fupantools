#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Backtest-style deterministic D1_STRONG_CS opening signal runner.

Goal:
- can be launched at any time of day;
- always resolves one explicit trade_date;
- for the opening send, first ensures an opening auction snapshot exists;
  same-day auctions are preferably repaired from MX (妙想) top-rank windows;
  same-day full stocks/indices/concepts are NOT required at 09:25;
- then builds premarket_strategy_v2 and D1_STRONG_CS from that same local snapshot;
- same trade_date + same local snapshot => same candidate/order set, independent of launch time.

Shareable adaptation: messaging disabled unless an explicit external hook is supplied.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(os.environ.get("REVIEW_WORKDIR", Path(__file__).resolve().parents[1])).expanduser().resolve()
WAREHOUSE_TABLES = ("stocks", "auctions", "indices", "concepts")
OPENING_AUCTION_MIN_ROWS = 50
FUYAO_LATEST = Path(os.environ.get(
    "FUYAO_AUCTION_LATEST",
    str(ROOT / "output" / "fuyao_auction_925" / "latest.json"),
))


def run(cmd: list[str], *, timeout: Optional[int] = None, env: Optional[dict[str, str]] = None) -> subprocess.CompletedProcess[str]:
    print("$ " + " ".join(cmd), flush=True)
    completed = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
    )
    if completed.stdout:
        sys.stdout.write(completed.stdout)
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)
    return completed


def local_trade_dates() -> list[str]:
    stocks_dir = ROOT / "股票历史数据库" / "stocks"
    if not stocks_dir.exists():
        return []
    return sorted(p.stem for p in stocks_dir.glob("*/*.parquet") if len(p.stem) == 10)


def latest_local_trade_date(today: Optional[str] = None) -> str:
    today = today or date.today().isoformat()
    dates = [d for d in local_trade_dates() if d <= today]
    if dates:
        return dates[-1]
    return today


def warehouse_paths(trade_date: str) -> dict[str, Path]:
    year = trade_date[:4]
    return {
        table: ROOT / "股票历史数据库" / table / year / f"{trade_date}.parquet"
        for table in WAREHOUSE_TABLES
    }


def missing_warehouse_tables(trade_date: str) -> list[str]:
    return [name for name, path in warehouse_paths(trade_date).items() if not path.exists()]


def ensure_warehouse(
    trade_date: str,
    *,
    refresh: bool = False,
    wait_seconds: int = 900,
    retry_interval: int = 60,
) -> None:
    deadline = time.time() + max(0, wait_seconds)
    attempt = 0

    while True:
        attempt += 1
        missing = missing_warehouse_tables(trade_date)
        if not refresh and not missing:
            print(f"WAREHOUSE_OK {trade_date} " + " ".join(f"{k}={v.relative_to(ROOT)}" for k, v in warehouse_paths(trade_date).items()), flush=True)
            return

        reason = "refresh requested" if refresh else f"missing tables: {','.join(missing)}"
        print(f"WAREHOUSE_DOWNLOAD attempt={attempt} {trade_date} ({reason})", flush=True)
        cmd = [
            sys.executable,
            "scripts/market_data_warehouse_downloader.py",
            "--start",
            trade_date,
            "--end",
            trade_date,
            "--include-concepts",
            "--date-source",
            "",
        ]
        if refresh:
            cmd.append("--force")
        run(cmd, timeout=900)

        missing_after = missing_warehouse_tables(trade_date)
        if not missing_after:
            print(f"WAREHOUSE_READY_AFTER_DOWNLOAD {trade_date} attempt={attempt}", flush=True)
            return

        now = time.time()
        if now >= deadline:
            raise SystemExit(f"Warehouse still incomplete for {trade_date}: {missing_after}")

        sleep_s = min(max(1, retry_interval), max(1, int(deadline - now)))
        print(
            f"WAREHOUSE_WAIT {trade_date} missing={missing_after} sleep={sleep_s}s "
            f"remaining={int(deadline - now)}s",
            flush=True,
        )
        time.sleep(sleep_s)
        refresh = False


def _table_exists_with_rows(table: str, trade_date: str, min_rows: int = 1) -> bool:
    path = warehouse_paths(trade_date).get(table)
    if not path or not path.exists():
        return False
    try:
        import pandas as pd
        return len(pd.read_parquet(path)) >= min_rows
    except Exception:
        return False


def build_mx_auction_snapshot(trade_date: str) -> Path:
    """Unavailable in shareable package; supply pre-built local auction data.

    Original MX skill invocation sourced a personal shell profile and requires
    private integrations. Configure an authorized provider separately.
    """
    raise SystemExit(f"MX auction builder not bundled for {trade_date}; provide local auction parquet or external adapter")


def apply_fuyao_latest_snapshot(trade_date: str) -> Path:
    """Read today's Fuyao latest.json directly and overlay its 09:25 data."""
    # The 09:25 D1 cron can race the collector that refreshes latest.json around
    # 09:25. If the snapshot is still yesterday's, wait briefly and re-read
    # instead of failing immediately.
    last_payload: dict | None = None
    for attempt in range(6):
        if FUYAO_LATEST.exists():
            payload = json.loads(FUYAO_LATEST.read_text(encoding="utf-8"))
            if payload.get("source") == "Fuyao" and payload.get("date") == trade_date:
                break
            last_payload = payload
        time.sleep(10)
    else:
        stale = (last_payload or {}).get("date") if last_payload else None
        raise SystemExit(
            f"Fuyao latest mismatch after retries: "
            f"source={(last_payload or {}).get('source')} date={stale} expected={trade_date}"
        )
    payload = json.loads(FUYAO_LATEST.read_text(encoding="utf-8"))
    quality = payload.get("quality") or {}
    if not quality.get("time_ok") or not quality.get("coverage_ok"):
        raise SystemExit(f"Fuyao latest failed quality gate: {quality}")

    import pandas as pd
    records = {}
    for bucket in ("top_turnover", "top_gainers", "top_losers"):
        for item in payload.get(bucket) or []:
            code = str(item.get("thscode") or "").upper()
            if code:
                records[code] = {
                    "date": trade_date, "code": code,
                    "name": item.get("name") or "", "market_type": "a股",
                    "auction_amount": item.get("turnover"),
                    "auction_pct": item.get("pct"),
                    "auction_vol_ratio": None,
                    "is_valid_auction": True,
                    "source_query": f"fuyao_latest:{FUYAO_LATEST}",
                    "fetched_at": payload.get("display_time"),
                }
    if len(records) < 40:
        raise SystemExit(f"Fuyao latest compact rows too small: {len(records)} < 40")

    path = warehouse_paths(trade_date)["auctions"]
    old = pd.read_parquet(path) if path.exists() else pd.DataFrame()
    fresh = pd.DataFrame(records.values())
    if not old.empty:
        # Keep the broader frozen table for coverage. Fuyao wins on overlapping
        # amount/pct/name fields; its compact file does not expose volume ratio.
        overlap = old[old["code"].astype(str).isin(records)][["code", "auction_vol_ratio"]]
        fresh = fresh.drop(columns=["auction_vol_ratio"]).merge(overlap, on="code", how="left")
        old = old[~old["code"].astype(str).isin(records)]
        merged = pd.concat([fresh, old], ignore_index=True, sort=False)
    else:
        merged = fresh
    merged = merged.sort_values("auction_amount", ascending=False, na_position="last").reset_index(drop=True)
    merged["auction_rank"] = range(1, len(merged) + 1)
    merged["auction_rank_base"] = len(merged)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".fuyao.tmp.parquet")
    merged.to_parquet(tmp, index=False)
    os.replace(tmp, path)
    print(f"FUYAO_LATEST_APPLIED date={trade_date} compact_rows={len(fresh)} merged_rows={len(merged)} source={FUYAO_LATEST}", flush=True)
    return path


def ensure_opening_inputs(
    trade_date: str,
    *,
    auction_source: str = "fuyao",
    refresh_auction: bool = False,
) -> None:
    """Ensure the minimum inputs needed by the 09:25 opening signal.

    Do NOT wait for same-day full warehouse tables. At 09:25 the same-day stocks
    daily table is normally unavailable; blocking on it was the root cause of
    missed sends.  The opening chain only needs:
      - trade_date auctions (local parquet; optional Fuyao overlay)
      - T-1 stocks for capacity/previous-day fields
      - concepts via premarket_strategy_v2's fallback chain
    """
    prev = latest_local_trade_date(trade_date)
    if prev >= trade_date:
        prev_dates = [d for d in local_trade_dates() if d < trade_date]
        prev = prev_dates[-1] if prev_dates else ""
    if not prev or not _table_exists_with_rows("stocks", prev, 1000):
        raise SystemExit(
            f"Missing T-1 stocks table for opening signal: trade_date={trade_date}, prev={prev or 'N/A'}"
        )
    print(f"OPENING_T1_STOCKS_OK {prev} {warehouse_paths(prev)['stocks'].relative_to(ROOT)}", flush=True)

    has_auction = _table_exists_with_rows("auctions", trade_date, OPENING_AUCTION_MIN_ROWS)
    if auction_source == "local" and not has_auction:
        raise SystemExit(f"Missing local auction snapshot for {trade_date}")
    if auction_source == "fuyao":
        if not has_auction:
            build_mx_auction_snapshot(trade_date)
        apply_fuyao_latest_snapshot(trade_date)
    elif auction_source == "mx" and (refresh_auction or not has_auction):
        build_mx_auction_snapshot(trade_date)
    elif has_auction:
        print(f"OPENING_AUCTION_OK {trade_date} {warehouse_paths(trade_date)['auctions'].relative_to(ROOT)}", flush=True)


def latest_file(pattern: str) -> Optional[Path]:
    files = sorted(ROOT.glob(pattern), key=lambda p: p.name)
    return files[-1] if files else None


def parse_generated_json_path(stdout: str, prefix: str) -> Path:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith(prefix):
            raw = line.split(prefix, 1)[1].strip()
            # opening_signal_d1_strong_cs.py has emitted both:
            #   "💾 /abs/path.json"
            # and newer annotated forms such as:
            #   "💾 正式信号: /abs/path.json"
            # Extract the actual JSON path instead of treating the label as part of the path.
            candidates = []
            if ":" in raw:
                candidates.append(raw.rsplit(":", 1)[1].strip())
            candidates.append(raw)
            candidates.extend(part.strip() for part in raw.split() if part.strip().endswith(".json"))
            for candidate in candidates:
                path = Path(candidate)
                if not path.is_absolute():
                    path = ROOT / path
                if path.exists():
                    return path
    raise SystemExit(f"Could not locate generated path from stdout prefix={prefix!r}")


def build_strategy_v2(trade_date: str) -> Path:
    completed = run([
        sys.executable,
        "scripts/premarket_strategy_v2.py",
        "--date",
        trade_date,
        "--data-source",
        "local",
        "--screening-mode",
        "top_n",
    ], timeout=300)
    return parse_generated_json_path(completed.stdout, "✅ JSON:")


def build_d1_signal(trade_date: str, capital: str, strategy_v2_path: Path) -> Path:
    completed = run([
        sys.executable,
        "scripts/opening_signal_d1_strong_cs.py",
        "--date",
        trade_date,
        "--capital",
        str(capital),
        "--strategy-v2-results",
        str(strategy_v2_path),
        "--data-source",
        "local",
        "--allow-post-open-snapshot",
    ], timeout=300)
    return parse_generated_json_path(completed.stdout, "💾")


def send_signal(signal_path: Path, marker: Optional[Path], *, dry_run: bool = False) -> None:
    """No built-in messaging transport or destination; opt-in external hook only.

    Hook gets signal path as argument and UTF-8 signal text on stdin. A success
    marker is only written when an explicitly supplied hook exits successfully.
    """
    hook = os.environ.get("REVIEW_SEND_HOOK", "")
    if dry_run or not hook:
        print(f"SEND_DISABLED signal={signal_path} (no hook invoked; no sent marker)")
        return
    hook_path = Path(hook).expanduser().resolve()
    if not hook_path.is_file() or not os.access(hook_path, os.X_OK):
        raise SystemExit("REVIEW_SEND_HOOK must be an executable file")
    data = json.loads(signal_path.read_text(encoding="utf-8"))
    message = data.get("signal_text") or signal_path.read_text(encoding="utf-8")[:3000]
    completed = subprocess.run([str(hook_path), str(signal_path)], input=message,
                               text=True, capture_output=True, timeout=120)
    if completed.returncode:
        raise SystemExit(f"Hook failed with exit code {completed.returncode}; no sent marker written")
    if marker:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"hook_success_at={datetime.now().isoformat()}\nsignal_path={signal_path}\n", encoding="utf-8")
        print(f"MARKER_WRITTEN {marker}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run D1_STRONG_CS via local/Fuyao opening auction snapshot")
    parser.add_argument("--date", default=None, help="Trade date YYYY-MM-DD; default latest local trade date <= today")
    parser.add_argument("--capital", default="1000000", help="Capital in yuan; default 1000000")
    parser.add_argument("--marker", default=None)
    parser.add_argument("--refresh-warehouse", action="store_true", help="Force re-download date-scoped warehouse snapshot before generating")
    parser.add_argument("--auction-source", choices=["fuyao", "mx", "local"], default="fuyao", help="Opening auction snapshot source; default Fuyao latest.json")
    parser.add_argument("--refresh-auction", action="store_true", help="Force refresh same-day MX auction snapshot before generating")
    parser.add_argument("--warehouse-wait-seconds", type=int, default=900, help="Wait/retry up to N seconds for same-day warehouse snapshot to become complete")
    parser.add_argument("--warehouse-retry-interval", type=int, default=60, help="Seconds between warehouse readiness retries")
    parser.add_argument("--no-send", action="store_true", help="Generate only, do not send")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    trade_date = args.date or latest_local_trade_date()
    print(f"D1_BACKTEST_STYLE_START trade_date={trade_date} generated_at={datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

    if args.refresh_warehouse:
        # Manual diagnostic mode only. Scheduled 09:25 sends should not use this,
        # because same-day full daily tables are not expected to be ready.
        ensure_warehouse(
            trade_date,
            refresh=args.refresh_warehouse,
            wait_seconds=args.warehouse_wait_seconds,
            retry_interval=args.warehouse_retry_interval,
        )
    else:
        ensure_opening_inputs(
            trade_date,
            auction_source=args.auction_source,
            refresh_auction=args.refresh_auction,
        )
    strategy_v2_path = build_strategy_v2(trade_date)
    signal_path = build_d1_signal(trade_date, args.capital, strategy_v2_path)

    if args.no_send:
        print(f"SIGNAL_PATH {signal_path.resolve()}")
        return

    marker = Path(args.marker) if args.marker else None
    if marker and not marker.is_absolute():
        marker = ROOT / marker
    send_signal(signal_path, marker, dry_run=args.dry_run)
    print(f"SIGNAL_PATH {signal_path.resolve()}")


if __name__ == "__main__":
    main()
