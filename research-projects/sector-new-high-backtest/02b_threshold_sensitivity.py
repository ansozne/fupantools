"""
02b_threshold_sensitivity.py
==============================
目的：测试不同阈值（15%/20%/25%/30%/40%/50%）下信号质量的变化。
回答：是否存在一个"甜蜜点"阈值，使信号更稀少但质量更高？
"""

import json
from pathlib import Path
import numpy as np
import pandas as pd

from config import DATA_DIR, STOCK_DIR
SECTOR_FILE = DATA_DIR / "sector_constituents.json"
RATIO_FILE = DATA_DIR / "sector_new_high_ratio.parquet"

DEDUP_WINDOW = 60
THRESHOLDS = [0.15, 0.20, 0.25, 0.30, 0.40, 0.50]


def identify_events(ratio_df, threshold):
    triggered = ratio_df[ratio_df["ratio"] >= threshold].sort_values(["sector", "date"])
    events = []
    last_event = {}
    for _, row in triggered.iterrows():
        sector, date = row["sector"], row["date"]
        if sector in last_event and (date - last_event[sector]).days < DEDUP_WINDOW:
            continue
        last_event[sector] = date
        events.append({"sector": sector, "trigger_date": date, "trigger_ratio": row["ratio"]})
    return pd.DataFrame(events)


def load_sector_returns(sectors):
    sector_returns = {}
    for name, codes in sectors.items():
        rets = []
        for code in codes:
            f = STOCK_DIR / f"{code}_qfq.csv"
            if not f.exists():
                continue
            try:
                df = pd.read_csv(f, usecols=["日期", "收盘"], parse_dates=["日期"])
                df = df.set_index("日期").sort_index()
                df = df[df.index >= "2020-01-01"]
                if len(df) < 50:
                    continue
                rets.append(df["收盘"].pct_change())
            except:
                continue
        if len(rets) >= 10:
            sector_returns[name] = pd.concat(rets, axis=1).mean(axis=1)
    return sector_returns


def calc_stats(events_df, sector_returns, days=60):
    results = []
    for _, row in events_df.iterrows():
        sector = row["sector"]
        if sector not in sector_returns:
            continue
        ret_s = sector_returns[sector]
        future = ret_s[ret_s.index >= row["trigger_date"]]
        if len(future) <= days:
            continue
        cum = (1 + future).cumprod()
        ret = cum.iloc[days] / cum.iloc[0] - 1
        results.append(ret)
    return results


def main():
    ratio_df = pd.read_parquet(RATIO_FILE)
    ratio_df["date"] = pd.to_datetime(ratio_df["date"])

    with open(SECTOR_FILE, "r", encoding="utf-8") as f:
        sectors = json.load(f)

    print("预加载行业收益率...")
    sector_returns = load_sector_returns(sectors)
    print(f"  {len(sector_returns)} 个行业\n")

    print(f"{'阈值':<8}{'事件数':<8}{'60日均收益':<12}{'60日中位数':<12}{'胜率':<8}{'亏>10%':<8}")
    print("-" * 60)

    for thresh in THRESHOLDS:
        events = identify_events(ratio_df, thresh)
        if events.empty:
            print(f"{thresh:.0%}    0")
            continue

        rets = calc_stats(events, sector_returns, days=60)
        if not rets:
            print(f"{thresh:.0%}    {len(events):<8} 无有效数据")
            continue

        arr = np.array(rets)
        print(f"{thresh:.0%}    {len(events):<8}{arr.mean():<12.2%}{np.median(arr):<12.2%}"
              f"{(arr>0).mean():<8.1%}{(arr<-0.10).mean():<8.1%}")

    print("\n结论：阈值越高，信号越少，观察质量变化趋势")


if __name__ == "__main__":
    main()
