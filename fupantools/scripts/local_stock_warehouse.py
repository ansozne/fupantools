#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地股票历史数据库工具。

原则：历史数据优先读取 `股票历史数据库` parquet；缺日期时再调用下载器增量补齐。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional

import pandas as pd

ROOT = Path(__import__('os').environ.get('REVIEW_WORKDIR', Path(__file__).resolve().parents[1])).expanduser().resolve()
DB_DIR = ROOT / "股票历史数据库"
DOWNLOADER = ROOT / "scripts" / "market_data_warehouse_downloader.py"
TABLES = ("stocks", "auctions", "indices", "concepts")
CORE_TABLES = ("stocks", "auctions", "indices")


def parse_day(value: str) -> date:
    if value == "today":
        return date.today()
    return datetime.strptime(value, "%Y-%m-%d").date()


def table_path(table: str, day: str) -> Path:
    return DB_DIR / table / day[:4] / f"{day}.parquet"


def available_dates(table: str = "stocks") -> List[str]:
    return sorted(p.stem for p in (DB_DIR / table).glob("*/*.parquet"))


def latest_date(table: str = "stocks") -> Optional[str]:
    dates = available_dates(table)
    return dates[-1] if dates else None


def iter_weekdays(start: str, end: str) -> Iterable[str]:
    cur = parse_day(start)
    last = parse_day(end)
    while cur <= last:
        if cur.weekday() < 5:
            yield cur.isoformat()
        cur += timedelta(days=1)


def missing_dates(start: str, end: str, tables: Iterable[str] = TABLES) -> List[str]:
    missing = []
    for day in iter_weekdays(start, end):
        if any(not table_path(table, day).exists() for table in tables):
            missing.append(day)
    return missing


def update(start: Optional[str] = None, end: str = "today", force: bool = False) -> int:
    start = start or _next_weekday_after(latest_date("stocks") or "2018-10-31")
    cmd = [
        sys.executable,
        str(DOWNLOADER),
        "--start",
        start,
        "--end",
        end,
        "--resume",
        "--sleep",
        "0.2",
        "--date-source",
        "",
        "--include-concepts",
    ]
    if force:
        cmd.append("--force")
    print("run:", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=ROOT)


def _next_weekday_after(day: str) -> str:
    cur = parse_day(day) + timedelta(days=1)
    while cur.weekday() >= 5:
        cur += timedelta(days=1)
    return cur.isoformat()


def read_table(table: str, start: str, end: str, codes: Optional[List[str]] = None) -> pd.DataFrame:
    frames = []
    for day in iter_weekdays(start, end):
        path = table_path(table, day)
        if not path.exists():
            continue
        df = pd.read_parquet(path)
        if codes and "code" in df.columns:
            df = df[df["code"].astype(str).isin(codes)]
        frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def status() -> dict:
    out = {"db_dir": str(DB_DIR), "tables": {}, "failed_total": None}
    for table in TABLES:
        dates = available_dates(table)
        out["tables"][table] = {
            "files": len(dates),
            "start": dates[0] if dates else None,
            "end": dates[-1] if dates else None,
        }
    failed_path = DB_DIR / "manifests" / "failed_dates.json"
    if failed_path.exists():
        failed = json.loads(failed_path.read_text(encoding="utf-8"))
        out["failed_total"] = len(failed)
        out["failed_dates"] = failed
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="本地股票历史数据库工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="查看本地库状态")

    p_missing = sub.add_parser("missing", help="检查工作日缺失日期")
    p_missing.add_argument("--start", required=True)
    p_missing.add_argument("--end", default="today")
    p_missing.add_argument("--include-concepts", action="store_true", help="同时检查 concepts 表")

    p_update = sub.add_parser("update", help="从最后日期后增量补库")
    p_update.add_argument("--start", default=None)
    p_update.add_argument("--end", default="today")
    p_update.add_argument("--force", action="store_true")

    p_read = sub.add_parser("read", help="读取本地 parquet 表并显示前几行")
    p_read.add_argument("table", choices=TABLES)
    p_read.add_argument("--start", required=True)
    p_read.add_argument("--end", required=True)
    p_read.add_argument("--code", action="append", default=[])
    p_read.add_argument("--head", type=int, default=10)

    args = parser.parse_args()
    if args.cmd == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    elif args.cmd == "missing":
        tables = TABLES if args.include_concepts else CORE_TABLES
        print(json.dumps(missing_dates(args.start, args.end, tables=tables), ensure_ascii=False, indent=2))
    elif args.cmd == "update":
        raise SystemExit(update(args.start, args.end, args.force))
    elif args.cmd == "read":
        df = read_table(args.table, args.start, args.end, args.code or None)
        print(f"rows={len(df)} cols={list(df.columns)}")
        print(df.head(args.head).to_string(index=False))


if __name__ == "__main__":
    main()
