import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from bitvavo_public import BitvavoPublic
from crypto_scanner import _direction_score, _sideways_score
from crypto_scanner_v2 import (
    _conversion_cost_pct,
    _grade_action,
    _net_reward_risk,
    _pair_snapshot,
    _persist_signal_research,
    _practical_net_return,
    _report_is_stale,
    _roundtrip_cost_pct,
    _taker_fee_pct,
)
from models import Candle, Decision


class CryptoScannerTests(unittest.TestCase):
    @staticmethod
    def _strong_metrics() -> dict[str, float]:
        return {
            'close': 110.0, 'fast15': 106.0, 'slow15': 100.0,
            'slope15_pct': 0.40, 'fast1h': 105.0, 'slow1h': 100.0,
            'one_hour_gap_pct': 1.0, 'slope1h_pct': 0.40,
            'momentum_pct': 2.0, 'breakout_pct': 0.60,
            'breakdown_pct': 0.0, 'atr_pct': 5.0,
        }

    def test_aligned_long_scores_higher_than_misaligned(self):
        aligned = {
            'close': 110.0,
            'fast15': 106.0,
            'slow15': 100.0,
            'slope15_pct': 0.30,
            'fast1h': 105.0,
            'slow1h': 100.0,
            'one_hour_gap_pct': 0.75,
            'slope1h_pct': 0.30,
            'momentum_pct': 2.0,
            'breakout_pct': 0.50,
            'breakdown_pct': 0.0,
            'atr_pct': 1.0,
        }
        misaligned = {**aligned, 'close': 99.0, 'fast15': 99.0, 'slope15_pct': -0.10}
        good = _direction_score(aligned, 'LONG', 0.78, 0.12, 0.40)
        bad = _direction_score(misaligned, 'LONG', 0.78, 0.12, 0.40)
        self.assertGreater(good, bad)
        self.assertGreaterEqual(good, 75.0)

    def test_sideways_score_cannot_become_trade_grade(self):
        band_metrics = {
            'close': 100.0,
            'prev_close': 99.0,
            'lower_band': 99.5,
            'middle_band': 105.0,
        }
        score = _sideways_score(band_metrics, 3.0, 0.78, 0.05, 0.40)
        self.assertLessEqual(score, 70.0)
        self.assertEqual(_grade_action('SIDEWAYS', 'LONG', 99.0, 9.0, 0.01), 'GEEN TRADE')

    def test_usdc_fee_is_materially_lower_than_eur(self):
        settings = SimpleNamespace(slippage_pct=0.08)
        eur = _roundtrip_cost_pct(settings, 0.12, 'EUR')
        usdc = _roundtrip_cost_pct(settings, 0.12, 'USDC')
        self.assertEqual(_taker_fee_pct('EUR'), 0.25)
        self.assertEqual(_taker_fee_pct('USDC'), 0.05)
        self.assertAlmostEqual(eur, 0.78)
        self.assertAlmostEqual(usdc, 0.38)
        self.assertLess(usdc, eur)

    def test_trade_grade_requires_three_times_cost_room(self):
        self.assertEqual(_grade_action('BULL', 'LONG', 85.0, 3.2, 0.10), 'LONG TRADE-GRADE')
        self.assertEqual(_grade_action('BEAR', 'SHORT', 85.0, 3.2, 0.10), 'SHORT TRADE-GRADE')
        self.assertEqual(_grade_action('BULL', 'LONG', 85.0, 2.9, 0.10), 'LONG WATCH')
        self.assertNotIn('TRADE-GRADE', _grade_action('BULL', 'SKIP', 95.0, 5.0, 0.01))

    def test_watch_also_requires_strategy_direction(self):
        self.assertEqual(_grade_action('BULL', 'SKIP', 75.0, 4.0, 0.01), 'GEEN TRADE')

    def test_trade_grade_requires_current_price_in_zone_and_net_rr(self):
        self.assertEqual(
            _grade_action(
                'BULL', 'LONG', 90.0, 5.0, 0.05,
                price_in_zone=False, net_reward_risk=3.0,
            ),
            'LONG WATCH',
        )
        self.assertEqual(
            _grade_action(
                'BULL', 'LONG', 90.0, 5.0, 0.05,
                price_in_zone=True, net_reward_risk=1.49,
            ),
            'LONG WATCH',
        )
        self.assertEqual(
            _grade_action(
                'BULL', 'LONG', 90.0, 5.0, 0.05,
                price_in_zone=True, net_reward_risk=1.50,
            ),
            'LONG TRADE-GRADE',
        )

    def test_pair_snapshot_uses_executable_l2_price_for_zone(self):
        class Api:
            buy_vwap = 110.05

            def depth_book(self, market, notional):
                return {
                    'bid': 109.95, 'ask': 110.05, 'spread_pct': 0.10,
                    'sell_vwap': 109.95, 'buy_vwap': self.buy_vwap,
                    'execution_spread_pct': (self.buy_vwap / 109.95 - 1) * 100,
                    'bid_depth_quote': 10_000.0, 'ask_depth_quote': 10_000.0,
                }

        api = Api()
        settings = SimpleNamespace(
            position_eur=200.0, slippage_pct=0.08, max_spread_pct=0.40, interval='15m'
        )
        candles = [Candle(1_000, 110.0, 111.0, 109.0, 110.0, 10.0)]
        directional = SimpleNamespace(
            evaluate_metrics=lambda *args: Decision('LONG', 'test', {})
        )
        row = _pair_snapshot(
            api=api, settings=settings, analyzer=SimpleNamespace(), directional=directional,
            band=SimpleNamespace(), market='AAA-EUR', regime='BULL', bull_breadth=1.0,
            bear_breadth=0.0, candle_limit=240, cached_candles=candles,
            cached_metrics=self._strong_metrics(),
        )
        self.assertTrue(row['price_in_zone'])
        self.assertGreaterEqual(row['net_reward_risk'], 1.5)
        self.assertEqual(row['action'], 'LONG TRADE-GRADE')

        api.buy_vwap = 112.0
        row = _pair_snapshot(
            api=api, settings=settings, analyzer=SimpleNamespace(), directional=directional,
            band=SimpleNamespace(), market='AAA-EUR', regime='BULL', bull_breadth=1.0,
            bear_breadth=0.0, candle_limit=240, cached_candles=candles,
            cached_metrics=self._strong_metrics(),
        )
        self.assertFalse(row['price_in_zone'])
        self.assertEqual(row['action'], 'LONG WATCH')

    def test_usdc_conversion_cost_is_included(self):
        settings = SimpleNamespace(slippage_pct=0.08)
        conversion = {'execution_spread_pct': 0.02}
        extra = _conversion_cost_pct(settings, conversion)
        self.assertAlmostEqual(extra, 0.38)
        total = _roundtrip_cost_pct(settings, 0.12, 'USDC', extra)
        self.assertAlmostEqual(total, 0.76)

    def test_net_reward_risk_subtracts_all_costs(self):
        reward, risk, ratio = _net_reward_risk(
            entry=100.0, stop=98.0, target=105.0, side='LONG', roundtrip_cost_pct=1.0
        )
        self.assertAlmostEqual(reward, 4.0)
        self.assertAlmostEqual(risk, 3.0)
        self.assertAlmostEqual(ratio, 4.0 / 3.0)

    def test_depth_vwap_uses_multiple_order_book_levels(self):
        vwap, depth = BitvavoPublic._depth_vwap([(101.0, 1.0), (102.0, 2.0)], 200.0)
        self.assertAlmostEqual(vwap, 200.0 / (1.0 + 99.0 / 102.0))
        self.assertAlmostEqual(depth, 305.0)
        with self.assertRaisesRegex(RuntimeError, 'onvoldoende orderboekdiepte'):
            BitvavoPublic._depth_vwap([(100.0, 0.5)], 200.0)

    def test_old_report_is_explicitly_stale(self):
        report = {'generated_at_ms': 1_000}
        self.assertTrue(_report_is_stale(report, now_ms=4_000_000))
        self.assertFalse(_report_is_stale(report, now_ms=2_000))

    def test_rare_signal_is_persisted_and_evaluated_prospectively(self):
        signal = {
            'market': 'AAA-EUR', 'action': 'LONG TRADE-GRADE', 'side': 'LONG',
            'score': 90.0, 'executable_entry': 100.0, 'roundtrip_cost_pct': 1.0,
            'net_reward_risk': 1.5, 'price_in_zone': True, 'latest_candle_ms': 1_000,
            'latest_candle_high': 101.0, 'latest_candle_low': 99.0,
            'latest_candle_close': 100.0, 'stop_hint': 98.0, 'target_hint': 105.0,
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {'SCANNER_V3_DB_PATH': os.path.join(tmp, 'scanner.db')}, clear=False
        ):
            first = {
                'generated_at_ms': 2_000, 'candidates': [signal],
                'all_pair_snapshots': [signal], 'rare_opportunities': [signal],
            }
            stats = _persist_signal_research(first)
            self.assertEqual(stats['signals_open'], 1)
            self.assertEqual(stats['audit_candidates'], 1)
            self.assertEqual(stats['audit_rare_moments'], 1)
            duplicate = {**first, 'generated_at_ms': 3_000}
            stats = _persist_signal_research(duplicate)
            self.assertEqual(stats['signals_total'], 1)

            next_row = {
                **signal, 'latest_candle_ms': 901_000, 'latest_candle_high': 101.0,
                'latest_candle_low': 99.0, 'latest_candle_close': 105.0,
                'action': 'GEEN TRADE',
                '_outcome_candles': [
                    [451_000, 106.0, 99.0, 105.0],
                    [901_000, 101.0, 99.0, 100.0],
                ],
            }
            second = {
                'generated_at_ms': 902_000, 'candidates': [next_row],
                'all_pair_snapshots': [next_row], 'rare_opportunities': [],
            }
            stats = _persist_signal_research(second)
            self.assertEqual(stats['signals_open'], 0)
            self.assertEqual(stats['signals_closed'], 1)
            self.assertEqual(stats['wins'], 1)

    def test_candidate_audit_reports_watches_and_overlapping_blockers(self):
        watch = {
            'market': 'WATCH-EUR', 'action': 'LONG WATCH', 'side': 'LONG',
            'decision_action': 'LONG', 'decision_reason': 'test_long',
            'score': 75.0, 'executable_entry': 101.0, 'roundtrip_cost_pct': 0.8,
            'cost_multiple': 4.0, 'execution_spread_pct': 0.05,
            'net_reward_risk': 2.0, 'price_in_zone': False,
            'reasons': ['actuele uitvoerprijs buiten besliszone'],
        }
        rare = {
            **watch, 'market': 'RARE-EUR', 'action': 'LONG TRADE-GRADE',
            'score': 90.0, 'price_in_zone': True,
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {'SCANNER_V3_DB_PATH': os.path.join(tmp, 'scanner.db')}, clear=False
        ):
            stats = _persist_signal_research({
                'generated_at_ms': 10_000,
                'regime': 'BULL',
                'candidates': [watch, rare],
                'all_pair_snapshots': [],
                'rare_opportunities': [],
            })
            self.assertEqual(stats['audit_cycles'], 1)
            self.assertEqual(stats['audit_candidates'], 2)
            self.assertEqual(stats['audit_watch_moments'], 1)
            self.assertEqual(stats['audit_rare_moments'], 1)
            self.assertEqual(stats['audit_detailed_candidates'], 2)
            self.assertEqual(stats['audit_blockers_overlap']['score'], 1)
            self.assertEqual(stats['audit_blockers_overlap']['price_zone'], 1)
            self.assertEqual(stats['audit_blockers_overlap']['strategy_direction'], 0)

    def test_candidate_audit_migrates_existing_snapshot_table(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {'SCANNER_V3_DB_PATH': os.path.join(tmp, 'scanner.db')}, clear=False
        ):
            db_path = os.environ['SCANNER_V3_DB_PATH']
            conn = sqlite3.connect(db_path)
            conn.execute(
                '''CREATE TABLE snapshots (
                    generated_ms INTEGER NOT NULL,
                    market TEXT NOT NULL,
                    action TEXT NOT NULL,
                    score REAL NOT NULL,
                    executable_entry REAL NOT NULL,
                    roundtrip_cost_pct REAL NOT NULL,
                    net_reward_risk REAL NOT NULL,
                    price_in_zone INTEGER NOT NULL,
                    PRIMARY KEY (generated_ms, market)
                )'''
            )
            conn.execute(
                '''INSERT INTO snapshots VALUES
                   (1000,'OLD-EUR','LONG WATCH',70,100,0.8,1.4,0)'''
            )
            conn.commit()
            conn.close()

            stats = _persist_signal_research({
                'generated_at_ms': 2_000,
                'regime': 'SIDEWAYS',
                'candidates': [],
                'all_pair_snapshots': [],
                'rare_opportunities': [],
            })
            self.assertEqual(stats['audit_candidates'], 1)
            self.assertEqual(stats['audit_watch_moments'], 1)
            self.assertEqual(stats['audit_detailed_candidates'], 0)

            conn = sqlite3.connect(db_path)
            columns = {row[1] for row in conn.execute('PRAGMA table_info(snapshots)')}
            conn.close()
            self.assertIn('decision_reason', columns)
            self.assertIn('cost_multiple', columns)
            self.assertIn('reasons_json', columns)

    def test_practical_watch_tracker_uses_l2_prices_and_trailing_profit(self):
        watch = {
            'market': 'WATCH-EUR', 'action': 'LONG WATCH', 'side': 'LONG',
            'decision_action': 'LONG', 'decision_reason': 'test_long',
            'score': 75.0, 'executable_entry': 100.0,
            'buy_vwap': 100.0, 'sell_vwap': 99.9,
            'roundtrip_cost_pct': 0.76, 'execution_spread_pct': 0.10,
            'cost_multiple': 4.0, 'net_reward_risk': 1.2,
            'price_in_zone': False, 'reasons': [],
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {'SCANNER_V3_DB_PATH': os.path.join(tmp, 'scanner.db')}, clear=False
        ):
            first = {
                'generated_at_ms': 1_000,
                'regime': 'BULL',
                'rules': {'shadow_notional_eur': 200.0},
                'candidates': [watch],
                'all_pair_snapshots': [watch],
                'rare_opportunities': [],
            }
            stats = _persist_signal_research(first)
            self.assertEqual(stats['practical_total'], 1)
            self.assertEqual(stats['practical_open'], 1)

            rising = {**watch, 'action': 'GEEN TRADE', 'sell_vwap': 102.0}
            stats = _persist_signal_research({
                **first,
                'generated_at_ms': 901_000,
                'candidates': [rising],
                'all_pair_snapshots': [rising],
            })
            self.assertEqual(stats['practical_open'], 1)

            pullback = {**rising, 'sell_vwap': 100.99}
            stats = _persist_signal_research({
                **first,
                'generated_at_ms': 1_801_000,
                'candidates': [pullback],
                'all_pair_snapshots': [pullback],
            })
            self.assertEqual(stats['practical_open'], 0)
            self.assertEqual(stats['practical_closed'], 1)
            self.assertEqual(stats['practical_wins'], 1)
            self.assertEqual(stats['practical_outcomes']['trail'], 1)
            self.assertGreater(stats['practical_pnl_eur'], 0.0)

            conn = sqlite3.connect(os.environ['SCANNER_V3_DB_PATH'])
            outcome, net_return = conn.execute(
                'SELECT outcome,net_return_pct FROM practical_signals'
            ).fetchone()
            conn.close()
            self.assertEqual(outcome, 'TRAIL')
            self.assertAlmostEqual(net_return, _practical_net_return(100.0, 100.99, 0.66))

    def test_practical_watch_tracker_opens_only_one_route_per_asset(self):
        eur = {
            'market': 'AAA-EUR', 'base': 'AAA', 'action': 'LONG WATCH', 'side': 'LONG',
            'buy_vwap': 100.0, 'sell_vwap': 99.9, 'roundtrip_cost_pct': 0.76,
            'execution_spread_pct': 0.10, 'score': 75.0,
            'net_reward_risk': 1.2, 'price_in_zone': False,
        }
        usdc = {**eur, 'market': 'AAA-USDC', 'buy_vwap': 100.1}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {'SCANNER_V3_DB_PATH': os.path.join(tmp, 'scanner.db')}, clear=False
        ):
            stats = _persist_signal_research({
                'generated_at_ms': 1_000, 'regime': 'BULL',
                'candidates': [eur, usdc], 'all_pair_snapshots': [eur, usdc],
                'rare_opportunities': [],
            })
            self.assertEqual(stats['practical_total'], 1)
            self.assertEqual(stats['practical_open'], 1)

    def test_practical_watch_tracker_does_not_open_short_watch(self):
        short_watch = {
            'market': 'SHORT-EUR', 'action': 'SHORT WATCH', 'side': 'SHORT',
            'score': 75.0, 'executable_entry': 100.0,
            'buy_vwap': 100.1, 'sell_vwap': 100.0,
            'roundtrip_cost_pct': 0.76, 'execution_spread_pct': 0.10,
            'net_reward_risk': 2.0, 'price_in_zone': True,
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {'SCANNER_V3_DB_PATH': os.path.join(tmp, 'scanner.db')}, clear=False
        ):
            stats = _persist_signal_research({
                'generated_at_ms': 1_000, 'regime': 'BEAR',
                'candidates': [short_watch], 'all_pair_snapshots': [short_watch],
                'rare_opportunities': [],
            })
            self.assertEqual(stats['practical_total'], 0)


if __name__ == '__main__':
    unittest.main()
