import tempfile
import unittest
from pathlib import Path

from config import Settings
from models import Book, Candle
from paper_trader import PaperTrader
from storage import Storage
from trend_strategy import TrendMomentumStrategy


def candles_from_closes(closes: list[float]) -> list[Candle]:
    return [
        Candle(i * 900_000, value, value * 1.002, value * 0.998, value, 1.0)
        for i, value in enumerate(closes)
    ]


class TrendStrategyTests(unittest.TestCase):
    def test_orderly_rising_breakout_buys(self):
        closes = [100.0 * (1.001 ** i) for i in range(60)]
        decision = TrendMomentumStrategy(Settings()).evaluate(candles_from_closes(closes))
        self.assertEqual(decision.action, 'BUY')
        self.assertEqual(decision.reason, 'trend_breakout')

    def test_flat_market_does_not_buy(self):
        closes = [100.0 for _ in range(60)]
        decision = TrendMomentumStrategy(Settings()).evaluate(candles_from_closes(closes))
        self.assertEqual(decision.action, 'SKIP')
        self.assertEqual(decision.reason, 'trend_niet_opwaarts')

    def test_extreme_momentum_is_not_chased(self):
        closes = [100.0 * (1.001 ** i) for i in range(59)]
        closes.append(closes[-1] * 1.08)
        decision = TrendMomentumStrategy(Settings()).evaluate(candles_from_closes(closes))
        self.assertEqual(decision.action, 'SKIP')
        self.assertEqual(decision.reason, 'momentum_te_hoog')

    def test_trend_trader_records_trend_entry_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Settings()
            db = Storage(str(Path(tmp) / 'trend.db'), s.paper_start_eur)
            try:
                trader = PaperTrader(s, db, entry_reason='trend_breakout')
                event = trader.open_long('AAA-EUR', Book(99.9, 100.0), 1, now_ms=1)
                self.assertIsNotNone(event)
                self.assertEqual(event.reason, 'trend_breakout')
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
