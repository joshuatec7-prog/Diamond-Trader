import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from config import Settings
from missed_trade_audit import (
    STRATEGY_A,
    audit_db_path,
    estimated_roundtrip_cost_floor_pct,
    update_missed_trade_audit,
)
from models import Candle, Decision
from storage import Storage


class MissedTradeAuditTests(unittest.TestCase):
    def test_skip_returns_are_measured_without_mutating_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            primary = str(Path(tmp) / 'cleanroom.db')
            s = replace(Settings(), db_path=primary)
            db = Storage(primary, s.paper_start_eur)
            decision_ts = 10 * 900_000
            try:
                db.set_universe(['AAA-EUR'])
                db.save_decision(
                    'AAA-EUR',
                    decision_ts,
                    Decision('SKIP', 'momentum_te_zwak', {'close': 100.0}),
                )
                candles = []
                for i in range(1, 49):
                    close = 100.0 * (1.0 + 0.001 * i)
                    candles.append(
                        Candle(
                            decision_ts + i * 900_000,
                            close,
                            close * 1.01,
                            close * 0.99,
                            close,
                            1.0,
                        )
                    )
                db.save_candles('AAA-EUR', s.interval, candles)
            finally:
                db.close()

            before = Path(primary).stat().st_size
            result = update_missed_trade_audit(s)
            after = Path(primary).stat().st_size

            self.assertEqual(result['imported'], 1)
            self.assertGreaterEqual(result['updated'], 1)
            self.assertEqual(before, after)

            audit = sqlite3.connect(audit_db_path(primary))
            audit.row_factory = sqlite3.Row
            try:
                row = audit.execute(
                    'SELECT * FROM skip_audit WHERE strategy=?',
                    (STRATEGY_A,),
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertAlmostEqual(float(row['r15m_pct']), 0.1, places=6)
                self.assertAlmostEqual(float(row['r1h_pct']), 0.4, places=6)
                self.assertAlmostEqual(float(row['r4h_pct']), 1.6, places=6)
                self.assertAlmostEqual(float(row['r12h_pct']), 4.8, places=6)
                self.assertGreater(float(row['mfe12h_pct']), 4.8)
                self.assertLess(float(row['mae12h_pct']), 0.0)
            finally:
                audit.close()

            again = update_missed_trade_audit(s)
            self.assertEqual(again['imported'], 0)
            self.assertEqual(again['updated'], 0)

    def test_cost_floor_is_fee_and_slippage_both_sides(self):
        s = replace(Settings(), taker_fee_pct=0.25, slippage_pct=0.08)
        self.assertAlmostEqual(estimated_roundtrip_cost_floor_pct(s), 0.66, places=8)


if __name__ == '__main__':
    unittest.main()
