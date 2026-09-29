"""
03_weakest_stock_bt.py (优化版)
================================
目的（Phase 2）：验证"买入板块内涨幅最小的股票"是否产生超额收益。
优化：预加载全部收盘价宽表，避免逐事件逐只IO。

对比组：
  - 组A：买入近60日涨幅最小的5只
  - 组B：等权买入板块全部成分股（基准）
  - 超额 = A - B
"""

import json
from pathlib import Path
import numpy as np
import pandas as pd

from config import DATA_DIR, STOCK_DIR
SECTOR_FILE = DATA_DIR / "sector_constituents.json"
EVENTS_FILE = DATA_DIR / "signal_events.parquet"
OUTPUT_FILE = DATA_DIR / "weakest_stock_bt_results.parquet"

LOOKBACK = 60   # 过去60交易日涨幅排序
HOLD = 40       # 持有40交易日
TOP_N = 5       # 选最弱5只


def load_close_wide(codes: set) -> pd.DataFrame:
    """加载所有股票收盘价为宽表"""
    print(f"加载 {len(codes)} 只股票收盘价...")
    frames = {}
    loaded = 0
    for code in codes:
        f = STOCK_DIR / f"{code}_qfq.csv"
        if not f.exists():
            continue
        try:
            df = pd.read_csv(f, usecols=["日期", "收盘"], parse_dates=["日期"])
            df = df.set_index("日期").sort_index()
            df = df[df.index >= "2019-01-01"]  # 需要一些lookback空间
            if len(df) < 100:
                continue
            frames[code] = df["收盘"]
            loaded += 1
        except:
            continue
        if loaded % 500 == 0:
            print(f"  已加载 {loaded}...")

    print(f"  完成: {loaded} 只")
    return pd.DataFrame(frames).sort_index()


def main():
    print("=" * 60)
    print("Phase 2: 最弱股 vs 板块基准")
    print("=" * 60)

    # 加载事件
    events = pd.read_parquet(EVENTS_FILE)
    events["trigger_date"] = pd.to_datetime(events["trigger_date"])
    print(f"事件数: {len(events)}")

    with open(SECTOR_FILE, "r", encoding="utf-8") as f:
        sectors = json.load(f)

    # 收集所有股票代码
    all_codes = set()
    for codes in sectors.values():
        all_codes.update(codes)

    # 预加载
    close_wide = load_close_wide(all_codes)
    trade_dates = close_wide.index.tolist()

    results = []
    for _, row in events.iterrows():
        sector = row["sector"]
        trigger_date = row["trigger_date"]

        if sector not in sectors:
            continue
        codes = [c for c in sectors[sector] if c in close_wide.columns]
        if len(codes) < TOP_N * 2:
            continue

        # 找到trigger_date在trade_dates中的位置
        idx = close_wide.index.searchsorted(trigger_date)
        if idx < LOOKBACK or idx + HOLD >= len(trade_dates):
            continue

        # 过去LOOKBACK日的涨幅
        past_close = close_wide.iloc[idx - LOOKBACK:idx + 1][codes]
        past_ret = (past_close.iloc[-1] / past_close.iloc[0]) - 1
        past_ret = past_ret.dropna()

        if len(past_ret) < TOP_N * 2:
            continue

        # 选最弱TOP_N只
        weakest = past_ret.nsmallest(TOP_N).index.tolist()

        # 未来HOLD日收益
        future_close = close_wide.iloc[idx:idx + HOLD + 1][codes]
        if len(future_close) < HOLD // 2:
            continue

        # 最弱组收益
        weak_future = future_close[weakest]
        weak_ret = (weak_future.iloc[-1] / weak_future.iloc[0] - 1).mean()

        # 板块基准收益（全部成分股等权）
        valid_codes = [c for c in codes if not np.isnan(future_close[c].iloc[0])]
        if len(valid_codes) < 10:
            continue
        bench_future = future_close[valid_codes]
        bench_ret = (bench_future.iloc[-1] / bench_future.iloc[0] - 1).mean()

        results.append({
            "sector": sector,
            "trigger_date": trigger_date,
            "weak_return": round(weak_ret, 4),
            "bench_return": round(bench_ret, 4),
            "excess": round(weak_ret - bench_ret, 4),
        })

    results_df = pd.DataFrame(results)
    print(f"\n有效事件: {len(results_df)}")

    if results_df.empty:
        print("⚠️ 无结果")
        return

    print(f"\n{'=' * 60}")
    print("📊 Phase 2 结论（持有40交易日）")
    print(f"{'=' * 60}")
    print(f"  最弱组平均收益: {results_df['weak_return'].mean():.2%}")
    print(f"  板块基准平均收益: {results_df['bench_return'].mean():.2%}")
    print(f"  平均超额: {results_df['excess'].mean():.2%}")
    print(f"  超额中位数: {results_df['excess'].median():.2%}")
    print(f"  超额为正比例: {(results_df['excess'] > 0).mean():.1%}")
    print(f"  超额标准差: {results_df['excess'].std():.2%}")

    # 分行业看
    print(f"\n  按行业分 (超额均值 top/bottom 5):")
    by_sector = results_df.groupby("sector")["excess"].mean().sort_values()
    print(f"    最差: {by_sector.head(3).to_dict()}")
    print(f"    最好: {by_sector.tail(3).to_dict()}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    results_df.to_parquet(OUTPUT_FILE, index=False)
    print(f"\n✓ 结果 → {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
