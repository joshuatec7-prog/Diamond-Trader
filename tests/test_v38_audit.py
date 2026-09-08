import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from v38_audit import MINUTE_MS, MinuteCandle, replay_vet, runtime_audit


class V38AuditTests(unittest.TestCase):
    def test_runtime_audit_proves_complete_market_tracking_and_vet(self):
        class Api:
            def trading_markets(self, quote):
                self.quote = quote
                return ['BTC-EUR', 'VET-EUR']

            def quote_market_tickers(self, quote):
                return [
                    {'market': 'BTC-EUR', 'last': 100.0, 'volume_quote': 1_000_000.0},
                    {'market': 'VET-EUR', 'last': 0.02, 'volume_quote': 2_000_000.0},
                ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / 'v38.db'
            report = root / 'v38.json'
            with sqlite3.connect(db) as conn:
                conn.execute('CREATE TABLE v38_prices(captured_ms INTEGER,market TEXT,price REAL,volume_quote REAL)')
                for timestamp_ms in (0, 21 * MINUTE_MS):
                    conn.executemany(
                        'INSERT INTO v38_prices VALUES (?,?,?,?)',
                        [(timestamp_ms, 'BTC-EUR', 100, 1_000_000),
                         (timestamp_ms, 'VET-EUR', .02, 2_000_000)],
                    )
            report.write_text(json.dumps({
                'mode': 'OBSERVE_ONLY', 'paper_execution': 'UIT',
                'live_orders': 'UIT / TECHNISCH ONMOGELIJK', 'execution_enabled': False,
            }), encoding='utf-8')
            result = runtime_audit(Api(), db, report, now_ms=21 * MINUTE_MS)
            self.assertTrue(result['complete'])
            self.assertTrue(result['passed_20_minutes'])
            self.assertTrue(result['vet_tracked'])

    def test_vet_replay_can_find_signal_before_entry(self):
        entry_ms = 120 * MINUTE_MS
        candles = []
        for minute in range(121):
            price = 0.02 * (1 + minute * 0.0007)
            candles.append(MinuteCandle(
                minute * MINUTE_MS, price, price * 1.001, price * .999, price, 100_000,
            ))
        result = replay_vet(candles, entry_ms, 120 * MINUTE_MS)
        self.assertIsNotNone(result['first_signal_before_entry'])
        self.assertGreater(result['jury_signals'], 0)


if __name__ == '__main__':
    unittest.main()
