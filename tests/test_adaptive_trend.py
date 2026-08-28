import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from adaptive_trend_strategy import AdaptiveTrendStrategy
from adaptive_trend_trader import AdaptiveTrendPaperTrader
from config import Settings
from models import Book, Candle
from storage import Storage


INTERVAL_MS = 900_000


def candles_from_growth(factor: float, count: int = 124) -> list[Candle]:
    last_ts = (int(time.time() * 1000) // INTERVAL_MS) * INTERVAL_MS - INTERVAL_MS
    start = last_ts - (count - 1) * INTERVAL_MS
    rows = []
    value = 100.0
    for i in range(count):
        value *= factor
        rows.append(Candle(start + i * INTERVAL_MS, value, value * 1.002, value * 0.998, value, 10.0))
    return rows


class AdaptiveTrendTests(unittest.TestCase):
    def test_broad_rising_market_allows_adaptive_breakout(self):
        strategy = AdaptiveTrendStrategy(Settings())
        markets = {
            f'M{i:02d}-EUR': strategy.analyze(candles_from_growth(1.0010 + i * 0.00005))
            for i in range(12)
        }
        regime, breadth = strategy.market_regime(markets)
        self.assertEqual(regime, 'BULL')
        self.assertGreaterEqual(breadth, 45.0)
        decision = strategy.evaluate_metrics(markets['M00-EUR'], regime, breadth)
        self.assertEqual(decision.action, 'BUY')
        self.assertIn(decision.reason, {'adaptive_breakout_resume', 'adaptive_pullback_resume'})

    def test_flat_market_is_not_bullish(self):
        strategy = AdaptiveTrendStrategy(Settings())
        metrics = strategy.analyze(candles_from_growth(1.0))
        regime, breadth = strategy.market_regime({f'M{i:02d}-EUR': metrics for i in range(12)})
        self.assertEqual(regime, 'BEAR')
        decision = strategy.evaluate_metrics(metrics, regime, breadth)
        self.assertEqual(decision.action, 'SKIP')
        self.assertEqual(decision.reason, 'marktregime_niet_bullish')

    def test_adaptive_stop_distance_grows_with_volatility(self):
        low = AdaptiveTrendPaperTrader.initial_stop_pct(0.30)
        high = AdaptiveTrendPaperTrader.initial_stop_pct(1.50)
        self.assertGreater(high, low)
        self.assertGreaterEqual(low, AdaptiveTrendPaperTrader.INITIAL_STOP_MIN_PCT)
        self.assertLessEqual(high, AdaptiveTrendPaperTrader.INITIAL_STOP_MAX_PCT)

    def test_runner_has_no_hard_take_profit_and_raises_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = replace(
                Settings(),
                db_path=str(Path(tmp) / 'adaptive.db'),
                slippage_pct=0.0,
                max_open_positions=3,
            )
            db = Storage(s.db_path, s.paper_start_eur)
            try:
                trader = AdaptiveTrendPaperTrader(s, db)
                event = trader.open_long_adaptive(
                    'AAA-EUR', Book(99.9, 100.0), candle_ts=1, atr_pct=0.8, now_ms=1
                )
                self.assertIsNotNone(event)
                before = db.get_position('AAA-EUR')
                self.assertIsNotNone(before)
                self.assertLess(before.stop_price, before.entry_price)

                close = trader.process_book(
                    'AAA-EUR', Book(109.9, 110.0), atr_pct=0.8, now_ms=2
                )
                self.assertIsNone(close)
                after = db.get_position('AAA-EUR')
                self.assertIsNotNone(after)
                self.assertGreater(after.stop_price, after.entry_price)
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
