import tempfile
import unittest
from pathlib import Path

from config import Settings
from continuation_v3_main import build_continuation_settings, continuation_db_path
from models import Book
from profit_protect_trader import ProfitProtectPaperTrader
from storage import Storage
from trend_v4_main import build_trend_settings, trend_db_path


class ProfitProtectTests(unittest.TestCase):
    def _open(self, tmp: str):
        s = Settings(
            db_path=str(Path(tmp) / 'paper.db'),
            take_profit_pct=3.5,
            max_open_positions=3,
        )
        db = Storage(s.db_path, s.paper_start_eur)
        trader = ProfitProtectPaperTrader(
            s,
            db,
            entry_reason='test_entry',
            trigger_pct=1.50,
            lock_profit_eur=0.50,
            trail_distance_pct=0.75,
        )
        event = trader.open_long('AAA-EUR', Book(99.9, 100.0), 1, now_ms=1)
        self.assertIsNotNone(event)
        return s, db, trader, db.get_position('AAA-EUR')

    def test_below_trigger_keeps_original_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            _s, db, trader, p = self._open(tmp)
            try:
                self.assertIsNotNone(p)
                old_stop = p.stop_price
                bid = p.entry_price * 1.014
                self.assertIsNone(trader.process_book('AAA-EUR', Book(bid, bid * 1.0005), now_ms=2))
                current = db.get_position('AAA-EUR')
                self.assertIsNotNone(current)
                self.assertAlmostEqual(current.stop_price, old_stop, places=10)
            finally:
                db.close()

    def test_trigger_locks_about_half_euro_net_profit(self):
        with tempfile.TemporaryDirectory() as tmp:
            _s, db, trader, p = self._open(tmp)
            try:
                self.assertIsNotNone(p)
                bid = p.entry_price * 1.015
                self.assertIsNone(trader.process_book('AAA-EUR', Book(bid, bid * 1.0005), now_ms=2))

                protected = db.get_position('AAA-EUR')
                self.assertIsNotNone(protected)
                self.assertGreater(protected.stop_price, protected.entry_price)

                stop_bid = protected.stop_price
                event = trader.process_book(
                    'AAA-EUR',
                    Book(stop_bid, stop_bid * 1.0005),
                    now_ms=3,
                )
                self.assertIsNotNone(event)
                self.assertEqual(event.reason, 'profit_protect')
                self.assertGreaterEqual(event.pnl_eur, 0.499)
            finally:
                db.close()

    def test_trailing_stop_only_moves_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            _s, db, trader, p = self._open(tmp)
            try:
                self.assertIsNotNone(p)
                first_bid = p.entry_price * 1.015
                trader.process_book('AAA-EUR', Book(first_bid, first_bid * 1.0005), now_ms=2)
                first = db.get_position('AAA-EUR')
                self.assertIsNotNone(first)

                second_bid = p.entry_price * 1.025
                trader.process_book('AAA-EUR', Book(second_bid, second_bid * 1.0005), now_ms=3)
                second = db.get_position('AAA-EUR')
                self.assertIsNotNone(second)
                self.assertGreater(second.stop_price, first.stop_price)

                lower_but_safe = p.entry_price * 1.020
                trader.process_book(
                    'AAA-EUR',
                    Book(lower_but_safe, lower_but_safe * 1.0005),
                    now_ms=4,
                )
                third = db.get_position('AAA-EUR')
                self.assertIsNotNone(third)
                self.assertAlmostEqual(third.stop_price, second.stop_price, places=10)
            finally:
                db.close()

    def test_new_versions_have_clean_db_paths_and_35pct_take_profit(self):
        base = Settings(db_path='/tmp/cryptobot_cleanroom.db')
        b = build_trend_settings(base)
        c = build_continuation_settings(base)
        self.assertEqual(trend_db_path(base.db_path), '/tmp/cryptobot_cleanroom_trend_v4.db')
        self.assertEqual(
            continuation_db_path(base.db_path),
            '/tmp/cryptobot_cleanroom_continuation_v3.db',
        )
        self.assertEqual(b.take_profit_pct, 3.5)
        self.assertEqual(c.take_profit_pct, 3.5)
        self.assertEqual(b.max_open_positions, 3)
        self.assertEqual(c.max_open_positions, 3)


if __name__ == '__main__':
    unittest.main()
