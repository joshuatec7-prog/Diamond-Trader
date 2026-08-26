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


if __name__ == '__main__':
    unittest.main()
