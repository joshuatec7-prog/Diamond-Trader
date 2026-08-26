import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from backtest import run_backtest
from bitvavo_market import BitvavoMarket
from config import Settings
from indicators import atr, ema, rsi
from models import Book, Candle, Position
from paper_trader import PaperTrader
from storage import Storage
from strategy import TrendBreakoutStrategy


class FakeResponse:
    status_code = 200
    headers = {}
    def __init__(self, payload):
        self.payload = payload
    def raise_for_status(self):
        return None
    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.headers = {}
    def get(self, *args, **kwargs):
        return FakeResponse(self.payload)


class CoreTests(unittest.TestCase):
    def test_ema(self):
        values = [float(i) for i in range(1, 31)]
        out = ema(values, 5)
        self.assertEqual(len(out), len(values))
        self.assertGreater(out[-1], out[-2])

    def test_rsi(self):
        values = [float(i) for i in range(1, 31)]
        self.assertEqual(rsi(values, 14)[-1], 100.0)

    def test_atr(self):
        candles = [Candle(i * 60000, 100+i, 102+i, 99+i, 101+i, 10) for i in range(30)]
        self.assertGreater(atr(candles, 14)[-1], 0)

    def test_open_candle_removed_and_sorted(self):
        payload = [
            [1800000, "102", "103", "101", "102.5", "5"],
            [0, "100", "101", "99", "100.5", "5"],
            [900000, "101", "102", "100", "101.5", "5"],
        ]
        api = BitvavoMarket("https://example.invalid", session=FakeSession(payload), retries=1)
        closed = api.get_closed_candles("BTC-EUR", "15m", 10, now_ms=2250000)
        self.assertEqual([c.timestamp_ms for c in closed], [0, 900000])

    def test_strategy_buy(self):
        s = replace(Settings(), ema_fast=5, ema_slow=10, rsi_period=5, atr_period=5,
                    breakout_lookback=5, volume_lookback=5, min_volume_ratio=1.0,
                    rsi_min=50, rsi_max=100)
        candles = []
        for i in range(30):
            close = 100 + i * 0.4
            candles.append(Candle(i * 900000, close - 0.2, close + 0.3, close - 0.4, close, 100 + i))
        last = candles[-1]
        candles[-1] = Candle(last.timestamp_ms, last.open, last.high + 2, last.low, candles[-2].close + 2.0, 300)
        self.assertEqual(TrendBreakoutStrategy(s).evaluate(candles).action, "BUY")

    def test_position_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "db.sqlite")
            db = Storage(path, 1000)
            p = Position("BTC-EUR", 1, 1, 100, 1, 100, 0.25, 2, 98, 104, 100)
            db.upsert_position(p)
            db.close()
            db2 = Storage(path, 1000)
            self.assertIsNotNone(db2.get_position("BTC-EUR"))
            db2.close()

    def test_paper_take_profit(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = replace(Settings(), db_path=str(Path(tmp) / "db.sqlite"), paper_start_eur=1000,
                        stake_eur=100, stop_atr_mult=1.0, take_atr_mult=2.0)
            db = Storage(s.db_path, s.paper_start_eur)
            trader = PaperTrader(s, db)
            trader.open_long("BTC-EUR", Book(99.9, 100.0), 2.0, 1000, now_ms=1000)
            p = db.get_position("BTC-EUR")
            event = trader.process_candle(
                "BTC-EUR",
                Candle(2000, 100, p.take_price + 0.1, p.stop_price + 0.1, 102, 10),
            )
            self.assertEqual(event.reason, "take_profit")
            self.assertIsNone(db.get_position("BTC-EUR"))
            db.close()

    def test_paper_same_candle_stop_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = replace(Settings(), db_path=str(Path(tmp) / "db.sqlite"), paper_start_eur=1000,
                        stake_eur=100, stop_atr_mult=1.0, take_atr_mult=2.0)
            db = Storage(s.db_path, s.paper_start_eur)
            trader = PaperTrader(s, db)
            trader.open_long("ETH-EUR", Book(99.9, 100.0), 2.0, 1000, now_ms=1000)
            p = db.get_position("ETH-EUR")
            event = trader.process_candle("ETH-EUR", Candle(2000, 100, p.take_price + 5, p.stop_price - 5, 100, 10))
            self.assertEqual(event.reason, "stop")
            db.close()

    def test_backtest_runs(self):
        s = replace(Settings(), ema_fast=3, ema_slow=5, rsi_period=3, atr_period=3,
                    breakout_lookback=3, volume_lookback=3, min_volume_ratio=0.5,
                    rsi_min=0, rsi_max=100, stop_atr_mult=2, take_atr_mult=3)
        candles = []
        for i in range(40):
            close = 100 + i * 0.5
            candles.append(Candle(i * 900000, close - 0.1, close + 0.4, close - 0.3, close, 100 + i))
        self.assertGreaterEqual(run_backtest("BTC-EUR", candles, s).trades, 1)


if __name__ == "__main__":
    unittest.main()
