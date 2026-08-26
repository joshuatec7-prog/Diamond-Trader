import unittest
from dataclasses import replace

from backtest import run_backtest
from config import Settings
from models import Candle


class BacktestTests(unittest.TestCase):
    def test_backtest_runs_without_lookahead_entry(self):
        s = replace(Settings(), band_window=5, band_stddev=1.0, max_hold_bars=3)
        closes = [10,10,10,10,10,8,9,9.2,9.4,9.6,10,10,10]
        candles = [Candle(i*3_600_000,v,v+0.3,v-0.3,v,1) for i,v in enumerate(closes)]
        r = run_backtest('AAA-EUR',candles,s)
        self.assertGreaterEqual(r.trades,1)

    def test_entry_candle_is_exposed_to_stop_immediately(self):
        s = replace(
            Settings(), band_window=5, band_stddev=1.0, max_hold_bars=3,
            taker_fee_pct=0, slippage_pct=0, backtest_assumed_spread_pct=0,
            stop_loss_pct=1.0, take_profit_pct=10.0,
        )
        closes = [10,10,10,10,10,8,9,9,9,9]
        candles = [Candle(i*3_600_000,v,v+0.1,v-0.1,v,1) for i,v in enumerate(closes)]
        # Signaal ontstaat op index 6; entry is op open index 7 (=9).
        # Alleen die entry-candle raakt de 1% stop.
        candles[7] = Candle(7*3_600_000, 9.0, 9.05, 8.0, 9.0, 1)
        r = run_backtest('AAA-EUR', candles, s)
        self.assertGreaterEqual(r.trades, 1)
        self.assertLess(r.pnl_eur, 0)

    def test_one_bar_max_hold_exits_on_entry_candle_close(self):
        s = replace(
            Settings(), band_window=5, band_stddev=1.0, max_hold_bars=1,
            taker_fee_pct=0, slippage_pct=0, backtest_assumed_spread_pct=0,
            stop_loss_pct=20.0, take_profit_pct=20.0,
        )
        closes = [10,10,10,10,10,8,9,9.5,9.5]
        candles = [Candle(i*3_600_000,v,v+0.05,v-0.05,v,1) for i,v in enumerate(closes)]
        candles[7] = Candle(7*3_600_000, 9.0, 9.55, 8.95, 9.5, 1)
        r = run_backtest('AAA-EUR', candles, s)
        self.assertEqual(r.trades, 1)
        self.assertGreater(r.pnl_eur, 0)


if __name__ == '__main__':
    unittest.main()
