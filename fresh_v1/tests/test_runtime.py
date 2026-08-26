import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from bitvavo_market import BitvavoMarket, BitvavoPermanentError
from config import Settings
from main import process_market, run_cycle
from models import Candle, Signal
from paper_trader import PaperTrader
from storage import Storage


class ErrorApi:
    def get_closed_candles(self, *args, **kwargs):
        raise RuntimeError("marktdata fout")


class StaleApi:
    def __init__(self, candles):
        self.candles = candles
        self.book_calls = 0

    def get_closed_candles(self, *args, **kwargs):
        return self.candles

    def get_book(self, market):
        self.book_calls += 1
        raise AssertionError("stale signaal mag geen orderboek opvragen")


class BuyStrategy:
    def evaluate(self, candles):
        return Signal("BUY", "trend_breakout", {"atr": 1.0})


class Response403:
    status_code = 403
    headers = {}
    text = "Forbidden"

    def raise_for_status(self):
        raise AssertionError("403 hoort vóór raise_for_status afgevangen te worden")


class Session403:
    def __init__(self):
        self.headers = {}
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return Response403()


class RuntimeTests(unittest.TestCase):
    def test_cycle_reports_market_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = replace(Settings(), markets=("BTC-EUR",), db_path=str(Path(tmp) / "db.sqlite"))
            db = Storage(s.db_path, s.paper_start_eur)
            trader = PaperTrader(s, db)
            stats = run_cycle(s, ErrorApi(), db, BuyStrategy(), trader)
            self.assertEqual(stats.markets_ok, 0)
            self.assertEqual(stats.markets_failed, 1)
            db.close()

    def test_stale_buy_signal_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = replace(
                Settings(), markets=("BTC-EUR",), db_path=str(Path(tmp) / "db.sqlite"),
                max_entry_delay_seconds=180,
            )
            now_ms = int(time.time() * 1000)
            last_open_ms = now_ms - 900_000 - 600_000
            candles = []
            for i in range(60):
                ts = last_open_ms - ((59 - i) * 900_000)
                candles.append(Candle(ts, 100, 101, 99, 100.5, 10))
            api = StaleApi(candles)
            db = Storage(s.db_path, s.paper_start_eur)
            trader = PaperTrader(s, db)
            ok = process_market("BTC-EUR", s, api, db, BuyStrategy(), trader)
            self.assertTrue(ok)
            self.assertEqual(api.book_calls, 0)
            row = db.conn.execute("SELECT action, reason FROM signals ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(row["action"], "SKIP")
            self.assertEqual(row["reason"], "entry_signal_stale")
            self.assertIsNone(db.get_position("BTC-EUR"))
            db.close()

    def test_http_403_is_not_retried(self):
        session = Session403()
        api = BitvavoMarket("https://example.invalid", retries=3, session=session)
        with self.assertRaises(BitvavoPermanentError):
            api.get_candles("BTC-EUR", "15m", 10)
        self.assertEqual(session.calls, 1)


if __name__ == "__main__":
    unittest.main()
