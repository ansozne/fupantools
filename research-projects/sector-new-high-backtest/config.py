"""Local-only configuration; no network or outbound destination is used."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
# The caller must supply licensed stock CSVs. No private machine default.
STOCK_DIR = Path(os.environ.get("STOCK_DATA_DIR", str(BASE_DIR / "input" / "stocks"))).expanduser()
# Input constituent JSON belongs in this directory; generated results stay local.
DATA_DIR = Path(os.environ.get("BACKTEST_DATA_DIR", str(BASE_DIR / "data"))).expanduser()
