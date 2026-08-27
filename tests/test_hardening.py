import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from config import Settings
from main import process_market
from models import Book, Candle
from offline_check import run_offline_check
from paper_trader import PaperTrader
from readiness import readiness
from storage import Storage
from strategy import BandReentryStrategy


class StaticMarketData:
    def __init__(self, candles: list[Candle], book: Book) -> None:
        self._candles = candles
        self._book = book
        self.book_calls = 0

    def top_markets_by_quote_volume(self, quote: str, limit: int) -> list[str]:
        return ['AAA-EUR'][:limit]

    def closed_candles(self, market: str, interval: str, limit: int,
                       now_ms: int | None = None) -> list[Candle]:
        return list(self._candles)

    def book(self, market: str) -> Book:
        self.book_calls += 1
        return self._book


class HardeningTests(unittest.TestCase):
    def test_default_cadence_is_15m_with_same_time_horizon(self):
        s = Settings()
        self.assertEqual(s.interval, '15m')
        self.assertEqual(s.poll_seconds, 120)
        self.assertEqual(s.band_window, 80)
        self.assertEqual(s.max_hold_bars, 96)
        self.assertAlmostEqual(s.band_window * 15 / 60, 20.0)
        self.assertAlmostEqual(s.max_hold_bars * 15 / 60, 24.0)

    def test_non_paper_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            replace(Settings(), run_mode='LIVE').validate()

    def test_non_finite_config_is_rejected(self):
        with self.assertRaises(ValueError):
            replace(Settings(), slippage_pct=float('nan')).validate()

    def test_invalid_book_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = replace(Settings(), db_path=str(Path(tmp)/'x.db'))
            db = Storage(s.db_path, s.paper_start_eur)
            try:
                ok, reason = PaperTrader(s, db).can_open('AAA-EUR', Book(math.nan, 100.0))
                self.assertFalse(ok)
                self.assertEqual(reason, 'ongeldig_orderboek')
            finally:
                db.close()

    def test_invalid_candle_cannot_mutate_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = replace(Settings(), db_path=str(Path(tmp)/'x.db'), slippage_pct=0)
            db = Storage(s.db_path, s.paper_start_eur)
            trader = PaperTrader(s, db)
            try:
                trader.open_long('AAA-EUR', Book(99.9,100), 1, now_ms=100)
                with self.assertRaises(ValueError):
                    trader.process_candle('AAA-EUR', Candle(2,100,90,110,100,1), now_ms=200)
                self.assertIsNotNone(db.get_position('AAA-EUR'))
            finally:
                db.close()

    def test_intracycle_book_stop_closes_open_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = replace(Settings(), db_path=str(Path(tmp)/'x.db'), slippage_pct=0)
            db = Storage(s.db_path, s.paper_start_eur)
            trader = PaperTrader(s, db)
            try:
                trader.open_long('AAA-EUR', Book(99.9, 100.0), 1, now_ms=100)
                p = db.get_position('AAA-EUR')
                self.assertIsNotNone(p)
                event = trader.process_book(
                    'AAA-EUR', Book(p.stop_price - 1.0, p.stop_price - 0.5), now_ms=200
                )
                self.assertIsNotNone(event)
                self.assertEqual(event.reason, 'stop_loss')
                self.assertIsNone(db.get_position('AAA-EUR'))
                self.assertEqual(len(db.trade_rows()), 1)
                self.assertLess(float(db.trade_rows()[0]['pnl_eur']), 0)
            finally:
                db.close()

    def test_open_position_is_monitored_without_new_closed_candle(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = replace(
                Settings(), db_path=str(Path(tmp)/'x.db'), band_window=5,
                slippage_pct=0,
            )
            db = Storage(s.db_path, s.paper_start_eur)
            trader = PaperTrader(s, db)
            try:
                candles = [
                    Candle(i * 900_000, 100.0, 101.0, 99.0, 100.0, 1.0)
                    for i in range(6)
                ]
                latest_ts = candles[-1].timestamp_ms
                trader.open_long('AAA-EUR', Book(99.9, 100.0), latest_ts - 900_000, now_ms=100)
                p = db.get_position('AAA-EUR')
                self.assertIsNotNone(p)
                db.set_last_processed('AAA-EUR', latest_ts)
                source = StaticMarketData(
                    candles, Book(p.stop_price - 1.0, p.stop_price - 0.5)
                )

                ok = process_market(
                    'AAA-EUR', source, db, BandReentryStrategy(s), trader, s
                )
                self.assertTrue(ok)
                self.assertEqual(source.book_calls, 1)
                self.assertIsNone(db.get_position('AAA-EUR'))
                self.assertEqual(len(db.trade_rows()), 1)
            finally:
                db.close()

    def test_close_cannot_credit_cash_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = replace(Settings(), db_path=str(Path(tmp)/'x.db'), slippage_pct=0)
            db = Storage(s.db_path, s.paper_start_eur)
            trader = PaperTrader(s, db)
            try:
                trader.open_long('AAA-EUR', Book(99.9,100), 1, now_ms=100)
                p = db.get_position('AAA-EUR')
                self.assertIsNotNone(p)
                event = trader.process_candle('AAA-EUR', Candle(2,100,p.take_price+1,99,101,1), now_ms=200)
                self.assertIsNotNone(event)
                cash_after = db.cash_eur()
                with self.assertRaises(RuntimeError):
                    db.close_position_atomic(p, 300, 101, 0, 200, 1, 0.5, 'duplicate')
                self.assertAlmostEqual(db.cash_eur(), cash_after, places=8)
                self.assertEqual(len(db.trade_rows()), 1)
            finally:
                db.close()

    def test_trade_timestamps_use_prospective_wall_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = replace(Settings(), db_path=str(Path(tmp)/'x.db'), slippage_pct=0)
            db = Storage(s.db_path, s.paper_start_eur)
            trader = PaperTrader(s, db)
            try:
                trader.open_long('AAA-EUR', Book(99.9,100), 10, now_ms=1_000_000)
                p = db.get_position('AAA-EUR')
                trader.process_candle('AAA-EUR', Candle(20,100,p.take_price+1,99,101,1), now_ms=2_000_000)
                row = db.trade_rows()[0]
                self.assertEqual(int(row['opened_at_ms']), 1_000_000)
                self.assertEqual(int(row['closed_at_ms']), 2_000_000)
            finally:
                db.close()

    def test_database_health_and_data_state_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp)/'x.db')
            db = Storage(path, 5000)
            db.set_data_health('BLOCKED', 'test detail')
            self.assertTrue(db.health()['ok'])
            db.close()
            db = Storage(path, 5000)
            try:
                self.assertTrue(db.health()['ok'])
                self.assertEqual(db.data_health()[0], 'BLOCKED')
                self.assertEqual(db.data_health()[1], 'test detail')
            finally:
                db.close()

    def test_readiness_separates_local_safety_from_market_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = replace(Settings(), db_path=str(Path(tmp)/'x.db'), universe_size=1)
            db = Storage(s.db_path, s.paper_start_eur)
            try:
                r = readiness(db, s)
                self.assertTrue(r['local_ok'])
                self.assertFalse(r['paper_observation_ready'])
                db.set_universe(['SIM-EUR'])
                db.set_data_health('READY', 'synthetic test')
                r = readiness(db, s)
                self.assertTrue(r['paper_observation_ready'])
            finally:
                db.close()

    def test_corrupt_universe_blocks_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = replace(Settings(), db_path=str(Path(tmp)/'x.db'), universe_size=1)
            db = Storage(s.db_path, s.paper_start_eur)
            try:
                db.set_state('universe_json', '{broken')
                health = db.health()
                self.assertFalse(health['ok'])
                self.assertIn('universe_ongeldig', health['errors'])
                r = readiness(db, s)
                self.assertFalse(r['local_ok'])
                self.assertFalse(r['paper_observation_ready'])
            finally:
                db.close()

    def test_partial_data_is_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = replace(Settings(), db_path=str(Path(tmp)/'x.db'), universe_size=1)
            db = Storage(s.db_path, s.paper_start_eur)
            try:
                db.set_universe(['SIM-EUR'])
                db.set_data_health('PARTIAL', '1 markt faalt')
                r = readiness(db, s)
                self.assertTrue(r['local_ok'])
                self.assertFalse(r['data_ok'])
                self.assertFalse(r['paper_observation_ready'])
            finally:
                db.close()

    def test_full_pipeline_offline(self):
        result = run_offline_check()
        self.assertTrue(result['ok'])
        self.assertEqual(result['trades'], 1)
        self.assertEqual(result['exit_reason'], 'take_profit')


if __name__ == '__main__':
    unittest.main()
