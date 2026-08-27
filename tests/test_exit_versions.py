import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from config import Settings
from continuation_v2_main import (
    build_continuation_settings,
    continuation_db_path,
)
from models import Book
from paper_trader import PaperTrader
from storage import Storage
from trend_v3_main import build_trend_settings, trend_db_path


class ExitVersionTests(unittest.TestCase):
    def test_trend_v3_is_clean_db_with_same_stop_and_3_5_take(self):
        primary = replace(Settings(), db_path='/tmp/cryptobot_cleanroom.db')
        s = build_trend_settings(primary)
        self.assertEqual(s.db_path, '/tmp/cryptobot_cleanroom_trend_v3.db')
        self.assertEqual(trend_db_path(primary.db_path), s.db_path)
        self.assertEqual(s.max_open_positions, 3)
        self.assertAlmostEqual(s.stop_loss_pct, 1.5)
        self.assertAlmostEqual(s.take_profit_pct, 3.5)

    def test_continuation_v2_is_clean_db_with_same_stop_and_3_5_take(self):
        primary = replace(Settings(), db_path='/tmp/cryptobot_cleanroom.db')
        s = build_continuation_settings(primary)
        self.assertEqual(s.db_path, '/tmp/cryptobot_cleanroom_continuation_v2.db')
        self.assertEqual(continuation_db_path(primary.db_path), s.db_path)
        self.assertEqual(s.max_open_positions, 3)
        self.assertAlmostEqual(s.stop_loss_pct, 1.5)
        self.assertAlmostEqual(s.take_profit_pct, 3.5)

    def test_paper_position_uses_version_exit_levels(self):
        with tempfile.TemporaryDirectory() as tmp:
            primary = replace(Settings(), db_path=str(Path(tmp) / 'cleanroom.db'))
            for builder, reason in (
                (build_trend_settings, 'trend_breakout'),
                (build_continuation_settings, 'trend_pullback_continuation'),
            ):
                s = builder(primary)
                db = Storage(s.db_path, s.paper_start_eur)
                try:
                    trader = PaperTrader(s, db, entry_reason=reason)
                    event = trader.open_long('AAA-EUR', Book(99.9, 100.0), 1, now_ms=1)
                    self.assertIsNotNone(event)
                    position = db.get_position('AAA-EUR')
                    self.assertIsNotNone(position)
                    assert position is not None
                    self.assertAlmostEqual(
                        position.stop_price,
                        position.entry_price * 0.985,
                        places=10,
                    )
                    self.assertAlmostEqual(
                        position.take_price,
                        position.entry_price * 1.035,
                        places=10,
                    )
                finally:
                    db.close()


if __name__ == '__main__':
    unittest.main()
