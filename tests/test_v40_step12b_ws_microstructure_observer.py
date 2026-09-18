from __future__ import annotations

import unittest

from research.v40_step12b_ws_microstructure_observer import (
    BookDesync,
    FlowStats,
    OrderBookState,
    summarize_samples,
)


class Step12BMicrostructureTests(unittest.TestCase):
    def test_taker_flow_uses_quote_volume_and_side(self):
        f = FlowStats()
        f.add_trade({"price": "100", "amount": "2", "side": "buy"})
        f.add_trade({"price": "100", "amount": "1", "side": "sell"})
        s = f.summary()
        self.assertEqual(s["trade_count"], 2)
        self.assertAlmostEqual(s["taker_flow_imbalance"], 1 / 3, places=8)
        self.assertAlmostEqual(s["taker_sell_share_pct"], 100 / 3, places=6)

    def test_book_snapshot_and_contiguous_delta(self):
        book = OrderBookState.from_snapshot({
            "market": "BTC-EUR",
            "nonce": 10,
            "bids": [["99", "2"], ["98", "3"]],
            "asks": [["101", "1"], ["102", "4"]],
        })
        before = book.metrics()
        self.assertGreater(before["spread_pct"], 0)
        status = book.apply_event({
            "event": "book",
            "market": "BTC-EUR",
            "nonce": 11,
            "bids": [["99", "0"], ["100", "1"]],
            "asks": [["101", "2"]],
        })
        self.assertEqual(status, "applied")
        after = book.metrics()
        self.assertEqual(book.nonce, 11)
        self.assertAlmostEqual(after["bid"], 100.0)
        self.assertAlmostEqual(after["ask"], 101.0)

    def test_nonce_gap_forces_resync(self):
        book = OrderBookState.from_snapshot({
            "market": "BTC-EUR",
            "nonce": 20,
            "bids": [["99", "1"]],
            "asks": [["101", "1"]],
        })
        with self.assertRaises(BookDesync):
            book.apply_event({
                "event": "book",
                "market": "BTC-EUR",
                "nonce": 22,
                "bids": [["99", "2"]],
                "asks": [],
            })

    def test_sample_summary_preserves_spread_and_pressure(self):
        s = summarize_samples([
            {"spread_pct": 0.10, "near_book_imbalance": -0.40, "mid": 100.0},
            {"spread_pct": 0.20, "near_book_imbalance": 0.20, "mid": 101.0},
            {"spread_pct": 0.15, "near_book_imbalance": -0.20, "mid": 102.0},
        ])
        self.assertEqual(s["sample_count"], 3)
        self.assertAlmostEqual(s["spread_median_pct"], 0.15)
        self.assertAlmostEqual(s["spread_range_pct"], 0.10)
        self.assertAlmostEqual(s["book_imbalance_median"], -0.20)
        self.assertAlmostEqual(s["mid_change_pct"], 2.0)


if __name__ == "__main__":
    unittest.main()
