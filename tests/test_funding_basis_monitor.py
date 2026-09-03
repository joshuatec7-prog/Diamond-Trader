import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from funding_basis_monitor import (
    NATIVE_EXISTING_HOLDING_BUFFER_PCT,
    TOTAL_ROUNDTRIP_BUFFER_PCT,
    _build_row,
    _order_book_metrics,
    _relative_funding_pct,
    _report_is_stale,
    scan_once,
    _score_candidate,
    _stress_metrics,
    _window_history,
    _future_record,
)


class FundingBasisMonitorTests(unittest.TestCase):
    def test_old_report_is_explicitly_stale(self):
        self.assertTrue(_report_is_stale({'generated_at_ms': 1_000}, now_ms=4_000_000))
        self.assertFalse(_report_is_stale({'generated_at_ms': 1_000}, now_ms=2_000))

    def test_roundtrip_buffer_includes_both_legs(self):
        self.assertAlmostEqual(TOTAL_ROUNDTRIP_BUFFER_PCT, 0.35)

    def test_existing_kraken_holding_uses_only_hedge_buffer(self):
        self.assertAlmostEqual(NATIVE_EXISTING_HOLDING_BUFFER_PCT, 0.20)
        self.assertLess(NATIVE_EXISTING_HOLDING_BUFFER_PCT, TOTAL_ROUNDTRIP_BUFFER_PCT)

    def test_absolute_kraken_rate_is_normalized_by_index(self):
        # $0.25 funding per BTC contract on $50k index = 0.0005% per hour.
        self.assertAlmostEqual(_relative_funding_pct(0.25, 50_000.0), 0.0005)

    def test_valid_perpetual_becomes_candidate_record(self):
        record = _future_record({
            'symbol': 'PF_XBTUSD',
            'tag': 'perpetual',
            'pair': 'XBT:USD',
            'markPrice': 50_010.0,
            'indexPrice': 50_000.0,
            'bid': 50_005.0,
            'ask': 50_015.0,
            'fundingRate': 0.25,
            'fundingRatePrediction': 0.20,
            'volumeQuote': 10_000_000,
            'openInterest': 100.0,
            'suspended': False,
        })
        self.assertIsNotNone(record)
        self.assertEqual(record['base'], 'BTC')

    def test_instrument_validation_fails_closed_on_unknown_contract_size(self):
        ticker = {
            'symbol': 'PF_XBTUSD',
            'tag': 'perpetual',
            'pair': 'XBT:USD',
            'markPrice': 50_010.0,
            'indexPrice': 50_000.0,
            'bid': 50_005.0,
            'ask': 50_015.0,
            'fundingRate': 0.25,
            'fundingRatePrediction': 0.20,
            'suspended': False,
        }
        instrument = {
            'symbol': 'PF_XBTUSD',
            'type': 'flexible_futures',
            'tradeable': True,
            'isExpired': False,
            'contractSize': 2,
        }
        self.assertIsNone(_future_record(ticker, instrument))

    def test_order_book_vwap_consumes_multiple_levels(self):
        metrics = _order_book_metrics(
            bids=[[100, 1], [99, 2]],
            asks=[[101, 1], [102, 2]],
            notional_quote=200,
        )
        self.assertAlmostEqual(metrics['sell_vwap'], 200 / (1 + 100 / 99))
        self.assertAlmostEqual(metrics['buy_vwap'], 200 / (1 + 99 / 102))
        self.assertGreater(metrics['execution_spread_pct'], metrics['spread_pct'])

    def test_order_book_with_insufficient_depth_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'onvoldoende orderboekdiepte'):
            _order_book_metrics(
                bids=[[100, 0.5]],
                asks=[[101, 10]],
                notional_quote=200,
            )

    def test_measurement_rejects_books_more_than_thirty_seconds_apart(self):
        conn = sqlite3.connect(':memory:')
        conn.execute(
            '''CREATE TABLE snapshots (
                generated_ms INTEGER, route_id TEXT, funding_hour_pct REAL, basis_pct REAL,
                roundtrip_buffer_pct REAL
            )'''
        )
        spot_book = _order_book_metrics([[99, 10]], [[101, 10]], notional_quote=200)
        futures_book = _order_book_metrics([[100, 10]], [[101, 10]], notional_quote=200)
        spot_book['captured_at_ms'] = 1_000
        futures_book['captured_at_ms'] = 31_001
        with self.assertRaisesRegex(RuntimeError, 'orderboeken liggen'):
            _build_row(
                future={
                    'symbol': 'PF_TESTUSD', 'pair': 'TEST:USD', 'base': 'TEST',
                    'mark': 100.0, 'index': 100.0, 'funding_hour_pct': 0.01,
                    'predicted_hour_pct': 0.01, 'futures_spread_pct': 0.01,
                    'volume_quote': 1_000_000.0, 'open_interest': 100.0,
                },
                route_id='KRAKEN_EXISTING_EXEC_V4_TEST',
                route_type='KRAKEN_EXISTING_HOLDING',
                spot_market='KRAKEN:TESTUSD',
                spot_book=spot_book,
                futures_book=futures_book,
                fixed_roundtrip_buffer_pct=NATIVE_EXISTING_HOLDING_BUFFER_PCT,
                watch_enabled=True,
                conn=conn,
                now_ms=1_000,
            )
        conn.close()

    def test_native_existing_holding_has_better_net_snapshot_at_same_funding(self):
        history = {
            'samples_24h': 10.0,
            'span_hours_24h': 2.0,
            'positive_share_24h': 1.0,
            'avg_funding_hour_pct_24h': 0.002,
        }
        _, _, cross_net = _score_candidate(
            funding_hour_pct=0.002,
            predicted_hour_pct=0.002,
            spread_pct=0.01,
            volume_quote=50_000_000,
            basis_pct=0.01,
            history=history,
        )
        _, _, native_net = _score_candidate(
            funding_hour_pct=0.002,
            predicted_hour_pct=0.002,
            spread_pct=0.01,
            volume_quote=50_000_000,
            basis_pct=0.01,
            history=history,
            roundtrip_buffer_pct=NATIVE_EXISTING_HOLDING_BUFFER_PCT,
        )
        self.assertGreater(native_net, cross_net)
        self.assertAlmostEqual(native_net - cross_net, 0.15)

    def test_monitor_collects_before_watch_label(self):
        score, action, net = _score_candidate(
            funding_hour_pct=0.010,
            predicted_hour_pct=0.009,
            spread_pct=0.03,
            volume_quote=50_000_000,
            basis_pct=0.10,
            history={
                'samples_24h': 10.0,
                'span_hours_24h': 2.0,
                'positive_share_24h': 1.0,
                'avg_funding_hour_pct_24h': 0.010,
            },
        )
        self.assertGreater(score, 0.0)
        self.assertGreater(net, 1.0)
        self.assertEqual(action, 'VERZAMELEN')

    def test_persistent_positive_funding_can_become_strong_watch(self):
        _, action, net = _score_candidate(
            funding_hour_pct=0.010,
            predicted_hour_pct=0.009,
            spread_pct=0.03,
            volume_quote=50_000_000,
            basis_pct=0.10,
            history={
                'samples_72h': 288.0,
                'span_hours_72h': 71.75,
                'positive_share_72h': 0.85,
                'avg_funding_hour_pct_72h': 0.009,
                'funding_decay_ratio_24h_vs_72h': 0.90,
                'max_gap_minutes_72h': 15.0,
                'avg_roundtrip_buffer_pct_72h': 0.35,
                'max_roundtrip_buffer_pct_72h': 0.35,
            },
        )
        self.assertGreaterEqual(net, 1.0)
        self.assertEqual(action, 'STERKE CARRY WATCH')

    def test_cross_exchange_watch_is_hard_blocked_even_with_strong_history(self):
        _, action, _ = _score_candidate(
            funding_hour_pct=0.010,
            predicted_hour_pct=0.009,
            spread_pct=0.03,
            volume_quote=50_000_000,
            basis_pct=0.10,
            history={
                'samples_72h': 288.0,
                'span_hours_72h': 71.75,
                'positive_share_72h': 0.90,
                'avg_funding_hour_pct_72h': 0.010,
                'funding_decay_ratio_24h_vs_72h': 1.0,
                'max_gap_minutes_72h': 15.0,
                'avg_roundtrip_buffer_pct_72h': 0.35,
                'max_roundtrip_buffer_pct_72h': 0.35,
            },
            watch_enabled=False,
        )
        self.assertEqual(action, 'CROSS GEBLOKKEERD')

    def test_cross_basis_uses_executable_books_and_usdc_conversion(self):
        conn = sqlite3.connect(':memory:')
        conn.execute(
            '''CREATE TABLE snapshots (
                generated_ms INTEGER, route_id TEXT, funding_hour_pct REAL, basis_pct REAL,
                roundtrip_buffer_pct REAL
            )'''
        )
        future = {
            'symbol': 'PF_TESTUSD',
            'pair': 'TEST:USD',
            'base': 'TEST',
            'mark': 999.0,
            'index': 998.0,
            'funding_hour_pct': 0.01,
            'predicted_hour_pct': 0.01,
            'futures_spread_pct': 0.01,
            'volume_quote': 50_000_000.0,
            'open_interest': 1_000.0,
        }
        spot_book = _order_book_metrics([[99, 10]], [[101, 10]], notional_quote=200)
        futures_book = _order_book_metrics([[102, 10]], [[103, 10]], notional_quote=200)
        stablecoin_book = _order_book_metrics([[0.999, 1000]], [[1.001, 1000]], notional_quote=200)
        row = _build_row(
            future=future,
            route_id='BITVAVO_USDC_EXEC_V4_TEST',
            route_type='BITVAVO_USDC_KRAKEN_PERP',
            spot_market='TEST-USDC',
            spot_book=spot_book,
            futures_book=futures_book,
            stablecoin_book=stablecoin_book,
            fixed_roundtrip_buffer_pct=TOTAL_ROUNDTRIP_BUFFER_PCT,
            watch_enabled=False,
            conn=conn,
            now_ms=1_000,
        )
        conn.close()
        expected_spot_entry = spot_book['buy_vwap'] * stablecoin_book['buy_vwap']
        expected_basis = (futures_book['sell_vwap'] / expected_spot_entry - 1.0) * 100.0
        self.assertAlmostEqual(row['spot_reference'], expected_spot_entry, places=8)
        self.assertAlmostEqual(row['entry_basis_pct'], expected_basis, places=5)
        self.assertNotAlmostEqual(row['spot_reference'], future['index'])
        self.assertGreater(row['roundtrip_buffer_pct'], TOTAL_ROUNDTRIP_BUFFER_PCT)
        self.assertEqual(row['action'], 'CROSS GEBLOKKEERD')

    def test_scan_persists_v4_measurements_and_never_labels_cross_route(self):
        ticker = {
            'symbol': 'PF_XBTUSD',
            'tag': 'perpetual',
            'pair': 'XBT:USD',
            'markPrice': 100.0,
            'indexPrice': 100.0,
            'bid': 99.9,
            'ask': 100.1,
            'fundingRate': 0.01,
            'fundingRatePrediction': 0.01,
            'volumeQuote': 50_000_000,
            'openInterest': 100.0,
            'suspended': False,
        }
        instrument = {
            'symbol': 'PF_XBTUSD',
            'type': 'flexible_futures',
            'tradeable': True,
            'isExpired': False,
            'contractSize': 1,
        }
        spot_book = _order_book_metrics([[99, 10]], [[101, 10]], notional_quote=200)
        futures_book = _order_book_metrics([[100, 10]], [[100.2, 10]], notional_quote=200)
        stablecoin_book = _order_book_metrics([[0.999, 1000]], [[1.001, 1000]], notional_quote=200)

        def kraken_spot_book(pair, notional_quote):
            self.assertEqual(notional_quote, 200)
            return stablecoin_book if pair == 'USDCUSD' else spot_book

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, 'funding.db')
            with mock.patch.dict(os.environ, {
                'FUNDING_MONITOR_DB_PATH': db_path,
                'KRAKEN_NATIVE_HOLDINGS': 'BTC',
                'FUNDING_SHADOW_NOTIONAL_USD': '200',
            }, clear=False), mock.patch.multiple(
                'funding_basis_monitor',
                _fetch_bitvavo_usdc_markets=mock.DEFAULT,
                _fetch_futures_tickers=mock.DEFAULT,
                _fetch_futures_instruments=mock.DEFAULT,
                _fetch_bitvavo_book=mock.DEFAULT,
                _fetch_kraken_futures_book=mock.DEFAULT,
                _fetch_kraken_spot_book=mock.DEFAULT,
            ) as patched:
                patched['_fetch_bitvavo_usdc_markets'].return_value = {'BTC': 'BTC-USDC'}
                patched['_fetch_futures_tickers'].return_value = [ticker]
                patched['_fetch_futures_instruments'].return_value = {'PF_XBTUSD': instrument}
                patched['_fetch_bitvavo_book'].return_value = spot_book
                patched['_fetch_kraken_futures_book'].return_value = futures_book
                patched['_fetch_kraken_spot_book'].side_effect = kraken_spot_book
                report = scan_once()

            self.assertEqual(report['version'], '4.1')
            self.assertFalse(report['cross_exchange_watch_enabled'])
            self.assertEqual(report['cross_exchange'][0]['action'], 'CROSS GEBLOKKEERD')
            self.assertTrue(report['cross_exchange'][0]['route_id'].startswith('BITVAVO_USDC_EXEC_V4_'))
            self.assertTrue(report['kraken_existing_holdings'][0]['route_id'].startswith('KRAKEN_EXISTING_EXEC_V4_'))
            with sqlite3.connect(db_path) as conn:
                stored = conn.execute(
                    'SELECT measurement_generation,measurement_valid,watch_eligible FROM snapshots '
                    'ORDER BY route_id'
                ).fetchall()
            self.assertEqual(stored, [(4, 1, 0), (4, 1, 1)])

    def test_negative_funding_is_not_short_perp_carry_watch(self):
        _, action, _ = _score_candidate(
            funding_hour_pct=-0.010,
            predicted_hour_pct=-0.005,
            spread_pct=0.02,
            volume_quote=100_000_000,
            basis_pct=0.20,
            history={
                'samples_72h': 288.0,
                'span_hours_72h': 71.75,
                'positive_share_72h': 0.10,
                'avg_funding_hour_pct_72h': -0.008,
                'funding_decay_ratio_24h_vs_72h': 0.0,
            },
        )
        self.assertEqual(action, 'VERZAMELEN')

    def test_watch_requires_full_72_hour_observation(self):
        _, action, _ = _score_candidate(
            funding_hour_pct=0.010,
            predicted_hour_pct=0.009,
            spread_pct=0.02,
            volume_quote=100_000_000,
            basis_pct=0.10,
            history={
                'samples_72h': 200.0,
                'span_hours_72h': 60.0,
                'positive_share_72h': 1.0,
                'avg_funding_hour_pct_72h': 0.010,
                'funding_decay_ratio_24h_vs_72h': 1.0,
            },
        )
        self.assertEqual(action, 'VERZAMELEN')

    def test_cost_and_basis_stress_are_conservative(self):
        stress = _stress_metrics(average_funding_hour_pct=0.005, roundtrip_buffer_pct=0.35)
        self.assertAlmostEqual(stress['gross_7d_historical_pct'], 0.84)
        self.assertAlmostEqual(stress['net_7d_historical_pct'], 0.49)
        self.assertAlmostEqual(stress['net_7d_cost_stress_2x_pct'], 0.14)
        self.assertAlmostEqual(stress['net_7d_basis_shock_1pct_pct'], -0.51)

    def test_watch_is_blocked_when_basis_shock_is_negative(self):
        _, action, _ = _score_candidate(
            funding_hour_pct=0.006,
            predicted_hour_pct=0.006,
            spread_pct=0.02,
            volume_quote=100_000_000,
            basis_pct=0.10,
            history={
                'samples_72h': 288.0,
                'span_hours_72h': 71.75,
                'positive_share_72h': 0.90,
                'avg_funding_hour_pct_72h': 0.006,
                'funding_decay_ratio_24h_vs_72h': 1.0,
                'max_gap_minutes_72h': 15.0,
                'avg_roundtrip_buffer_pct_72h': 0.35,
                'max_roundtrip_buffer_pct_72h': 0.35,
            },
        )
        self.assertEqual(action, 'VERZAMELEN')

    def test_watch_is_blocked_by_large_measurement_gap(self):
        _, action, _ = _score_candidate(
            funding_hour_pct=0.010,
            predicted_hour_pct=0.009,
            spread_pct=0.02,
            volume_quote=100_000_000,
            basis_pct=0.10,
            history={
                'samples_72h': 288.0,
                'span_hours_72h': 71.75,
                'positive_share_72h': 0.90,
                'avg_funding_hour_pct_72h': 0.010,
                'funding_decay_ratio_24h_vs_72h': 1.0,
                'max_gap_minutes_72h': 45.0,
                'avg_roundtrip_buffer_pct_72h': 0.35,
                'max_roundtrip_buffer_pct_72h': 0.35,
            },
        )
        self.assertEqual(action, 'VERZAMELEN')

    def test_worst_historical_cost_is_used_for_stress(self):
        stress = _stress_metrics(
            average_funding_hour_pct=0.010,
            roundtrip_buffer_pct=0.35,
            stress_buffer_pct=0.80,
        )
        self.assertAlmostEqual(stress['net_7d_historical_pct'], 1.33)
        self.assertAlmostEqual(stress['net_7d_cost_stress_2x_pct'], 0.08)
        self.assertAlmostEqual(stress['net_7d_basis_shock_1pct_pct'], -0.12)

    def test_history_window_counts_sign_flips_and_basis_range(self):
        rows = [
            (0, 0.001, -0.10, 0.30),
            (1_000, -0.001, 0.20, 0.50),
            (2_000, 0.002, 0.05, 0.40),
        ]
        summary = _window_history(rows, 0, 'test')
        self.assertEqual(summary['samples_test'], 3.0)
        self.assertEqual(summary['sign_flips_test'], 2.0)
        self.assertAlmostEqual(summary['positive_share_test'], 2 / 3)
        self.assertEqual(summary['min_basis_pct_test'], -0.10)
        self.assertEqual(summary['max_basis_pct_test'], 0.20)
        self.assertAlmostEqual(summary['avg_roundtrip_buffer_pct_test'], 0.40)
        self.assertAlmostEqual(summary['max_roundtrip_buffer_pct_test'], 0.50)


if __name__ == '__main__':
    unittest.main()

