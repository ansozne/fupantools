"""
02_signal_stats.py (优化版)
============================
目的（Phase 1）：统计250日新高信号的全样本表现。
核心改进：预加载所有收盘价到内存（宽表），避免逐事件逐只读CSV。

核心问题：
  1. 信号去重后总共触发多少次？
  2. 触发后30/60/90日板块整体涨幅分布？
  3. 有多少"假信号"（触发后60日板块下跌>10%）？
  4. 年度/行业分布

信号定义：
  - 触发条件：板块250日新高比例 >= 20%
  - 去重规则：同一板块在60天内的多次触发合并为一个事件（取首次）
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

from config import DATA_DIR, STOCK_DIR
SECTOR_FILE = DATA_DIR / "sector_constituents.json"
RATIO_FILE = DATA_DIR / "sector_new_high_ratio.parquet"
OUTPUT_EVENTS = DATA_DIR / "signal_events.parquet"
OUTPUT_SUMMARY = DATA_DIR / "signal_stats_summary.json"

# 参数
TRIGGER_THRESHOLD = 0.20
DEDUP_WINDOW = 60
FORWARD_DAYS = [30, 60, 90]


def identify_signal_events(ratio_df: pd.DataFrame) -> pd.DataFrame:
    """提取独立信号事件（同板块60天去重）"""
    triggered = ratio_df[ratio_df["ratio"] >= TRIGGER_THRESHOLD].copy()
    triggered = triggered.sort_values(["sector", "date"]).reset_index(drop=True)

    events = []
    last_event = {}

    for _, row in triggered.iterrows():
        sector = row["sector"]
        date = row["date"]

        if sector in last_event:
            if (date - last_event[sector]).days < DEDUP_WINDOW:
                continue

        last_event[sector] = date
        events.append({
            "sector": sector,
            "trigger_date": date,
            "trigger_ratio": row["ratio"],
            "new_high_count": row["new_high_count"],
            "total_count": row["total_count"],
        })

    return pd.DataFrame(events)


def load_sector_returns(sectors: dict) -> dict:
    """
    预计算每个行业的等权日收益率序列。
    返回 {行业名: pd.Series(日收益率, index=date)}
    """
    print("预计算各行业等权日收益率...")
    sector_returns = {}

    for sector_name, codes in sectors.items():
        returns_list = []
        loaded = 0

        for code in codes:
            file = STOCK_DIR / f"{code}_qfq.csv"
            if not file.exists():
                continue
            try:
                df = pd.read_csv(file, usecols=["日期", "收盘"], parse_dates=["日期"])
                df = df.rename(columns={"日期": "date", "收盘": "close"})
                df = df.set_index("date").sort_index()
                df = df[df.index >= "2020-01-01"]
                if len(df) < 50:
                    continue
                ret = df["close"].pct_change()
                returns_list.append(ret)
                loaded += 1
            except Exception:
                continue

        if loaded < 10:
            continue

        # 等权平均
        ret_df = pd.concat(returns_list, axis=1)
        avg_ret = ret_df.mean(axis=1)
        sector_returns[sector_name] = avg_ret
        print(f"  {sector_name}: {loaded} 只股票")

    return sector_returns


def calc_forward_stats(events_df: pd.DataFrame, sector_returns: dict) -> pd.DataFrame:
    """对每个事件计算前瞻收益和最大回撤"""
    print(f"\n计算 {len(events_df)} 个事件的前瞻收益...")
    results = []

    for _, row in events_df.iterrows():
        sector = row["sector"]
        trigger_date = row["trigger_date"]

        if sector not in sector_returns:
            continue

        ret_series = sector_returns[sector]
        # 取触发日之后的收益
        future = ret_series[ret_series.index >= trigger_date]
        if len(future) < 20:
            continue

        # 累计净值
        cum_nav = (1 + future).cumprod()

        result = {
            "sector": sector,
            "trigger_date": trigger_date,
            "trigger_ratio": row["trigger_ratio"],
        }

        for days in FORWARD_DAYS:
            if len(cum_nav) > days:
                ret = cum_nav.iloc[days] / cum_nav.iloc[0] - 1
                result[f"ret_{days}d"] = round(ret, 4)

                # 期间最大回撤
                period_nav = cum_nav.iloc[:days+1]
                running_max = period_nav.cummax()
                drawdown = (period_nav - running_max) / running_max
                result[f"maxdd_{days}d"] = round(drawdown.min(), 4)
            else:
                result[f"ret_{days}d"] = None
                result[f"maxdd_{days}d"] = None

        results.append(result)

    return pd.DataFrame(results)


def main():
    print("=" * 60)
    print("Phase 1: 250日新高信号全样本统计")
    print("=" * 60)

    # 1. 加载数据
    ratio_df = pd.read_parquet(RATIO_FILE)
    ratio_df["date"] = pd.to_datetime(ratio_df["date"])
    print(f"加载 ratio 数据: {len(ratio_df)} 条")

    with open(SECTOR_FILE, "r", encoding="utf-8") as f:
        sectors = json.load(f)
    print(f"加载板块: {len(sectors)} 个")

    # 2. 提取独立事件
    events_df = identify_signal_events(ratio_df)
    print(f"\n独立信号事件: {len(events_df)} 次")
    print(f"涉及板块数: {events_df['sector'].nunique()}")
    print(f"日期范围: {events_df['trigger_date'].min().date()} ~ {events_df['trigger_date'].max().date()}")

    # 年度分布
    events_df["year"] = events_df["trigger_date"].dt.year
    print(f"\n年度分布:")
    for year, count in events_df.groupby("year").size().items():
        print(f"  {year}: {count} 次")

    # 3. 预加载行业收益率
    sector_returns = load_sector_returns(sectors)

    # 4. 计算前瞻收益
    results_df = calc_forward_stats(events_df, sector_returns)
    print(f"\n有效事件（有前瞻数据）: {len(results_df)}")

    if results_df.empty:
        print("⚠️ 没有有效结果")
        return

    # 5. 汇总统计
    summary = {
        "total_events": len(results_df),
        "unique_sectors": int(results_df["sector"].nunique()),
    }

    print(f"\n{'=' * 60}")
    print("📊 关键结论")
    print(f"{'=' * 60}")

    for days in FORWARD_DAYS:
        col = f"ret_{days}d"
        dd_col = f"maxdd_{days}d"
        if col in results_df.columns:
            valid = results_df[col].dropna()
            dd_valid = results_df[dd_col].dropna()

            stats = {
                "count": int(len(valid)),
                "mean": round(float(valid.mean()), 4),
                "median": round(float(valid.median()), 4),
                "std": round(float(valid.std()), 4),
                "win_rate": round(float((valid > 0).mean()), 4),
                "big_loss_rate": round(float((valid < -0.10).mean()), 4),
                "avg_maxdd": round(float(dd_valid.mean()), 4),
                "percentiles": {
                    "10%": round(float(valid.quantile(0.10)), 4),
                    "25%": round(float(valid.quantile(0.25)), 4),
                    "75%": round(float(valid.quantile(0.75)), 4),
                    "90%": round(float(valid.quantile(0.90)), 4),
                },
            }
            summary[f"forward_{days}d"] = stats

            print(f"\n  触发后{days}日 (n={stats['count']}):")
            print(f"    平均收益: {stats['mean']:.2%}")
            print(f"    中位数:   {stats['median']:.2%}")
            print(f"    胜率:     {stats['win_rate']:.1%}")
            print(f"    亏>10%:   {stats['big_loss_rate']:.1%}")
            print(f"    平均最大回撤: {stats['avg_maxdd']:.2%}")
            print(f"    10%-90%分位: [{stats['percentiles']['10%']:.2%}, {stats['percentiles']['90%']:.2%}]")

    # 6. 保存
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    results_df.to_parquet(OUTPUT_EVENTS, index=False)
    with open(OUTPUT_SUMMARY, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\n✓ 事件 → {OUTPUT_EVENTS}")
    print(f"✓ 统计 → {OUTPUT_SUMMARY}")


if __name__ == "__main__":
    main()
