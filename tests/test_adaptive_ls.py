import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from adaptive_ls_strategy import AdaptiveLongShortStrategy
from adaptive_ls_trader import AdaptiveLongShortPaperTrader
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


class AdaptiveLongShortTests(unittest.TestCase):
    def test_rising_market_is_bull_and_creates_long(self):
        strategy = AdaptiveLongShortStrategy(Settings())
        metrics = strategy.analyze(candles_from_growth(1.0010))
        regime, bull, bear = strategy.market_regime({f'M{i:02d}-EUR': metrics for i in range(12)})
        self.assertEqual(regime, 'BULL')
        self.assertGreaterEqual(bull, 45.0)
        self.assertLess(bear, 45.0)
        decision = strategy.evaluate_metrics(metrics, regime, bull, bear)
        self.assertEqual(decision.action, 'LONG')

    def test_falling_market_is_bear_and_creates_short(self):
        strategy = AdaptiveLongShortStrategy(Settings())
        metrics = strategy.analyze(candles_from_growth(0.9990))
        regime, bull, bear = strategy.market_regime({f'M{i:02d}-EUR': metrics for i in range(12)})
        self.assertEqual(regime, 'BEAR')
        self.assertGreaterEqual(bear, 45.0)
        self.assertLess(bull, 45.0)
        decision = strategy.evaluate_metrics(metrics, regime, bull, bear)
        self.assertEqual(decision.action, 'SHORT')

    def test_flat_market_is_sideways_and_does_not_trade(self):
        strategy = AdaptiveLongShortStrategy(Settings())
        metrics = strategy.analyze(candles_from_growth(1.0))
        regime, bull, bear = strategy.market_regime({f'M{i:02d}-EUR': metrics for i in range(12)})
        self.assertEqual(regime, 'SIDEWAYS')
        decision = strategy.evaluate_metrics(metrics, regime, bull, bear)
        self.assertEqual(decision.action, 'SKIP')
        self.assertEqual(decision.reason, 'marktregime_sideways')

    def test_short_profit_and_side_are_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = replace(Settings(), db_path=str(Path(tmp) / 'd2.db'), slippage_pct=0.0, max_open_positions=3)
            db = Storage(s.db_path, s.paper_start_eur)
            try:
                trader = AdaptiveLongShortPaperTrader(s, db)
                opened = trader.open_directional('SHORT', 'AAA-EUR', Book(100.0, 100.0), 1, 0.8, now_ms=1)
                self.assertIsNotNone(opened)
                self.assertEqual(trader.position_side('AAA-EUR'), 'SHORT')
                p = db.get_position('AAA-EUR')
                self.assertIsNotNone(p)
                self.assertGreater(p.stop_price, p.entry_price)
                closed = trader.close_trend_break('AAA-EUR', 90.0, 'test_exit', now_ms=2)
                self.assertIsNotNone(closed)
                self.assertGreater(closed.pnl_eur, 0.0)
                self.assertIsNone(db.get_position('AAA-EUR'))
                self.assertEqual(trader.position_side('AAA-EUR'), '')
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
