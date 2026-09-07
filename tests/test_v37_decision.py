import unittest

from models import Candle
from v36_decision import (
    FIFTEEN_MINUTES_MS,
    FIVE_MINUTES_MS,
    ONE_HOUR_MS,
    candle_quality,
    timeframe_features,
)
from v37_decision import evaluate_human_gates, resolve_regime


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


def strong_context(now_ms: int = NOW_MS) -> dict:
    five_rows = strong_candles(now_ms, FIVE_MINUTES_MS)
    fifteen_rows = strong_candles(now_ms, FIFTEEN_MINUTES_MS)
    hour_rows = strong_candles(now_ms, ONE_HOUR_MS)
    return {
        'market': 'RAY-EUR',
        'cycle_ms': now_ms - FIVE_MINUTES_MS,
        'regime': 'BULL',
        'bull_breadth_pct': 80.0,
        'bear_breadth_pct': 5.0,
        'five': timeframe_features(five_rows),
        'fifteen': timeframe_features(fifteen_rows),
        'hour': timeframe_features(hour_rows),
        'bitcoin': {
            'five': timeframe_features(five_rows),
            'fifteen': timeframe_features(fifteen_rows),
            'hour': timeframe_features(hour_rows),
        },
        'quality': {
            'five': candle_quality(
                five_rows, interval_ms=FIVE_MINUTES_MS, now_ms=now_ms,
                allow_one_small_gap=True,
            ),
            'fifteen': candle_quality(
                fifteen_rows, interval_ms=FIFTEEN_MINUTES_MS, now_ms=now_ms,
            ),
            'hour': candle_quality(
                hour_rows, interval_ms=ONE_HOUR_MS, now_ms=now_ms,
            ),
            'bitcoin_five': candle_quality(
                five_rows, interval_ms=FIVE_MINUTES_MS, now_ms=now_ms,
                allow_one_small_gap=True,
            ),
            'bitcoin_fifteen': candle_quality(
                fifteen_rows, interval_ms=FIFTEEN_MINUTES_MS, now_ms=now_ms,
            ),
            'bitcoin_hour': candle_quality(
                hour_rows, interval_ms=ONE_HOUR_MS, now_ms=now_ms,
            ),
        },
    }


def invariants() -> dict:
    return {
        'mode': 'OBSERVE_ONLY',
        'paper_start_eur': 3000.0,
        'position_eur': 500.0,
        'reserve_eur': 200.0,
        'max_open_positions': 5,
        'existing_assets_excluded': True,
    }


def snapshots(now_ms: int = NOW_MS, *, unstable: bool = False) -> list[dict]:
    spreads = [0.06, 0.07, 0.40 if unstable else 0.05]
    return [
        {
            'captured_at_ms': now_ms - 60_000 + index * 30_000,
            'buy_vwap': 100.00 + index * 0.01,
            'sell_vwap': 99.94 + index * 0.01,
            'execution_spread_pct': spread,
            'near_book_imbalance': [0.18, 0.12, 0.20][index],
        }
        for index, spread in enumerate(spreads)
    ]


class V37DecisionTests(unittest.TestCase):
    def test_complete_stable_evidence_becomes_shadow_opportunity_only(self):
        decision = evaluate_human_gates(
            context=strong_context(),
            snapshots=snapshots(),
            invariants=invariants(),
            now_ms=NOW_MS,
        )
        self.assertTrue(decision['would_enter'], decision['blockers'])
        self.assertFalse(decision['eligible'])
        self.assertFalse(decision['execution_enabled'])
        self.assertEqual(decision['action'], 'SCHADUW-KANS')
        self.assertTrue(all(
            gate['status'] == 'PASS' for gate in decision['gates'].values()
        ))
        self.assertGreaterEqual(decision['edge']['net_reward_risk'], 1.20)
        self.assertTrue(decision['thesis']['context_hash'])

    def test_one_l2_snapshot_waits_instead_of_guessing(self):
        decision = evaluate_human_gates(
            context=strong_context(),
            snapshots=snapshots()[:1],
            invariants=invariants(),
            now_ms=NOW_MS,
        )
        self.assertEqual(decision['action'], 'L2 VERZAMELEN')
        self.assertFalse(decision['would_enter'])
        self.assertEqual(decision['gates']['l2_bevestiging']['status'], 'WAIT')

    def test_bad_data_is_a_veto_and_cannot_be_offset(self):
        context = strong_context()
        context['quality']['hour'] = {
            'valid': False, 'status': 'BLOCK', 'reason': 'recente_candle_ontbreekt'
        }
        decision = evaluate_human_gates(
            context=context,
            snapshots=snapshots(),
            invariants=invariants(),
            now_ms=NOW_MS,
        )
        self.assertEqual(decision['action'], 'AFWIJZEN')
        self.assertEqual(decision['gates']['data_integriteit']['status'], 'BLOCK')
        self.assertIn('hour_recente_candle_ontbreekt', decision['blockers'])

    def test_capital_and_existing_asset_rules_are_hard_vetoes(self):
        unsafe = invariants()
        unsafe['reserve_eur'] = 199.0
        unsafe['existing_assets_excluded'] = False
        decision = evaluate_human_gates(
            context=strong_context(),
            snapshots=snapshots(),
            invariants=unsafe,
            now_ms=NOW_MS,
        )
        self.assertEqual(decision['gates']['veiligheidsgrenzen']['status'], 'BLOCK')
        self.assertIn('reserve_lager_dan_200', decision['blockers'])
        self.assertIn('bestaande_munten_niet_uitgesloten', decision['blockers'])

    def test_negative_momentum_cannot_receive_a_safe_setup_pass(self):
        context = strong_context()
        context['five']['momentum_3_pct'] = -4.0
        decision = evaluate_human_gates(
            context=context,
            snapshots=snapshots(),
            invariants=invariants(),
            now_ms=NOW_MS,
        )
        self.assertEqual(decision['gates']['setup_expert']['status'], 'BLOCK')
        self.assertIn('5m_momentum_niet_positief', decision['blockers'])

    def test_unstable_l2_window_blocks_even_when_median_looks_good(self):
        decision = evaluate_human_gates(
            context=strong_context(),
            snapshots=snapshots(unstable=True),
            invariants=invariants(),
            now_ms=NOW_MS,
        )
        self.assertEqual(decision['gates']['l2_bevestiging']['status'], 'BLOCK')
        self.assertIn('l2_spread_niet_stabiel', decision['blockers'])
        self.assertFalse(decision['would_enter'])

    def test_portfolio_correlation_cluster_is_a_hard_veto(self):
        decision = evaluate_human_gates(
            context=strong_context(),
            snapshots=snapshots(),
            invariants=invariants(),
            portfolio_state={
                'paper_cash_eur': 2500.0,
                'open_positions': 2,
                'open_planned_risk_eur': 20.0,
                'cluster_counts': {'solana_ecosysteem': 2},
                'entries_this_cycle': 0,
                'daily_realized_pnl_eur': 0.0,
                'recent_stops_two_hours': 0,
            },
            now_ms=NOW_MS,
        )
        self.assertEqual(decision['gates']['portefeuillerisico']['status'], 'BLOCK')
        self.assertIn('correlatiecluster_reeds_vol', decision['blockers'])

    def test_regime_change_requires_confirmation_and_exposes_transition(self):
        first = resolve_regime(previous_regime='BULL', raw_regime='SIDEWAYS')
        self.assertEqual(first['regime'], 'TRANSITION')
        second = resolve_regime(
            previous_regime='BULL',
            raw_regime='SIDEWAYS',
            pending_regime=first['pending_regime'],
            pending_count=first['pending_count'],
        )
        self.assertEqual(second['regime'], 'SIDEWAYS')
        self.assertFalse(second['transition'])


if __name__ == '__main__':
    unittest.main()
