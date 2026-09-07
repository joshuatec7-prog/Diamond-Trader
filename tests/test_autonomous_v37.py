import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path

import autonomous_v37 as v37
from models import Candle


NOW_MS = 300_000_000


def strong_candles(now_ms: int, interval_ms: int) -> list[Candle]:
    latest = now_ms // interval_ms * interval_ms - interval_ms
    timestamps = [latest - index * interval_ms for index in reversed(range(60))]
    wave = [0.0, 0.45, -0.35, 0.30, -0.20]
    rows: list[Candle] = []
    for index, timestamp in enumerate(timestamps):
        close = 100.0 + index * 0.18 + wave[(index + 2) % len(wave)]
        rows.append(Candle(
            timestamp_ms=timestamp,
            open=close - 0.12,
            high=close + 0.80,
            low=close - 0.80,
            close=close,
            volume=160.0 if index == len(timestamps) - 1 else 100.0,
        ))
    return rows


class StrongPublicApi:
    def __init__(self, now_ms: int, markets: list[str] | None = None) -> None:
        self.now_ms = now_ms
        self.markets = markets or ['RAY-EUR', 'SOL-EUR']
        self.depth_calls = 0

    def top_markets_by_quote_volume(self, quote: str, limit: int) -> list[str]:
        self.assert_eur = quote
        return self.markets[:limit]

    def closed_candles(self, market: str, interval: str, limit: int, now_ms=None):
        del market
        interval_ms = {'5m': 300_000, '15m': 900_000, '1h': 3_600_000}[interval]
        return strong_candles(int(now_ms or self.now_ms), interval_ms)[-limit:]

    def depth_book(self, market: str, notional: float) -> dict:
        del market
        self.depth_calls += 1
        return {
            'captured_at_ms': self.now_ms,
            'buy_vwap': 100.00 + self.depth_calls * 0.001,
            'sell_vwap': 99.94 + self.depth_calls * 0.001,
            'execution_spread_pct': 0.06,
            'near_book_imbalance': 0.18,
            'notional_quote': notional,
        }


class AutonomousV37Tests(unittest.TestCase):
    def settings(self, folder: str, **changes) -> v37.V37Settings:
        base = v37.V37Settings(
            db_path=str(Path(folder) / 'v37.db'),
            report_path=str(Path(folder) / 'v37.json'),
            universe_size=2,
            maximum_l2_candidates=2,
        )
        return replace(base, **changes)

    def test_non_observe_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'uitsluitend OBSERVE_ONLY'):
                self.settings(tmp, mode='PAPER').validate()

    def test_one_missing_higher_timeframe_candle_is_activity_warning_not_feed_failure(self):
        rows = strong_candles(NOW_MS, 900_000)
        del rows[20]
        quality, features = v37._quality_and_features(
            rows,
            interval_ms=900_000,
            now_ms=NOW_MS,
            allow_one_small_gap=True,
            interval_label='15m',
        )
        self.assertTrue(quality['valid'])
        self.assertEqual(quality['status'], 'WARN')
        self.assertTrue(quality['market_activity_warning'])
        self.assertTrue(features['valid'])

    def test_separate_observer_collects_three_l2_samples_without_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(tmp)
            api = StrongPublicApi(NOW_MS)
            v37.ensure_observer(settings, NOW_MS)
            first = v37.evaluate_new_five_minute_cycle(
                settings, api=api, now_ms=NOW_MS
            )
            self.assertTrue(first['evaluated'])
            self.assertEqual(first['selected_candidates'], 2)

            api.now_ms = NOW_MS + 30_000
            second = v37.recheck_candidates(settings, api=api, now_ms=api.now_ms)
            self.assertEqual(second['observed'], 0)
            api.now_ms = NOW_MS + 60_000
            third = v37.recheck_candidates(settings, api=api, now_ms=api.now_ms)
            self.assertEqual(third['observed'], 2, third)

            report = v37.build_report(settings, now_ms=api.now_ms)
            self.assertEqual(report['decisions']['shadow_opportunities_24h'], 2)
            self.assertEqual(report['capital_model']['paper_positions_opened'], 0)
            self.assertFalse(report['safety']['execution_enabled'])
            self.assertEqual(report['l2_research']['snapshots_24h'], 6)

            conn = sqlite3.connect(settings.db_path)
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            statuses = {
                row[0] for row in conn.execute('SELECT status FROM v37_candidates')
            }
            conn.close()
            self.assertEqual(statuses, {'OBSERVED'})
            self.assertFalse(any('position' in name or 'order' in name for name in tables))

    def test_cycle_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(tmp)
            api = StrongPublicApi(NOW_MS)
            v37.ensure_observer(settings, NOW_MS)
            first = v37.evaluate_new_five_minute_cycle(settings, api=api, now_ms=NOW_MS)
            second = v37.evaluate_new_five_minute_cycle(settings, api=api, now_ms=NOW_MS)
            self.assertTrue(first['evaluated'])
            self.assertFalse(second['evaluated'])
            conn = sqlite3.connect(settings.db_path)
            self.assertEqual(
                conn.execute('SELECT COUNT(*) FROM v37_cycles').fetchone()[0], 1
            )
            conn.close()

    def test_only_five_best_candidates_receive_l2_measurements(self):
        with tempfile.TemporaryDirectory() as tmp:
            markets = [f'COIN{index}-EUR' for index in range(6)]
            settings = self.settings(
                tmp, universe_size=6, maximum_l2_candidates=5
            )
            api = StrongPublicApi(NOW_MS, markets)
            v37.ensure_observer(settings, NOW_MS)
            result = v37.evaluate_new_five_minute_cycle(
                settings, api=api, now_ms=NOW_MS
            )
            self.assertEqual(result['selected_candidates'], 5)
            conn = sqlite3.connect(settings.db_path)
            candidate_count = conn.execute(
                'SELECT COUNT(*) FROM v37_candidates'
            ).fetchone()[0]
            blocked = conn.execute(
                "SELECT blockers_json FROM v37_decisions WHERE market='COIN5-EUR'"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(candidate_count, 5)
            self.assertIn('buiten_l2_selectie_top5', json.loads(blocked))

    def test_report_write_and_status_expose_observe_only_safety(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(tmp)
            v37.ensure_observer(settings, NOW_MS)
            report = v37.build_report(settings, NOW_MS)
            v37.write_report(settings, report)
            stored = json.loads(Path(settings.report_path).read_text(encoding='utf-8'))
            self.assertEqual(stored['modes']['paper_execution'], 'UIT')
            self.assertEqual(stored['modes']['live_orders'], 'UIT / TECHNISCH ONMOGELIJK')
            self.assertFalse(stored['observation_validation']['automatic_paper_activation'])
            self.assertFalse(stored['observation_validation']['ready_for_manual_review'])
            output = StringIO()
            with redirect_stdout(output):
                v37.print_status(stored)
            text = output.getvalue()
            self.assertIn('MENSELIJKE OBSERVATIELAAG', text)
            self.assertIn('observe-only kan niets uitvoeren', text)
            self.assertIn('start €3000.00', text)


if __name__ == '__main__':
    unittest.main()
