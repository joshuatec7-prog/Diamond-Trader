import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from audit_all import STRATEGY_B, STRATEGY_C
from auto_research_controller import (
    MODE,
    load_report,
    print_report,
    research_db_path,
    research_report_path,
    run_once,
)
from config import Settings
from missed_trade_audit import STRATEGY_A


class AutoResearchControllerTests(unittest.TestCase):
    def _source_db(self, path: str, status: str = 'READY') -> sqlite3.Connection:
        conn = sqlite3.connect(path)
        conn.executescript(
            '''
            CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE positions (market TEXT PRIMARY KEY);
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market TEXT NOT NULL,
                closed_at_ms INTEGER NOT NULL,
                exit_price REAL NOT NULL,
                pnl_eur REAL NOT NULL,
                exit_reason TEXT NOT NULL
            );
            CREATE TABLE candles (
                market TEXT NOT NULL,
                interval TEXT NOT NULL,
                timestamp_ms INTEGER NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL
            );
            '''
        )
        conn.executemany(
            'INSERT INTO state(key,value) VALUES(?,?)',
            [('cash_eur', '5000'), ('data_status', status)],
        )
        conn.commit()
        return conn

    def _audit_db(self, path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(path)
        conn.execute(
            '''CREATE TABLE skip_audit (
                   strategy TEXT NOT NULL,
                   r15m_pct REAL,
                   r1h_pct REAL,
                   r4h_pct REAL
               )'''
        )
        conn.commit()
        return conn

    def test_paths_are_separate_from_trading_database(self):
        base = '/tmp/cryptobot_cleanroom.db'
        self.assertEqual(
            research_db_path(base),
            '/tmp/cryptobot_cleanroom_research_controller.db',
        )
        self.assertEqual(
            research_report_path(base),
            '/tmp/cryptobot_cleanroom_research_controller_report.json',
        )

    def test_hourly_observer_measures_stop_rebound_without_mutating_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = str(Path(tmp) / 'cryptobot_cleanroom.db')
            a_path = base
            b_path = str(Path(tmp) / 'cryptobot_cleanroom_trend_v7.db')
            c_path = str(Path(tmp) / 'cryptobot_cleanroom_continuation_v6.db')
            controller_path = str(Path(tmp) / 'controller.db')
            report_path = str(Path(tmp) / 'controller.json')
            audit_path = str(Path(tmp) / 'missed.db')

            a = self._source_db(a_path)
            b = self._source_db(b_path)
            c = self._source_db(c_path)
            audit = self._audit_db(audit_path)
            try:
                b.execute(
                    '''INSERT INTO trades(market,closed_at_ms,exit_price,pnl_eur,exit_reason)
                       VALUES('AAA-EUR',900000,100.0,-3.0,'stop_loss')'''
                )
                b.executemany(
                    '''INSERT INTO candles(market,interval,timestamp_ms,high,low,close)
                       VALUES('AAA-EUR','15m',?,?,?,?)''',
                    [
                        (900000, 101.0, 99.0, 100.5),
                        (1800000, 102.0, 99.5, 101.0),
                        (2700000, 103.0, 100.0, 102.0),
                        (3600000, 104.0, 100.5, 103.5),
                    ],
                )
                b.commit()
                audit.execute(
                    '''INSERT INTO skip_audit(strategy,r15m_pct,r1h_pct,r4h_pct)
                       VALUES(?,?,?,?)''',
                    (STRATEGY_B, 1.0, 2.0, None),
                )
                audit.commit()
            finally:
                a.close()
                b.close()
                c.close()
                audit.close()

            settings = Settings(db_path=base)
            sources = {
                STRATEGY_A: a_path,
                STRATEGY_B: b_path,
                STRATEGY_C: c_path,
            }
            report = run_once(
                settings,
                now_ms=5_000_000,
                controller_path=controller_path,
                report_path=report_path,
                source_paths=sources,
                missed_path=audit_path,
            )

            self.assertEqual(report['mode'], MODE)
            self.assertFalse(report['auto_modify_strategy'])
            self.assertFalse(report['auto_deploy'])
            self.assertEqual(report['strategies'][STRATEGY_B]['closed_trades'], 1)
            self.assertIn('VERZAMELEN', report['recommendations'][STRATEGY_B])

            rebound = report['stop_rebound_summary'][STRATEGY_B][60]
            self.assertEqual(rebound['n'], 1)
            self.assertAlmostEqual(rebound['avg_end_return_pct'], 3.5, places=6)
            self.assertAlmostEqual(rebound['avg_max_up_pct'], 4.0, places=6)
            self.assertEqual(rebound['recovered_1_5pct_pct'], 100.0)

            missed = report['missed_trade_summary'][STRATEGY_B][60]
            self.assertEqual(missed['n'], 1)
            self.assertAlmostEqual(missed['avg_return_pct'], 2.0, places=6)

            source = sqlite3.connect(b_path)
            try:
                self.assertEqual(source.execute('SELECT COUNT(*) FROM trades').fetchone()[0], 1)
                self.assertEqual(source.execute('SELECT COUNT(*) FROM candles').fetchone()[0], 4)
            finally:
                source.close()

    def test_same_hour_updates_snapshot_without_duplicate_and_json_report_prints(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = str(Path(tmp) / 'cryptobot_cleanroom.db')
            a_path = base
            b_path = str(Path(tmp) / 'cryptobot_cleanroom_trend_v7.db')
            c_path = str(Path(tmp) / 'cryptobot_cleanroom_continuation_v6.db')
            controller_path = str(Path(tmp) / 'controller.db')
            report_path = str(Path(tmp) / 'controller.json')
            audit_path = str(Path(tmp) / 'missed.db')

            for path in (a_path, b_path, c_path):
                self._source_db(path).close()
            self._audit_db(audit_path).close()

            settings = Settings(db_path=base)
            sources = {
                STRATEGY_A: a_path,
                STRATEGY_B: b_path,
                STRATEGY_C: c_path,
            }
            for now_ms in (7_000_000, 7_100_000):
                run_once(
                    settings,
                    now_ms=now_ms,
                    controller_path=controller_path,
                    report_path=report_path,
                    source_paths=sources,
                    missed_path=audit_path,
                )

            db = sqlite3.connect(controller_path)
            try:
                self.assertEqual(
                    db.execute('SELECT COUNT(*) FROM strategy_snapshots').fetchone()[0],
                    3,
                )
            finally:
                db.close()

            loaded = load_report(report_path)
            self.assertIsNotNone(loaded)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                print_report(loaded)
            text = output.getvalue()
            self.assertIn('AUTO WIJZIGEN   : NEE', text)
            self.assertIn('AUTO DEPLOY     : NEE', text)


if __name__ == '__main__':
    unittest.main()
