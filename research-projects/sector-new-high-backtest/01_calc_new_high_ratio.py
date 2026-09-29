"""
01_calc_new_high_ratio.py
==========================
目的：计算每个板块每个交易日的"250日新高比例"。
      即：板块内有多少百分比的成分股，当日收盘价距离过去250个交易日最高价在5%以内。

输入：
  - data/sector_constituents.json（来自 00_fetch_sector_data.py）
  - 个股前复权日线数据（由 STOCK_DATA_DIR 指定）

输出：
  data/sector_new_high_ratio.parquet
  列: date | sector | ratio | new_high_count | total_count

性能考虑：
  - 11000+只股票的250日滚动最大值计算量大
  - 使用向量化操作，避免逐行循环
  - 先加载所有股票收盘价到宽表，再批量计算
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# 路径配置
from config import DATA_DIR, STOCK_DIR
SECTOR_FILE = DATA_DIR / "sector_constituents.json"
OUTPUT_FILE = DATA_DIR / "sector_new_high_ratio.parquet"

# 参数
LOOKBACK = 250          # 250个交易日
NEAR_HIGH_PCT = 0.05    # 距离新高5%以内算"接近新高"
START_DATE = "2020-01-01"


def load_sector_constituents() -> dict:
    """加载板块成分股"""
    with open(SECTOR_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_all_close_prices(codes: set) -> pd.DataFrame:
    """
    加载所有需要的股票收盘价，返回宽表 (date × code)
    只加载前复权数据
    """
    print(f"正在加载 {len(codes)} 只股票的收盘价...")
    frames = {}
    loaded = 0
    missing = 0

    for code in codes:
        file = STOCK_DIR / f"{code}_qfq.csv"
        if not file.exists():
            missing += 1
            continue
        try:
            df = pd.read_csv(file, usecols=["日期", "收盘"], parse_dates=["日期"])
            df = df.rename(columns={"日期": "date", "收盘": code})
            df = df.set_index("date")
            frames[code] = df[code]
            loaded += 1
        except Exception:
            missing += 1

        if (loaded + missing) % 500 == 0:
            print(f"  已处理 {loaded + missing}/{len(codes)} (成功 {loaded}, 缺失 {missing})")

    print(f"  加载完成: 成功 {loaded}, 缺失 {missing}")

    # 合并为宽表
    if not frames:
        raise RuntimeError("没有加载到任何股票数据")

    wide = pd.DataFrame(frames)
    wide = wide.sort_index()
    wide = wide[wide.index >= START_DATE]
    print(f"  日期范围: {wide.index.min()} ~ {wide.index.max()}, 共 {len(wide)} 个交易日")
    return wide


def calc_near_high_mask(close_wide: pd.DataFrame) -> pd.DataFrame:
    """
    计算每只股票每天是否"接近250日新高"
    返回布尔宽表，True表示当日收盘价距250日最高价在5%以内
    """
    print("正在计算250日滚动最高价...")
    rolling_max = close_wide.rolling(window=LOOKBACK, min_periods=LOOKBACK).max()

    print("正在计算接近新高比例...")
    # 距离新高的比例 = (rolling_max - close) / rolling_max
    distance = (rolling_max - close_wide) / rolling_max
    near_high = distance <= NEAR_HIGH_PCT

    # 原版把无历史/缺价的样本当作 False，造成分母包含无效样本；保留缺失以防误导。
    return near_high.where(rolling_max.notna() & close_wide.notna())


def calc_sector_ratios(near_high: pd.DataFrame, sectors: dict) -> pd.DataFrame:
    """
    对每个板块、每个交易日，计算"接近250日新高"的成分股比例
    """
    print(f"正在计算 {len(sectors)} 个板块的新高比例...")
    records = []
    available_codes = set(near_high.columns)

    for i, (sector_name, constituents) in enumerate(sectors.items(), 1):
        # 只保留有数据的成分股
        valid_codes = [c for c in constituents if c in available_codes]
        if len(valid_codes) < 10:
            continue

        # 提取该板块的子矩阵
        sector_mask = near_high[valid_codes]

        # 每天的新高数量和比例
        daily_count = sector_mask.eq(True).sum(axis=1)
        daily_total = sector_mask.notna().sum(axis=1)  # 当天有足够历史且有价格的股票数
        daily_ratio = daily_count / daily_total.replace(0, np.nan)

        # 只保留有效行（总数>=10）
        valid_days = daily_total >= 10
        dates = daily_ratio.index[valid_days]
        ratios = daily_ratio[valid_days].values
        counts = daily_count[valid_days].values
        totals = daily_total[valid_days].values

        for d, r, c, t in zip(dates, ratios, counts, totals):
            if r > 0:  # 只记录有新高股票的日期（节省空间）
                records.append({
                    "date": d,
                    "sector": sector_name,
                    "ratio": round(r, 4),
                    "new_high_count": int(c),
                    "total_count": int(t),
                })

        if i % 50 == 0:
            print(f"  已处理 {i}/{len(sectors)} 个板块")

    df = pd.DataFrame(records)
    print(f"  共 {len(df)} 条记录")
    return df


def main():
    # 1. 加载板块成分股
    sectors = load_sector_constituents()
    print(f"共 {len(sectors)} 个板块")

    # 2. 收集所有需要的股票代码
    all_codes = set()
    for codes in sectors.values():
        all_codes.update(codes)
    print(f"共需要 {len(all_codes)} 只股票数据")

    # 3. 加载收盘价
    close_wide = load_all_close_prices(all_codes)

    # 4. 计算接近新高的布尔矩阵
    near_high = calc_near_high_mask(close_wide)

    # 5. 计算每个板块的新高比例
    ratio_df = calc_sector_ratios(near_high, sectors)

    # 6. 保存
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ratio_df.to_parquet(OUTPUT_FILE, index=False)
    print(f"\n✓ 已保存 → {OUTPUT_FILE}")
    print(f"  板块数: {ratio_df['sector'].nunique()}")
    print(f"  日期范围: {ratio_df['date'].min()} ~ {ratio_df['date'].max()}")

    # 7. 快速统计：有多少次板块新高比例>=20%
    high_events = ratio_df[ratio_df["ratio"] >= 0.20]
    print(f"\n📊 ratio>=20% 的记录数: {len(high_events)}")
    print(f"   涉及板块数: {high_events['sector'].nunique()}")


if __name__ == "__main__":
    main()
