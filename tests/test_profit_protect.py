import tempfile
import unittest
from pathlib import Path

from config import Settings
from continuation_v4_main import build_continuation_settings, continuation_db_path
from models import Book, Candle
from profit_protect_trader import ProfitProtectPaperTrader, RunnerProfitProtectPaperTrader
from storage import Storage
from trend_v5_main import build_trend_settings, trend_db_path


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

    def _open_runner(self, tmp: str, max_hold_bars: int = 96):
        s = Settings(
            db_path=str(Path(tmp) / 'runner.db'),
            take_profit_pct=3.5,
            max_open_positions=3,
            max_hold_bars=max_hold_bars,
        )
        db = Storage(s.db_path, s.paper_start_eur)
        trader = RunnerProfitProtectPaperTrader(
            s,
            db,
            entry_reason='runner_entry',
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

    def test_runner_does_not_close_at_old_35pct_take_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            _s, db, trader, p = self._open_runner(tmp)
            try:
                self.assertIsNotNone(p)
                old_take = p.take_price

                bid_4pct = p.entry_price * 1.040
                self.assertGreater(bid_4pct, old_take)
                event = trader.process_book(
                    'AAA-EUR', Book(bid_4pct, bid_4pct * 1.0005), now_ms=2
                )
                self.assertIsNone(event)
                after_4 = db.get_position('AAA-EUR')
                self.assertIsNotNone(after_4)
                stop_4 = after_4.stop_price

                bid_6pct = p.entry_price * 1.060
                event = trader.process_book(
                    'AAA-EUR', Book(bid_6pct, bid_6pct * 1.0005), now_ms=3
                )
                self.assertIsNone(event)
                after_6 = db.get_position('AAA-EUR')
                self.assertIsNotNone(after_6)
                self.assertGreater(after_6.stop_price, stop_4)
                self.assertGreater(after_6.stop_price, old_take)
            finally:
                db.close()

    def test_runner_closes_on_trailing_stop_after_large_gain(self):
        with tempfile.TemporaryDirectory() as tmp:
            _s, db, trader, p = self._open_runner(tmp)
            try:
                self.assertIsNotNone(p)
                high_bid = p.entry_price * 1.060
                trader.process_book('AAA-EUR', Book(high_bid, high_bid * 1.0005), now_ms=2)
                protected = db.get_position('AAA-EUR')
                self.assertIsNotNone(protected)

                event = trader.process_book(
                    'AAA-EUR',
                    Book(protected.stop_price, protected.stop_price * 1.0005),
                    now_ms=3,
                )
                self.assertIsNotNone(event)
                self.assertEqual(event.reason, 'runner_trailing_stop')
                self.assertGreater(event.pnl_eur, 0.50)
            finally:
                db.close()

    def test_protected_runner_can_continue_past_normal_max_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            _s, db, trader, p = self._open_runner(tmp, max_hold_bars=1)
            try:
                self.assertIsNotNone(p)
                trigger_bid = p.entry_price * 1.020
                trader.process_book(
                    'AAA-EUR', Book(trigger_bid, trigger_bid * 1.0005), now_ms=2
                )
                protected = db.get_position('AAA-EUR')
                self.assertIsNotNone(protected)

                close = p.entry_price * 1.018
                candle = Candle(
                    timestamp_ms=2,
                    open=close,
                    high=close * 1.001,
                    low=max(protected.stop_price * 1.001, close * 0.999),
                    close=close,
                    volume=1.0,
                )
                event = trader.process_candle('AAA-EUR', candle, now_ms=3)
                self.assertIsNone(event)
                self.assertIsNotNone(db.get_position('AAA-EUR'))
            finally:
                db.close()

    def test_new_runner_versions_have_clean_db_paths(self):
        base = Settings(db_path='/tmp/cryptobot_cleanroom.db')
        b = build_trend_settings(base)
        c = build_continuation_settings(base)
        self.assertEqual(trend_db_path(base.db_path), '/tmp/cryptobot_cleanroom_trend_v5.db')
        self.assertEqual(
            continuation_db_path(base.db_path),
            '/tmp/cryptobot_cleanroom_continuation_v4.db',
        )
        self.assertEqual(b.max_open_positions, 3)
        self.assertEqual(c.max_open_positions, 3)


if __name__ == '__main__':
    unittest.main()
