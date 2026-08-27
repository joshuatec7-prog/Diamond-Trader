import tempfile
import unittest
from pathlib import Path

from config import Settings
from continuation_v5_main import build_continuation_settings, continuation_db_path
from models import Book, Candle
from staged_runner_trader import StagedRunnerPaperTrader
from storage import Storage
from trend_v6_main import build_trend_settings, trend_db_path


class StagedRunnerTests(unittest.TestCase):
    def _open(self, tmp: str):
        s = Settings(
            db_path=str(Path(tmp) / 'paper.db'),
            take_profit_pct=3.5,
            max_open_positions=3,
        )
        db = Storage(s.db_path, s.paper_start_eur)
        trader = StagedRunnerPaperTrader(
            s,
            db,
            entry_reason='test_entry',
            lock_trigger_pct=1.50,
            lock_profit_eur=0.50,
            wide_trigger_pct=3.00,
            wide_trail_pct=1.25,
            tight_trigger_pct=6.00,
            tight_trail_pct=0.75,
        )
        event = trader.open_long('AAA-EUR', Book(99.9, 100.0), 1, now_ms=1)
        self.assertIsNotNone(event)
        p = db.get_position('AAA-EUR')
        self.assertIsNotNone(p)
        return s, db, trader, p

    def test_stage_one_keeps_only_profit_lock_until_three_pct(self):
        with tempfile.TemporaryDirectory() as tmp:
            _s, db, trader, p = self._open(tmp)
            try:
                assert p is not None
                bid = p.entry_price * 1.015
                self.assertIsNone(
                    trader.process_book('AAA-EUR', Book(bid, bid * 1.0005), now_ms=2)
                )
                first = db.get_position('AAA-EUR')
                self.assertIsNotNone(first)
                assert first is not None
                lock_stop = first.stop_price
                self.assertAlmostEqual(lock_stop, trader._lock_reference_price(first), places=10)

                # Ook bij +2,5% blijft alleen de €0,50-winstvloer actief.
                bid = p.entry_price * 1.025
                self.assertIsNone(
                    trader.process_book('AAA-EUR', Book(bid, bid * 1.0005), now_ms=3)
                )
                second = db.get_position('AAA-EUR')
                self.assertIsNotNone(second)
                assert second is not None
                self.assertAlmostEqual(second.stop_price, lock_stop, places=10)
            finally:
                db.close()

    def test_wide_trail_starts_at_three_pct(self):
        with tempfile.TemporaryDirectory() as tmp:
            _s, db, trader, p = self._open(tmp)
            try:
                assert p is not None
                bid = p.entry_price * 1.032
                trader.process_book('AAA-EUR', Book(bid, bid * 1.0005), now_ms=2)
                current = db.get_position('AAA-EUR')
                self.assertIsNotNone(current)
                assert current is not None
                self.assertAlmostEqual(current.stop_price, bid * 0.9875, places=10)
            finally:
                db.close()

    def test_tight_trail_starts_at_six_pct_and_no_hard_take(self):
        with tempfile.TemporaryDirectory() as tmp:
            _s, db, trader, p = self._open(tmp)
            try:
                assert p is not None
                bid = p.entry_price * 1.070
                event = trader.process_book(
                    'AAA-EUR', Book(bid, bid * 1.0005), now_ms=2
                )
                self.assertIsNone(event)
                current = db.get_position('AAA-EUR')
                self.assertIsNotNone(current)
                assert current is not None
                self.assertAlmostEqual(current.stop_price, bid * 0.9925, places=10)

                # Ook ruim boven de oude 3,5%-referentie blijft de runner open.
                higher = p.entry_price * 1.100
                event = trader.process_book(
                    'AAA-EUR', Book(higher, higher * 1.0005), now_ms=3
                )
                self.assertIsNone(event)
                self.assertIsNotNone(db.get_position('AAA-EUR'))
            finally:
                db.close()

    def test_lock_exit_is_separate_reason_and_about_half_euro_net(self):
        with tempfile.TemporaryDirectory() as tmp:
            _s, db, trader, p = self._open(tmp)
            try:
                assert p is not None
                bid = p.entry_price * 1.020
                trader.process_book('AAA-EUR', Book(bid, bid * 1.0005), now_ms=2)
                protected = db.get_position('AAA-EUR')
                self.assertIsNotNone(protected)
                assert protected is not None
                event = trader.process_book(
                    'AAA-EUR',
                    Book(protected.stop_price, protected.stop_price * 1.0005),
                    now_ms=3,
                )
                self.assertIsNotNone(event)
                assert event is not None
                self.assertEqual(event.reason, 'runner_profit_lock')
                self.assertGreaterEqual(event.pnl_eur or 0.0, 0.499)
            finally:
                db.close()

    def test_protected_runner_ignores_max_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            s, db, trader, p = self._open(tmp)
            try:
                assert p is not None
                bid = p.entry_price * 1.020
                trader.process_book('AAA-EUR', Book(bid, bid * 1.0005), now_ms=2)
                protected = db.get_position('AAA-EUR')
                self.assertIsNotNone(protected)
                assert protected is not None
                protected.bars_held = s.max_hold_bars - 1
                db.update_position(protected)

                close = p.entry_price * 1.020
                candle = Candle(
                    2,
                    close,
                    close * 1.002,
                    max(protected.stop_price * 1.001, close * 0.998),
                    close,
                    1.0,
                )
                event = trader.process_candle('AAA-EUR', candle, now_ms=4)
                self.assertIsNone(event)
                self.assertIsNotNone(db.get_position('AAA-EUR'))
            finally:
                db.close()

    def test_new_versions_have_clean_database_paths(self):
        base = Settings(db_path='/tmp/cryptobot_cleanroom.db')
        b = build_trend_settings(base)
        c = build_continuation_settings(base)
        self.assertEqual(trend_db_path(base.db_path), '/tmp/cryptobot_cleanroom_trend_v6.db')
        self.assertEqual(
            continuation_db_path(base.db_path),
            '/tmp/cryptobot_cleanroom_continuation_v5.db',
        )
        self.assertEqual(b.max_open_positions, 3)
        self.assertEqual(c.max_open_positions, 3)


if __name__ == '__main__':
    unittest.main()
