import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import autonomous_v39 as v39
from autonomous_v37 import V37Settings, recheck_candidates
from models import Candle


NOW_MS = 300_000_000


def strong_candles(now_ms: int, interval_ms: int) -> list[Candle]:
    latest = now_ms // interval_ms * interval_ms - interval_ms
    rows = []
    for index in range(60):
        timestamp = latest - (59 - index) * interval_ms
        close = 100.0 + index * 0.20 + [0.0, .35, -.20, .25, -.10][index % 5]
        rows.append(Candle(
            timestamp_ms=timestamp, open=close - .1, high=close + .8,
            low=close - .8, close=close,
            volume=170.0 if index == 59 else 100.0,
        ))
    return rows


class PublicApi:
    def __init__(self, now_ms: int):
        self.now_ms = now_ms
        self.depth_calls = 0

    def closed_candles(self, market, interval, limit, now_ms=None):
        del market
        duration = {'5m': 300_000, '15m': 900_000, '1h': 3_600_000}[interval]
        return strong_candles(int(now_ms or self.now_ms), duration)[-limit:]

    def depth_book(self, market, notional):
        del market
        self.depth_calls += 1
        return {
            'captured_at_ms': self.now_ms,
            'buy_vwap': 100.0,
            'sell_vwap': 99.95,
            'execution_spread_pct': .05,
            'near_book_imbalance': .20,
            'notional_quote': notional,
        }

    def sell_vwap_for_base(self, market, base_amount):
        del market
        return {
            'captured_at_ms': self.now_ms,
            'sell_vwap': 102.0,
            'base_amount': base_amount,
        }


class AutonomousV39Tests(unittest.TestCase):
    def settings(self, root: Path) -> V37Settings:
        return replace(
            V37Settings(), universe_size=10, maximum_l2_candidates=5,
            db_path=str(root / 'v39.db'), report_path=str(root / 'v39.json'),
        )

    def sources(self, root: Path, now_ms: int = NOW_MS) -> dict[str, str]:
        v38 = root / 'v38.db'
        with sqlite3.connect(v38) as conn:
            conn.execute(
                '''CREATE TABLE v38_decisions(
                   evaluated_ms INTEGER,market TEXT,action TEXT,score REAL,details_json TEXT)'''
            )
            rows = [
                (now_ms, 'VET-EUR', 'DOOR_NAAR_MENSELIJKE_JURY', 80.0,
                 json.dumps({'features': {'last': .02}, 'volume_quote': 2_000_000})),
                (now_ms, 'LOW-EUR', 'DOOR_NAAR_MENSELIJKE_JURY', 64.9,
                 json.dumps({'features': {'last': 1}, 'volume_quote': 2_000_000})),
                (now_ms, 'WAIT-EUR', 'ALLEEN_VOLGEN', 90.0,
                 json.dumps({'features': {'last': 1}, 'volume_quote': 2_000_000})),
            ]
            conn.executemany('INSERT INTO v38_decisions VALUES (?,?,?,?,?)', rows)
        v37_report = root / 'v37.json'
        v37_report.write_text(json.dumps({
            'generated_at_ms': now_ms,
            'latest_cycle': {
                'status': 'COMPLETE', 'regime': 'BULL', 'raw_regime': 'BULL',
                'bull_breadth_pct': 75.0, 'bear_breadth_pct': 5.0,
            },
        }), encoding='utf-8')
        return {'V38_DB_PATH': str(v38), 'V37_REPORT_PATH': str(v37_report)}

    def test_shortlist_has_hard_score_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.sources(root)
            with patch.dict(os.environ, env):
                scan_ms, shortlist = v39._discovery_shortlist(NOW_MS)
            self.assertEqual(scan_ms, NOW_MS)
            self.assertEqual([item['market'] for item in shortlist], ['VET-EUR'])
            self.assertEqual(shortlist[0]['rank'], 1)

    def test_full_pipeline_reaches_l2_and_measures_fixed_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.settings(root)
            env = self.sources(root)
            api = PublicApi(NOW_MS)
            with patch.dict(os.environ, env):
                v39.ensure_v39(settings, NOW_MS)
                cycle = v39.evaluate_discovery_cycle(settings, api=api, now_ms=NOW_MS)
                self.assertEqual(cycle['shortlist_count'], 1)
                self.assertEqual(cycle['selected_candidates'], 1)

                api.now_ms = NOW_MS + 30_000
                recheck_candidates(settings, api=api, now_ms=api.now_ms)
                api.now_ms = NOW_MS + 60_000
                observed = recheck_candidates(settings, api=api, now_ms=api.now_ms)
                self.assertEqual(observed['observed'], 1)
                v39.monitor_outcomes(settings, api=api, now_ms=api.now_ms)

                api.now_ms = NOW_MS + 241 * 60_000
                outcomes = v39.monitor_outcomes(settings, api=api, now_ms=api.now_ms)
                self.assertEqual(outcomes['measured'], 3)
                report = v39.build_report(settings, api.now_ms)

            self.assertEqual(report['component'], 'FULL_EUR_HUMAN_PIPELINE_V39')
            self.assertFalse(report['safety']['execution_enabled'])
            self.assertEqual(report['prospective_results']['qualified_opportunities'], 1)
            self.assertEqual(report['prospective_results']['outcomes']['240']['samples'], 1)
            self.assertGreater(
                report['prospective_results']['outcomes']['240']['average_net_pct'], 0
            )
            with sqlite3.connect(settings.db_path) as conn:
                tables = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
            self.assertFalse(any('position' in name or 'order' in name for name in tables))

    def test_fixed_decision_rejects_without_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.settings(root)
            v39.ensure_v39(settings, NOW_MS)
            report = v39.build_report(
                settings, NOW_MS + (v39.COLLECTION_HOURS + v39.SETTLEMENT_HOURS) * v39.HOUR_MS
            )
            self.assertEqual(
                report['fixed_evaluation']['decision'], 'AFWIJZEN_GEEN_VERLENGING'
            )
            self.assertFalse(report['fixed_evaluation']['extension_allowed'])
            output = StringIO()
            with redirect_stdout(output):
                v39.print_status(report)
            self.assertIn('geen verlenging', output.getvalue())


if __name__ == '__main__':
    unittest.main()
