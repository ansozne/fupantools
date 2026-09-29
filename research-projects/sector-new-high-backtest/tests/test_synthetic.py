"""Offline synthetic logic checks only; no market data or file outputs."""
import importlib.util
from pathlib import Path
import sys
import unittest

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('.', '_'), ROOT / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SyntheticChecks(unittest.TestCase):
    def test_near_high_denominator_missing_history(self):
        calc = load('01_calc_new_high_ratio.py')
        dates = pd.bdate_range('2020-01-01', periods=251)
        wide = pd.DataFrame({f'{i:06d}': [10.0] * 251 for i in range(10)}, index=dates)
        wide['000009'] = [float('nan')] * 251
        mask = calc.calc_near_high_mask(wide)
        ratios = calc.calc_sector_ratios(mask, {'synthetic': list(wide.columns)})
        self.assertTrue(ratios.empty)  # only 9 valid members: minimum is 10
        wide['000009'] = 10.0
        mask = calc.calc_near_high_mask(wide)
        ratios = calc.calc_sector_ratios(mask, {'synthetic': list(wide.columns)})
        self.assertEqual(len(ratios), 2)
        self.assertEqual(ratios.iloc[-1]['ratio'], 1.0)
        self.assertEqual(ratios.iloc[-1]['total_count'], 10)

    def test_calendar_day_dedup(self):
        stats = load('02_signal_stats.py')
        dates = pd.to_datetime(['2024-01-01', '2024-02-01', '2024-03-03'])
        frame = pd.DataFrame({'date': dates, 'sector': ['sample'] * 3,
                              'ratio': [0.21] * 3, 'new_high_count': [3] * 3,
                              'total_count': [10] * 3})
        events = stats.identify_signal_events(frame)
        self.assertEqual(len(events), 2)


if __name__ == '__main__':
    unittest.main()
