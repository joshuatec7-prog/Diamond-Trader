import unittest

from models import Candle
from v40_human_engine import (
    candle_features,
    evaluate_entry,
    evaluate_dynamic_l2_challenger,
    evaluate_exit,
    evaluate_human_challenger,
    proposed_position_eur,
    resolve_market_regime,
)
from v40_offline_scan import scan_all_eur
from v40_historical_lab import run_historical_lab
from v40_replay import (
    SIGNAL_COOLDOWN_MS,
    audit_large_moves,
    build_runner_validation,
    build_capacity_validation,
    build_capacity_diagnostics,
    simulate_capacity_limited_portfolio,
    forward_outcomes,
    rolling_quote_volume,
    rolling_quote_volume_series,
    simulate_broad_runner_trade,
    simulate_signal_trade,
    summarize_strategy_trades,
    summarize_replays,
)


def candles_from_closes(closes, *, volumes=None, start_ms=0):
    volumes = volumes or [100.0] * len(closes)
    result = []
    for index, (close, volume) in enumerate(zip(closes, volumes)):
        previous = closes[index - 1] if index else close
        opening = previous
        result.append(Candle(
            timestamp_ms=start_ms + index * 300_000,
            open=opening,
            high=max(opening, close) * 1.001,
            low=min(opening, close) * .999,
            close=close,
            volume=volume,
        ))
    return result


class V40HumanEngineTests(unittest.TestCase):
    def btc(self):
        return candles_from_closes([100.0 + index * .02 for index in range(120)])

    def test_regime_change_requires_two_confirming_scans(self):
        bullish = [
            {
                'market': 'BTC-EUR' if index == 0 else f'M{index}-EUR',
                'features': {'valid': True, 'return_60m_pct': 1.0},
            }
            for index in range(25)
        ]
        bearish = [
            {
                'market': 'BTC-EUR' if index == 0 else f'M{index}-EUR',
                'features': {'valid': True, 'return_60m_pct': -2.5},
            }
            for index in range(25)
        ]
        first = resolve_market_regime(bullish)
        transition = resolve_market_regime(bearish, previous_regime=first['stable_regime'])
        confirmed = resolve_market_regime(
            bearish,
            previous_regime=transition['stable_regime'],
            pending_regime=transition['pending_regime'],
            pending_count=transition['pending_count'],
        )
        self.assertEqual(first['regime'], 'BULL')
        self.assertEqual(transition['regime'], 'TRANSITION')
        self.assertEqual(confirmed['regime'], 'BEAR')

    def test_human_challenger_abstains_when_evidence_is_near_boundaries(self):
        decision = {
            'action': 'KOOPKANS', 'route': 'SWING_OPBOUW', 'score': 72.0,
            'net_reward_risk': 1.52, 'relative_strength_vs_btc_1h_pct': .3,
            'features': {'volume_ratio': 1.0}, 'stop_reference': 97.0,
            'target_reference': 105.0,
        }
        review = evaluate_human_challenger(
            decision, regime_state={'regime': 'SIDEWAYS'},
        )
        self.assertEqual(review['review_action'], 'AFZIEN')
        self.assertIn('meerdere_onzekere_randgevallen', review['vetoes'])
        self.assertFalse(review['active_paper_changed'])

    def test_human_challenger_accepts_strong_paper_candidate(self):
        decision = {
            'action': 'KOOPKANS', 'route': 'VROEG_MOMENTUM', 'score': 92.0,
            'net_reward_risk': 2.1, 'relative_strength_vs_btc_1h_pct': 2.0,
            'features': {'volume_ratio': 1.8}, 'stop_reference': 97.0,
            'target_reference': 110.0,
        }
        review = evaluate_human_challenger(
            decision, regime_state={'regime': 'BULL'},
        )
        self.assertEqual(review['review_action'], 'PAPER_KANDIDAAT')
        self.assertEqual(review['evidence_strength'], 'STERK')
        self.assertEqual(review['vetoes'], [])

    def test_human_challenger_pauses_after_loss_streak(self):
        decision = {
            'action': 'KOOPKANS', 'route': 'VROEG_MOMENTUM', 'score': 95.0,
            'net_reward_risk': 2.2, 'relative_strength_vs_btc_1h_pct': 2.5,
            'features': {'volume_ratio': 2.0},
        }
        review = evaluate_human_challenger(
            decision, regime_state={'regime': 'BULL'},
            consecutive_losses=3, pause_active=True, daily_realized_pnl_eur=-30.0,
        )
        self.assertEqual(review['review_action'], 'AFZIEN')
        self.assertIn('pauze_na_verliesreeks_of_dagverlies', review['vetoes'])

    def test_dynamic_l2_challenger_detects_fading_buy_pressure(self):
        review = evaluate_dynamic_l2_challenger([
            {'spread_pct': .05, 'imbalance': .40, 'buy_vwap': 100.00},
            {'spread_pct': .06, 'imbalance': .10, 'buy_vwap': 100.05},
            {'spread_pct': .08, 'imbalance': -.05, 'buy_vwap': 100.10},
        ], atr_pct=1.0)
        self.assertEqual(review['status'], 'AFZIEN')
        self.assertIn('l2_koopdruk_verzwakt_snel', review['vetoes'])
        self.assertFalse(review['active_paper_changed'])

    def test_requires_closed_history(self):
        result = candle_features(candles_from_closes([100.0] * 59))
        self.assertFalse(result['valid'])

    def test_reentry_cooldown_is_three_hours(self):
        self.assertEqual(SIGNAL_COOLDOWN_MS, 3 * 60 * 60_000)

    def test_rolling_volume_series_matches_direct_calculation(self):
        rows = candles_from_closes(
            [1.0 + index * .01 for index in range(320)],
            volumes=[100.0 + index for index in range(320)],
        )
        optimized = rolling_quote_volume_series(rows)
        for index in (0, 50, 287, 288, 319):
            self.assertAlmostEqual(optimized[index], rolling_quote_volume(rows, index), places=7)

    def test_early_momentum_can_become_buy_opportunity(self):
        closes = [100.0 + index * .025 for index in range(114)]
        closes += [102.85, 103.20, 103.60, 104.00, 104.45, 104.90]
        volumes = [100.0] * 117 + [220.0, 230.0, 240.0]
        result = evaluate_entry(
            'TEST-EUR', candles_from_closes(closes, volumes=volumes), self.btc(),
            volume_quote_eur=2_000_000.0, spread_pct=.08,
        )
        self.assertEqual(result['route'], 'VROEG_MOMENTUM')
        self.assertEqual(result['action'], 'KOOPKANS')
        self.assertGreaterEqual(result['score'], 70.0)
        self.assertIn(result['proposed_paper_eur'], {250.0, 400.0, 500.0})
        self.assertFalse(result['execution_enabled'])
        self.assertFalse(result['live_orders_possible'])

    def test_late_pump_is_never_a_buy(self):
        closes = [100.0] * 107 + [101, 102, 103, 104, 105, 107, 109, 111, 113, 116, 120, 124, 129]
        result = evaluate_entry(
            'PUMP-EUR', candles_from_closes(closes), self.btc(),
            volume_quote_eur=5_000_000.0, spread_pct=.05,
        )
        self.assertEqual(result['action'], 'PUMP_TE_LAAT')
        self.assertEqual(result['proposed_paper_eur'], 0.0)

    def test_bad_liquidity_is_rejected_before_setup(self):
        result = evaluate_entry(
            'THIN-EUR', candles_from_closes([100 + index * .05 for index in range(120)]),
            self.btc(), volume_quote_eur=10_000.0, spread_pct=.05,
        )
        self.assertEqual(result['action'], 'AFWIJZEN')
        self.assertIn('24u_eurovolume_te_laag', result['reasons'])

    def test_position_size_is_variable_but_capped(self):
        self.assertEqual(proposed_position_eur(69.9), 0.0)
        self.assertEqual(proposed_position_eur(70.0), 250.0)
        self.assertEqual(proposed_position_eur(80.0), 400.0)
        self.assertEqual(proposed_position_eur(90.0), 500.0)
        self.assertEqual(proposed_position_eur(500.0), 500.0)

    def test_large_profit_is_protected_after_reversal(self):
        result = evaluate_exit(
            entry_price=100.0, current_price=125.0, highest_price=130.0,
            initial_stop_price=97.0, held_hours=30.0,
            current_features={'trend_up': True, 'volume_ratio': 1.2, 'return_15m_pct': -1.0},
        )
        self.assertEqual(result['action'], 'VERKOPEN')
        self.assertEqual(result['reason'], 'grote_winst_beschermen')
        self.assertGreater(result['protected_stop_price'], 120.0)

    def test_signal_trade_takes_partial_profit_and_protects_remainder(self):
        closes = [0.90 + index * .0015 for index in range(61)] + [1.16, 1.24]
        volumes = [100.0] * 60 + [1000.0, 1000.0, 1000.0]
        rows = candles_from_closes(closes, volumes=volumes)
        rows[-1] = Candle(
            timestamp_ms=rows[-1].timestamp_ms, open=1.29, high=1.30,
            low=1.23, close=1.24, volume=1000.0,
        )
        signal = {
            'market': 'TEST-EUR', 'signal_ms': rows[60].timestamp_ms,
            'entry_reference': 0.99, 'stop_reference': 0.96,
            'route': 'VROEG_MOMENTUM', 'score': 92.0,
            'proposed_paper_eur': 500.0,
        }
        trade = simulate_signal_trade(rows, signal)
        self.assertEqual(trade['status'], 'GESLOTEN')
        self.assertEqual([event['action'] for event in trade['events']], [
            'KOPEN', 'DEEL_VERKOPEN', 'VERKOPEN',
        ])
        self.assertGreater(trade['result_eur'], 0.0)
        self.assertAlmostEqual(trade['assumed_roundtrip_cost_pct'], .78)

    def test_broad_runner_sells_half_then_protects_the_remainder(self):
        rows = candles_from_closes([100.0] * 61 + [126.0, 180.0, 120.0])
        rows[62] = Candle(
            timestamp_ms=rows[62].timestamp_ms, open=126.0, high=200.0,
            low=125.0, close=180.0, volume=100.0,
        )
        signal = {
            'market': 'RUNNER-EUR', 'signal_ms': rows[60].timestamp_ms,
            'entry_reference': 100.0, 'stop_reference': 97.0,
            'route': 'VROEG_MOMENTUM', 'score': 90.0,
            'proposed_paper_eur': 500.0,
        }
        trade = simulate_broad_runner_trade(rows, signal)
        self.assertEqual(trade['status'], 'GESLOTEN')
        self.assertTrue(trade['partial_taken'])
        self.assertEqual([event['action'] for event in trade['events']], [
            'KOPEN', 'DEEL_VERKOPEN', 'VERKOPEN',
        ])
        self.assertEqual(trade['events'][1]['price'], 125.0)
        self.assertEqual(trade['events'][2]['price'], 140.0)
        self.assertGreater(trade['result_eur'], 0.0)

    def test_runner_validation_is_not_approved_by_lsk_alone(self):
        baseline = []
        runner = []
        for index in range(50):
            market = 'LSK-EUR' if index == 0 else f'M{index}-EUR'
            common = {
                'market': market, 'signal_ms': index,
                'status': 'GESLOTEN', 'events': [{'event_ms': index}],
            }
            baseline.append({**common, 'result_eur': 0.0})
            runner.append({
                **common,
                'result_eur': 1000.0 if market == 'LSK-EUR' else -1.0,
            })
        report = build_runner_validation(baseline, runner)
        self.assertGreater(report['all_markets']['runner']['total_result_eur'], 0.0)
        self.assertLess(report['without_lsk']['runner']['total_result_eur'], 0.0)
        self.assertNotEqual(report['decision'], 'KANDIDAAT_VOOR_APARTE_PAPERTEST')
        self.assertFalse(report['active_bot_changed'])

    def test_strategy_summary_calculates_signal_sequence_drawdown(self):
        trades = [
            {'market': 'A-EUR', 'result_eur': 10.0, 'events': [{'event_ms': 1}]},
            {'market': 'B-EUR', 'result_eur': -25.0, 'events': [{'event_ms': 2}]},
            {'market': 'C-EUR', 'result_eur': 5.0, 'events': [{'event_ms': 3}]},
        ]
        summary = summarize_strategy_trades(trades)
        self.assertEqual(summary['maximum_signal_sequence_drawdown_eur'], 25.0)
        self.assertEqual(summary['worst_trade_eur'], -25.0)

    def test_accelerating_profit_can_be_partially_sold(self):
        result = evaluate_exit(
            entry_price=100.0, current_price=118.0, highest_price=118.0,
            initial_stop_price=97.0, held_hours=24.0,
            current_features={'trend_up': True, 'volume_ratio': 3.0, 'return_15m_pct': 4.0},
        )
        self.assertEqual(result['action'], 'DEEL_VERKOPEN')

    def test_offline_scan_includes_all_markets_but_skips_thin_market_calls(self):
        candles = candles_from_closes([100.0 + index * .02 for index in range(120)])

        class Book:
            spread_pct = .05

        class Api:
            candle_markets = []

            def trading_markets(self, quote):
                self.markets_quote = quote
                return ['BTC-EUR', 'MISSING-EUR', 'THIN-EUR']

            def quote_market_tickers(self, quote):
                self.assert_quote = quote
                return [
                    {'market': 'BTC-EUR', 'last': 102.0, 'volume_quote': 5_000_000.0},
                    {'market': 'THIN-EUR', 'last': 1.0, 'volume_quote': 10_000.0},
                ]

            def market_books(self, markets):
                self.book_markets = markets
                return {'BTC-EUR': Book()}

            def closed_candles(self, market, interval, limit):
                del interval, limit
                self.candle_markets.append(market)
                return candles

        api = Api()
        report = scan_all_eur(api)
        self.assertEqual(report['markets_seen'], 3)
        self.assertEqual(report['markets_evaluated'], 3)
        self.assertEqual(api.candle_markets.count('THIN-EUR'), 0)
        thin = next(item for item in report['decisions'] if item['market'] == 'THIN-EUR')
        self.assertEqual(thin['action'], 'AFWIJZEN')
        missing = next(item for item in report['decisions'] if item['market'] == 'MISSING-EUR')
        self.assertIn('actieve_markt_zonder_bruikbare_ticker', missing['reasons'])
        self.assertFalse(report['execution_enabled'])

    def test_forward_outcomes_include_costs_and_long_horizons(self):
        candles = candles_from_closes([100.0] * 10 + [102.0, 104.0, 106.0, 108.0])
        result = forward_outcomes(
            candles, 9, spread_pct=.12, horizons_minutes=(5, 15, 30),
        )
        self.assertAlmostEqual(result['5']['gross_pct'], 2.0)
        self.assertAlmostEqual(result['5']['net_pct'], 1.22)
        self.assertEqual(result['15']['mature'], 1)
        self.assertEqual(result['30']['mature'], 0)

    def test_rolling_volume_never_uses_future_candles(self):
        base = candles_from_closes([1.0] * 300, volumes=[100.0] * 300)
        before = rolling_quote_volume(base, 290)
        changed = list(base)
        future = changed[299]
        changed[299] = Candle(
            timestamp_ms=future.timestamp_ms, open=1.0, high=1.0,
            low=1.0, close=1.0, volume=9_999_999.0,
        )
        self.assertEqual(before, rolling_quote_volume(changed, 290))

    def test_replay_summary_covers_48_hours(self):
        replay = {
            'market': 'TEST-EUR',
            'signals': [{
                'market': 'TEST-EUR', 'route': 'SWING_OPBOUW',
                'outcomes': {'2880': {'net_pct': 12.5}},
            }],
        }
        summary = summarize_replays([replay])
        self.assertEqual(summary['markets_with_signals'], 1)
        self.assertEqual(summary['outcomes']['2880']['samples'], 1)
        self.assertEqual(summary['outcomes']['2880']['average_net_pct'], 12.5)
        self.assertFalse(summary['execution_enabled'])

    def test_large_move_audit_separates_early_detection_from_miss(self):
        closes = [100.0 + index * (20.0 / 287.0) for index in range(288)]
        candles = candles_from_closes(closes)
        early = [{
            'signal_ms': candles[10].timestamp_ms,
            'entry_reference': candles[10].close,
            'route': 'VROEG_MOMENTUM',
        }]
        caught = audit_large_moves('TEST-EUR', candles, early)
        missed = audit_large_moves('TEST-EUR', candles, [])
        self.assertEqual(caught['large_moves'], 1)
        self.assertEqual(caught['caught_early'], 1)
        self.assertEqual(missed['missed'], 1)

    def test_historical_lab_keeps_control_markets_and_saves_no_raw_candles(self):
        day_ms = 86_400_000
        end_ms = 40 * day_ms
        fetch_start = end_ms - 8 * day_ms
        closes = [100.0] * (8 * 288)
        rows = candles_from_closes(closes, start_ms=fetch_start)

        class Api:
            def closed_candles_between(self, market, interval, start_ms, stop_ms, now_ms):
                del market, interval
                self.bounds = (start_ms, stop_ms, now_ms)
                return rows

        report = run_historical_lab(
            Api(), days=7, end_ms=end_ms,
            markets=['VTHO-EUR', 'LSK-EUR'],
        )
        self.assertEqual(report['markets_requested'], 3)
        self.assertEqual(report['markets_completed'], 3)
        self.assertIn('VTHO-EUR', report['control_cases'])
        self.assertIn('LSK-EUR', report['control_cases'])
        self.assertFalse(report['raw_candles_saved'])
        self.assertEqual(report['version'], '4.0-phase-6')
        self.assertEqual(report['control_cases']['LSK-EUR']['paper_trades'], [])
        self.assertEqual(report['control_cases']['LSK-EUR']['runner_trades'], [])
        self.assertEqual(report['runner_validation']['decision'], 'ONVOLDOENDE_DATA')
        self.assertFalse(report['execution_enabled'])

    def test_historical_lab_rejects_unbounded_period(self):
        with self.assertRaises(ValueError):
            run_historical_lab(object(), days=91)


if __name__ == '__main__':
    unittest.main()


class V40CapacityPortfolioTests(unittest.TestCase):
    def trade(self, market, signal_ms, *, result, close_ms, score=80.0):
        notional = 1000.0
        return {
            'market': market, 'signal_ms': signal_ms, 'score': score,
            'position_eur': notional, 'status': 'GESLOTEN',
            'realized_proceeds_eur': notional + result, 'open_value_eur': 0.0,
            'result_eur': result,
            'events': [
                {'event_ms': signal_ms, 'action': 'KOPEN'},
                {'event_ms': close_ms, 'action': 'VERKOPEN'},
            ],
        }

    def test_capacity_blocks_duplicate_and_over_deployment(self):
        report = simulate_capacity_limited_portfolio([
            self.trade('AAA-EUR', 0, result=100.0, close_ms=10),
            self.trade('BBB-EUR', 1, result=-50.0, close_ms=20),
            self.trade('AAA-EUR', 2, result=20.0, close_ms=30),
            self.trade('CCC-EUR', 3, result=20.0, close_ms=30),
        ])
        self.assertEqual(report['trades_accepted'], 2)
        self.assertEqual(report['trades_rejected'], 2)
        self.assertEqual(report['rejection_counts']['dubbele_munt'], 1)
        self.assertEqual(report['rejection_counts']['maximale_inzet_2500'], 1)
        self.assertEqual(report['portfolio_limits']['reserve_eur'], 200.0)
        self.assertEqual(report['portfolio_limits']['maximum_open_positions'], 5)

    def test_capacity_validation_is_observe_only(self):
        report = build_capacity_validation([
            self.trade('AAA-EUR', 0, result=10.0, close_ms=1),
        ])
        self.assertFalse(report['execution_enabled'])
        self.assertFalse(report['live_orders_possible'])
        self.assertFalse(report['active_paper_changed'])


class V40CapacityDiagnosticTests(unittest.TestCase):
    def test_diagnostics_keep_vtho_visible_and_are_observe_only(self):
        accepted = [{
            'market': 'VTHO-EUR', 'route': 'VROEG_MOMENTUM', 'score': 91.0,
            'result_eur': 12.0, 'holding_hours': 6.0,
            'relative_strength_vs_btc_1h_pct': 1.4,
            'entry_features': {'volume_ratio': 2.0},
            'btc_return_1h_pct': .5,
        }]
        validation = {
            'all_markets': {
                'accepted_trades': accepted,
                'rejection_counts_by_market': {'VTHO-EUR': {'dubbele_munt': 2}},
            },
            'without_lsk': {'accepted_trades': accepted},
        }
        report = build_capacity_diagnostics(validation)
        self.assertEqual(report['control_markets']['VTHO-EUR']['selected']['trades'], 1)
        self.assertEqual(
            report['control_markets']['VTHO-EUR']['rejection_counts']['dubbele_munt'], 2
        )
        self.assertEqual(report['by_route']['VROEG_MOMENTUM']['total_result_eur'], 12.0)
        self.assertFalse(report['active_paper_changed'])
        self.assertFalse(report['live_orders_possible'])
