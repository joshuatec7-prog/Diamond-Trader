import tempfile
import unittest
from pathlib import Path

from config import Settings
from continuation_main import (
    CONTINUATION_MAX_OPEN_POSITIONS,
    build_continuation_settings,
    continuation_db_path,
)
from continuation_strategy import TrendContinuationStrategy
from models import Candle


def candles_from_closes(closes: list[float]) -> list[Candle]:
    return [
        Candle(i * 900_000, value, value * 1.002, value * 0.998, value, 1.0)
        for i, value in enumerate(closes)
    ]


class ContinuationStrategyTests(unittest.TestCase):
    def test_rising_trend_pullback_recovery_buys(self):
        base = [100.0 * (1.0015 ** i) for i in range(60)]
        anchor = base[-1]
        closes = base + [
            anchor * 1.003,
            anchor * 1.006,
            anchor * 1.004,
            anchor * 1.000,
            anchor * 0.997,
            anchor * 0.999,
            anchor * 1.001,
            anchor * 1.004,
        ]
        decision = TrendContinuationStrategy(Settings()).evaluate(
            candles_from_closes(closes)
        )
        self.assertEqual(decision.action, 'BUY')
        self.assertEqual(decision.reason, 'trend_pullback_continuation')

    def test_no_pullback_does_not_buy(self):
        closes = [100.0 * (1.0015 ** i) for i in range(70)]
        decision = TrendContinuationStrategy(Settings()).evaluate(
            candles_from_closes(closes)
        )
        self.assertEqual(decision.action, 'SKIP')
        self.assertEqual(decision.reason, 'geen_duidelijke_pullback')

    def test_deep_pullback_is_rejected(self):
        base = [100.0 * (1.0015 ** i) for i in range(60)]
        anchor = base[-1]
        closes = base + [
            anchor * 1.006,
            anchor * 1.004,
            anchor * 0.995,
            anchor * 0.965,
            anchor * 0.968,
            anchor * 0.973,
            anchor * 0.980,
            anchor * 0.990,
        ]
        decision = TrendContinuationStrategy(Settings()).evaluate(
            candles_from_closes(closes)
        )
        self.assertEqual(decision.action, 'SKIP')
        self.assertEqual(decision.reason, 'pullback_te_diep')

    def test_continuation_has_separate_db_and_three_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            primary = str(Path(tmp) / 'cleanroom.db')
            s = Settings(db_path=primary)
            continuation = build_continuation_settings(s)
            self.assertEqual(continuation.db_path, continuation_db_path(primary))
            self.assertTrue(
                continuation.db_path.endswith('cleanroom_continuation_v1.db')
            )
            self.assertEqual(
                continuation.max_open_positions,
                CONTINUATION_MAX_OPEN_POSITIONS,
            )
            self.assertEqual(continuation.max_open_positions, 3)


if __name__ == '__main__':
    unittest.main()
