import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from config import Settings
from models import Book, Candle
from paper_trader import PaperTrader
from storage import Storage
from trend_main import build_trend_settings, run_trend_cycle, trend_db_path
from trend_strategy import TrendMomentumStrategy


INTERVAL_MS = 900_000


def candles_from_closes(closes: list[float], last_ts: int | None = None) -> list[Candle]:
    if last_ts is None:
        last_ts = (int(time.time() * 1000) // INTERVAL_MS) * INTERVAL_MS - INTERVAL_MS
    start = last_ts - (len(closes) - 1) * INTERVAL_MS
    return [
        Candle(start + i * INTERVAL_MS, value, value * 1.002, value * 0.998, value, 1.0)
        for i, value in enumerate(closes)
    ]


class MultiMarketData:
    def __init__(self, candles_by_market: dict[str, list[Candle]]) -> None:
        self.candles_by_market = candles_by_market

    def closed_candles(self, market: str, interval: str, limit: int,
                       now_ms: int | None = None) -> list[Candle]:
        return list(self.candles_by_market[market])

    def book(self, market: str) -> Book:
        last = self.candles_by_market[market][-1].close
        return Book(last * 0.999, last * 1.001)


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

    def test_rank_score_prefers_stronger_valid_trend(self):
        weak = [100.0 * (1.001 ** i) for i in range(60)]
        strong = [100.0 * (1.002 ** i) for i in range(60)]
        strategy = TrendMomentumStrategy(Settings())
        weak_d = strategy.evaluate(candles_from_closes(weak))
        strong_d = strategy.evaluate(candles_from_closes(strong))
        self.assertEqual(weak_d.action, 'BUY')
        self.assertEqual(strong_d.action, 'BUY')
        self.assertGreater(strategy.rank_score(strong_d), strategy.rank_score(weak_d))

    def test_trend_v2_has_own_database_and_three_slots(self):
        primary = replace(Settings(), db_path='/tmp/cryptobot_cleanroom.db')
        trend = build_trend_settings(primary)
        self.assertEqual(trend.max_open_positions, 3)
        self.assertEqual(trend_db_path(primary.db_path), '/tmp/cryptobot_cleanroom_trend_v2.db')
        self.assertEqual(trend.db_path, '/tmp/cryptobot_cleanroom_trend_v2.db')

    def test_ranked_cycle_opens_best_three_not_first_three(self):
        markets = ['AAA-EUR', 'BBB-EUR', 'CCC-EUR', 'DDD-EUR']
        growth = {
            'AAA-EUR': 1.0010,
            'BBB-EUR': 1.0012,
            'CCC-EUR': 1.0014,
            'DDD-EUR': 1.0016,
        }
        candles_by_market = {
            market: candles_from_closes([100.0 * (factor ** i) for i in range(60)])
            for market, factor in growth.items()
        }

        with tempfile.TemporaryDirectory() as tmp:
            s = replace(
                Settings(),
                db_path=str(Path(tmp) / 'trend_v2.db'),
                max_open_positions=3,
                slippage_pct=0.0,
            )
            db = Storage(s.db_path, s.paper_start_eur)
            db.set_universe(markets)
            strategy = TrendMomentumStrategy(s)
            trader = PaperTrader(s, db, entry_reason='trend_breakout')
            source = MultiMarketData(candles_by_market)

            try:
                expected = []
                for market in markets:
                    decision = strategy.evaluate(candles_by_market[market])
                    self.assertEqual(decision.action, 'BUY')
                    expected.append((strategy.rank_score(decision), market))
                expected_markets = {
                    market for _, market in sorted(expected, key=lambda x: (-x[0], x[1]))[:3]
                }

                ok, failed, _ = run_trend_cycle(source, db, strategy, trader, s, markets)
                self.assertEqual((ok, failed), (4, 0))
                self.assertEqual(len(db.all_positions()), 3)
                self.assertEqual({p.market for p in db.all_positions()}, expected_markets)

                weakest = sorted(expected, key=lambda x: (-x[0], x[1]))[3][1]
                row = db.conn.execute(
                    'SELECT action,reason FROM decisions WHERE market=?',
                    (weakest,),
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(str(row['action']), 'SKIP')
                self.assertEqual(str(row['reason']), 'rank_buiten_top_slots')
            finally:
                db.close()

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
