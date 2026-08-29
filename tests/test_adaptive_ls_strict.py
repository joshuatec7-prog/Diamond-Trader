import time
import unittest

from adaptive_ls_strict_main import strict_db_path
from adaptive_ls_strict_strategy import StrictAdaptiveLongShortStrategy
from config import Settings
from models import Candle

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


class StrictAdaptiveLongShortTests(unittest.TestCase):
    def test_requires_sixty_percent_bear_breadth(self):
        strategy = StrictAdaptiveLongShortStrategy(Settings())
        bearish = strategy.analyze(candles_from_growth(0.9990))
        flat = strategy.analyze(candles_from_growth(1.0))
        metrics = {f'B{i}-EUR': bearish for i in range(6)}
        metrics.update({f'F{i}-EUR': flat for i in range(6)})
        regime, bull, bear = strategy.market_regime(metrics)
        self.assertEqual(regime, 'SIDEWAYS')
        self.assertEqual(bear, 50.0)
        self.assertEqual(bull, 0.0)

    def test_strong_falling_market_can_create_short(self):
        strategy = StrictAdaptiveLongShortStrategy(Settings())
        metrics = strategy.analyze(candles_from_growth(0.9990))
        regime, bull, bear = strategy.market_regime({f'M{i:02d}-EUR': metrics for i in range(12)})
        self.assertEqual(regime, 'BEAR')
        self.assertGreaterEqual(bear, 60.0)
        decision = strategy.evaluate_metrics(metrics, regime, bull, bear)
        self.assertEqual(decision.action, 'SHORT')
        self.assertEqual(decision.reason, 'adaptive_v2s_short_breakdown_confirmed')

    def test_rising_market_keeps_d2_long_side(self):
        strategy = StrictAdaptiveLongShortStrategy(Settings())
        metrics = strategy.analyze(candles_from_growth(1.0010))
        regime, bull, bear = strategy.market_regime({f'M{i:02d}-EUR': metrics for i in range(12)})
        self.assertEqual(regime, 'BULL')
        decision = strategy.evaluate_metrics(metrics, regime, bull, bear)
        self.assertEqual(decision.action, 'LONG')

    def test_strict_database_is_separate(self):
        path = strict_db_path('/var/data/cryptobot_cleanroom.db')
        self.assertEqual(path, '/var/data/cryptobot_cleanroom_adaptive_trend_v2_strict.db')


if __name__ == '__main__':
    unittest.main()
