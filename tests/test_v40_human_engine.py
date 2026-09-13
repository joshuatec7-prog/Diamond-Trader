import unittest

from models import Candle
from v40_human_engine import (
    candle_features,
    evaluate_entry,
    evaluate_exit,
    proposed_position_eur,
)
from v40_offline_scan import scan_all_eur
from v40_historical_lab import run_historical_lab
from v40_replay import (
    audit_large_moves,
    forward_outcomes,
    rolling_quote_volume,
    simulate_signal_trade,
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

    def test_requires_closed_history(self):
        result = candle_features(candles_from_closes([100.0] * 59))
        self.assertFalse(result['valid'])

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
        self.assertEqual(report['version'], '4.0-phase-5')
        self.assertEqual(report['control_cases']['LSK-EUR']['paper_trades'], [])
        self.assertFalse(report['execution_enabled'])

    def test_historical_lab_rejects_unbounded_period(self):
        with self.assertRaises(ValueError):
            run_historical_lab(object(), days=91)


if __name__ == '__main__':
    unittest.main()
