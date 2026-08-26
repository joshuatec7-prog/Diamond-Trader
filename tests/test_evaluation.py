import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from config import Settings
from models import Book, Candle
from paper_trader import PaperTrader
from report import performance, verdict
from storage import Storage


DAY = 86_400_000


def insert_trade(db: Storage, index: int, pnl: float, total: int = 40) -> None:
    opened = int(index * (15 * DAY) / max(1, total - 1))
    closed = opened + 1_000
    entry_notional = 200.0
    amount = 2.0
    entry_price = 100.0
    entry_fee = 0.0
    exit_fee = 0.0
    exit_price = (entry_notional + pnl) / amount
    pnl_pct = pnl / entry_notional * 100.0
    db.conn.execute(
        '''INSERT INTO trades(market,opened_at_ms,closed_at_ms,entry_price,exit_price,amount,
                              entry_fee,exit_fee,pnl_eur,pnl_pct,exit_reason)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
        ('SIM-EUR', opened, closed, entry_price, exit_price, amount, entry_fee, exit_fee,
         pnl, pnl_pct, 'test'),
    )
    db.conn.commit()


class EvaluationTests(unittest.TestCase):
    def test_fixed_gate_can_pass_after_minimum_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Settings()
            db = Storage(str(Path(tmp)/'x.db'), s.paper_start_eur)
            try:
                pnls = [2.0] * 30 + [-1.0] * 10
                for i, pnl in enumerate(pnls):
                    insert_trade(db, i, pnl)
                p = performance(db, s.paper_start_eur)
                result, reasons = verdict(p, s)
                self.assertEqual(p.trades, 40)
                self.assertGreaterEqual(p.span_days, 14)
                self.assertEqual(result, 'PASS')
                self.assertEqual(reasons, [])
            finally:
                db.close()

    def test_gate_waits_even_if_early_results_are_positive(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Settings()
            db = Storage(str(Path(tmp)/'x.db'), s.paper_start_eur)
            try:
                for i in range(39):
                    insert_trade(db, i, 2.0, total=39)
                result, reasons = verdict(performance(db, s.paper_start_eur), s)
                self.assertEqual(result, 'WAIT')
                self.assertTrue(any('trades' in r for r in reasons))
            finally:
                db.close()

    def test_drawdown_gate_can_fail_despite_positive_pnl_and_pf(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Settings()
            db = Storage(str(Path(tmp)/'x.db'), s.paper_start_eur)
            try:
                pnls = [100.0, -600.0] + [20.0] * 38
                for i, pnl in enumerate(pnls):
                    insert_trade(db, i, pnl)
                p = performance(db, s.paper_start_eur)
                result, reasons = verdict(p, s)
                self.assertGreater(p.pnl_eur, 0)
                self.assertGreater(p.profit_factor, s.eval_min_profit_factor)
                self.assertGreater(p.max_drawdown_pct, s.eval_max_drawdown_pct)
                self.assertEqual(result, 'FAIL')
                self.assertTrue(any('drawdown' in r for r in reasons))
            finally:
                db.close()

    def test_spread_gate_and_time_exit_costs(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = replace(
                Settings(), db_path=str(Path(tmp)/'x.db'), max_hold_bars=1,
                taker_fee_pct=0.25, slippage_pct=0.10,
            )
            db = Storage(s.db_path, s.paper_start_eur)
            trader = PaperTrader(s, db)
            try:
                ok, reason = trader.can_open('SIM-EUR', Book(99.0, 101.0))
                self.assertFalse(ok)
                self.assertEqual(reason, 'spread_te_hoog')

                event = trader.open_long('SIM-EUR', Book(99.95, 100.0), 10, now_ms=1_000)
                self.assertIsNotNone(event)
                p = db.get_position('SIM-EUR')
                candle = Candle(20, 101.0, 101.5, 99.5, 101.0, 1.0)
                event = trader.process_candle('SIM-EUR', candle, now_ms=2_000)
                self.assertEqual(event.reason, 'time_exit')

                expected_entry_price = 100.0 * 1.001
                amount = s.position_eur / expected_entry_price
                entry_fee = s.position_eur * 0.0025
                expected_exit_price = 101.0 * 0.999
                exit_notional = amount * expected_exit_price
                exit_fee = exit_notional * 0.0025
                expected_pnl = exit_notional - exit_fee - s.position_eur - entry_fee
                self.assertAlmostEqual(float(db.trade_rows()[0]['pnl_eur']), expected_pnl, places=10)
            finally:
                db.close()

    def test_database_health_detects_corrupted_cash_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Settings()
            db = Storage(str(Path(tmp)/'x.db'), s.paper_start_eur)
            try:
                db.set_state('cash_eur', 'nan')
                health = db.health()
                self.assertFalse(health['ok'])
                self.assertIn('cash_ongeldig', health['errors'])
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
