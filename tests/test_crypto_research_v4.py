import sqlite3
import unittest
from datetime import datetime, timezone

from crypto_research_v4 import (
    MARKETS,
    _benchmark_summary,
    _ensure_schema,
    _realized_volatility_pct,
    _sma,
    _store_week,
    _target_weights,
    _weekly_cutoff_ms,
)


class CryptoResearchV4Tests(unittest.TestCase):
    def test_weekly_cutoff_is_previous_sunday_midnight_utc(self):
        now = datetime(2026, 8, 31, 12, 30, tzinfo=timezone.utc)
        expected = datetime(2026, 8, 30, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(_weekly_cutoff_ms(int(now.timestamp() * 1000)), int(expected.timestamp() * 1000))

    def test_signal_average_uses_last_65_days(self):
        values = [float(value) for value in range(1, 71)]
        self.assertAlmostEqual(_sma(values, 65), sum(values[-65:]) / 65)

    def test_volatility_needs_21_closes_and_is_finite(self):
        closes = [100.0 + index + (2.0 if index % 2 else 0.0) for index in range(21)]
        value = _realized_volatility_pct(closes)
        self.assertGreater(value, 0.0)

    def test_high_volatility_reduces_total_exposure(self):
        rows = [
            {'market': 'BTC-USDC', 'long_signal': True, 'volatility_20d_pct': 160.0},
            {'market': 'ETH-USDC', 'long_signal': True, 'volatility_20d_pct': 160.0},
        ]
        weights, cash = _target_weights(rows, 80.0)
        self.assertAlmostEqual(weights['BTC-USDC'], 0.25)
        self.assertAlmostEqual(weights['ETH-USDC'], 0.25)
        self.assertAlmostEqual(cash, 0.50)

    def test_no_long_signal_means_full_cash(self):
        rows = [
            {'market': 'BTC-USDC', 'long_signal': False, 'volatility_20d_pct': 40.0},
            {'market': 'ETH-USDC', 'long_signal': False, 'volatility_20d_pct': 50.0},
        ]
        weights, cash = _target_weights(rows, 80.0)
        self.assertEqual(weights, {'BTC-USDC': 0.0, 'ETH-USDC': 0.0})
        self.assertEqual(cash, 1.0)

    def test_benchmarks_include_cost_stress_and_dca(self):
        conn = sqlite3.connect(':memory:')
        _ensure_schema(conn)

        def rows(close: float):
            return [
                {
                    'market': market,
                    'decision_close_ms': 1,
                    'close': close,
                    'sma_65': close - 1.0,
                    'volatility_20d_pct': 40.0,
                    'long_signal': True,
                    'target_weight': 0.5,
                    'measured_spread_pct': 0.02,
                }
                for market in MARKETS
            ]

        _store_week(conn, 1, 1, rows(100.0))
        _store_week(conn, 2, 2, rows(110.0))
        summary = _benchmark_summary(conn)
        conn.close()

        self.assertEqual(summary['weeks'], 2.0)
        self.assertGreater(summary['buy_hold_50_50_index'], 109.0)
        self.assertLess(summary['buy_hold_50_50_index'], 110.0)
        self.assertGreater(summary['weekly_dca_index'], 104.0)
        self.assertLess(summary['weekly_dca_index'], 105.0)
        self.assertGreater(summary['v4_index'], 100.0)
        self.assertLess(summary['v4_cost_stress_2x_index'], summary['v4_index'])
        self.assertLess(summary['v4_cost_stress_3x_index'], summary['v4_cost_stress_2x_index'])


if __name__ == '__main__':
    unittest.main()

