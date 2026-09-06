import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

import autonomous_v36 as v36
from models import Candle
from v36_decision import (
    FIFTEEN_MINUTES_MS,
    FIVE_MINUTES_MS,
    JURY_CATEGORIES,
    ONE_HOUR_MS,
    candle_quality,
    evaluate_jury,
    timeframe_features,
)


def candles_for(now_ms: int, interval_ms: int, *, gap_count: int = 0) -> list[Candle]:
    latest_start = now_ms // interval_ms * interval_ms - interval_ms
    timestamps = [latest_start - index * interval_ms for index in reversed(range(60))]
    if gap_count >= 1:
        del timestamps[20]
    if gap_count >= 2:
        del timestamps[30]
    result = []
    for index, timestamp in enumerate(timestamps):
        close = 100.0 + index * 0.30
        result.append(Candle(
            timestamp_ms=timestamp,
            open=close - 0.15,
            high=close + 1.20,
            low=close - 1.20,
            close=close,
            volume=160.0 if index == len(timestamps) - 1 else 100.0,
        ))
    return result


class StrongPublicApi:
    def __init__(self, now_ms: int, markets: list[str] | None = None) -> None:
        self.now_ms = now_ms
        self.markets = markets or ['AAA-EUR', 'BBB-EUR']
        self.candle_calls: list[tuple[str, str]] = []
        self.sell_price = 119.0
        self.fail_market = ''
        self.fail_monitor = False

    def top_markets_by_quote_volume(self, quote: str, limit: int) -> list[str]:
        return self.markets[:limit]

    def closed_candles(self, market: str, interval: str, limit: int, now_ms=None):
        self.candle_calls.append((market, interval))
        if market == self.fail_market:
            raise RuntimeError('test API-fout')
        interval_ms = {'5m': FIVE_MINUTES_MS, '15m': FIFTEEN_MINUTES_MS, '1h': ONE_HOUR_MS}[interval]
        return candles_for(int(now_ms or self.now_ms), interval_ms)[-limit:]

    def depth_book(self, market: str, notional: float):
        return {
            'buy_vwap': 117.75,
            'sell_vwap': 117.68,
            'execution_spread_pct': 0.06,
            'near_book_imbalance': 0.20,
            'captured_at_ms': float(self.now_ms),
        }

    def sell_vwap_for_base(self, market: str, base_amount: float):
        if self.fail_monitor:
            raise RuntimeError('onvoldoende orderboekdiepte')
        return {'sell_vwap': self.sell_price, 'captured_at_ms': float(self.now_ms)}


class AutonomousV36Tests(unittest.TestCase):
    def settings(self, folder: str, **changes) -> v36.V36Settings:
        base = v36.V36Settings(
            db_path=str(Path(folder) / 'v36.db'),
            report_path=str(Path(folder) / 'v36.json'),
            universe_size=2,
        )
        return replace(base, **changes)

    def insert_decision(self, settings: v36.V36Settings, now_ms: int, market='AAA-EUR'):
        decision = {
            'market': market,
            'eligible': True,
            'active_candidate': True,
            'action': 'AUTOMATISCHE PAPER-INSTAP',
            'score': 90.0,
            'trigger': 'test',
            'data_quality_status': 'OK',
            'buy_vwap': 100.0,
            'sell_vwap': 99.95,
            'roundtrip_cost_pct': 0.71,
            'non_book_cost_pct': 0.66,
            'blockers': [],
            'jury': {},
        }
        context = {'market': market, 'cycle_ms': now_ms - FIVE_MINUTES_MS}
        conn = v36._connect(settings)
        decision_id = v36._decision_upsert(
            conn,
            decision_key=f'test:{market}',
            cycle_ms=context['cycle_ms'],
            evaluated_ms=now_ms,
            evaluation_kind='NEW_5M',
            decision=decision,
            context=context,
        )
        conn.commit()
        conn.close()
        return decision_id, decision, context

    def test_one_small_5m_gap_gets_penalty_but_two_gaps_block(self):
        now_ms = 300_000_000
        one_gap = candle_quality(
            candles_for(now_ms, FIVE_MINUTES_MS, gap_count=1),
            interval_ms=FIVE_MINUTES_MS,
            now_ms=now_ms,
            allow_one_small_gap=True,
        )
        self.assertTrue(one_gap['valid'])
        self.assertEqual(one_gap['status'], 'WARN')
        self.assertEqual(one_gap['score'], 4.0)

        two_gaps = candle_quality(
            candles_for(now_ms, FIVE_MINUTES_MS, gap_count=2),
            interval_ms=FIVE_MINUTES_MS,
            now_ms=now_ms,
            allow_one_small_gap=True,
        )
        self.assertFalse(two_gaps['valid'])
        self.assertEqual(two_gaps['reason'], 'meerdere_of_grote_candlegaten')

    def test_stale_latest_candle_blocks(self):
        now_ms = 300_000_000
        quality = candle_quality(
            candles_for(now_ms - 3 * FIVE_MINUTES_MS, FIVE_MINUTES_MS),
            interval_ms=FIVE_MINUTES_MS,
            now_ms=now_ms,
            allow_one_small_gap=True,
        )
        self.assertFalse(quality['valid'])
        self.assertEqual(quality['reason'], 'recente_candle_ontbreekt')

    def test_jury_has_all_eight_visible_votes_and_can_approve(self):
        now_ms = 300_000_000
        five_rows = candles_for(now_ms, FIVE_MINUTES_MS)
        fifteen_rows = candles_for(now_ms, FIFTEEN_MINUTES_MS)
        hour_rows = candles_for(now_ms, ONE_HOUR_MS)
        context = {
            'market': 'AAA-EUR',
            'regime': 'BULL',
            'five': timeframe_features(five_rows),
            'fifteen': timeframe_features(fifteen_rows),
            'hour': timeframe_features(hour_rows),
            'bitcoin': timeframe_features(five_rows),
            'quality': {
                'five': candle_quality(five_rows, interval_ms=FIVE_MINUTES_MS, now_ms=now_ms, allow_one_small_gap=True),
                'fifteen': candle_quality(fifteen_rows, interval_ms=FIFTEEN_MINUTES_MS, now_ms=now_ms),
                'hour': candle_quality(hour_rows, interval_ms=ONE_HOUR_MS, now_ms=now_ms),
                'bitcoin': candle_quality(five_rows, interval_ms=FIVE_MINUTES_MS, now_ms=now_ms, allow_one_small_gap=True),
            },
        }
        decision = evaluate_jury(
            context=context,
            depth={
                'buy_vwap': 117.75,
                'sell_vwap': 117.68,
                'execution_spread_pct': 0.06,
                'near_book_imbalance': 0.20,
                'captured_at_ms': float(now_ms),
            },
            now_ms=now_ms,
        )
        self.assertEqual(tuple(decision['jury']), JURY_CATEGORIES)
        self.assertTrue(decision['eligible'], decision['blockers'])
        self.assertGreaterEqual(decision['score'], 72.0)
        self.assertTrue(all('reason' in vote for vote in decision['jury'].values()))

    def test_live_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(tmp, mode='LIVE')
            with self.assertRaisesRegex(ValueError, 'uitsluitend PAPER'):
                settings.validate()

    def test_portfolio_starts_at_3000_and_deposits_are_separate_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(tmp)
            v36.ensure_portfolio(settings, 1_000_000)
            self.assertEqual(v36.apply_scheduled_deposits(settings, date(2026, 9, 20)), 2)
            self.assertEqual(v36.apply_scheduled_deposits(settings, date(2026, 9, 20)), 0)
            portfolio = v36.build_report(settings, 2_000_000)['portfolio']
            self.assertEqual(portfolio['start_eur'], 3000.0)
            self.assertEqual(portfolio['deposits_eur'], 100.0)
            self.assertEqual(portfolio['trading_pnl_eur'], 0.0)
            self.assertEqual(portfolio['reserve_eur'], 200.0)

    def test_duplicate_entry_is_blocked_and_restart_state_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(tmp)
            now_ms = 300_000_000
            v36.ensure_portfolio(settings, now_ms)
            decision_id, decision, context = self.insert_decision(settings, now_ms)
            self.assertTrue(v36.open_paper_position(
                settings, decision_id=decision_id, decision=decision,
                context=context, now_ms=now_ms,
            ))
            self.assertFalse(v36.open_paper_position(
                settings, decision_id=decision_id, decision=decision,
                context=context, now_ms=now_ms + 1,
            ))
            reopened = v36.build_report(settings, now_ms + 2)['portfolio']
            self.assertEqual(reopened['open'], 1)
            conn = sqlite3.connect(settings.db_path)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM v36_paper_orders').fetchone()[0], 1)
            conn.close()

    def test_reserve_is_a_hard_entry_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(tmp)
            now_ms = 300_000_000
            v36.ensure_portfolio(settings, now_ms)
            conn = v36._connect(settings)
            conn.execute(
                "INSERT INTO v36_ledger VALUES ('TEST_LOSS',?,'TRADE_PNL',-2400,'test')",
                (now_ms,),
            )
            conn.commit()
            conn.close()
            decision_id, decision, context = self.insert_decision(settings, now_ms)
            self.assertFalse(v36.open_paper_position(
                settings, decision_id=decision_id, decision=decision,
                context=context, now_ms=now_ms,
            ))
            self.assertEqual(v36.build_report(settings, now_ms)['portfolio']['open'], 0)

    def test_exact_quantity_monitor_activates_and_executes_trailing_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(tmp)
            now_ms = 300_000_000
            v36.ensure_portfolio(settings, now_ms)
            decision_id, decision, context = self.insert_decision(settings, now_ms)
            self.assertTrue(v36.open_paper_position(
                settings, decision_id=decision_id, decision=decision,
                context=context, now_ms=now_ms,
            ))
            api = StrongPublicApi(now_ms)
            api.sell_price = 102.0
            first = v36.monitor_positions(settings, api=api, now_ms=now_ms + 30_000)
            self.assertEqual(first['priced'], 1)
            opened = v36.build_report(settings, now_ms + 30_000)['portfolio']
            self.assertEqual(opened['open'], 1)
            self.assertIsNotNone(opened['open_positions'][0]['trailing_floor_pct'])
            api.sell_price = 100.8
            v36.monitor_positions(settings, api=api, now_ms=now_ms + 60_000)
            closed = v36.build_report(settings, now_ms + 60_000)['portfolio']
            self.assertEqual(closed['open'], 0)
            self.assertEqual(closed['closed'], 1)
            self.assertEqual(closed['wins'], 1)
            self.assertGreater(closed['trading_pnl_eur'], 0.0)

    def test_l2_depth_failure_never_closes_position_on_a_guessed_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(tmp)
            now_ms = 300_000_000
            v36.ensure_portfolio(settings, now_ms)
            decision_id, decision, context = self.insert_decision(settings, now_ms)
            v36.open_paper_position(
                settings, decision_id=decision_id, decision=decision,
                context=context, now_ms=now_ms,
            )
            api = StrongPublicApi(now_ms)
            api.fail_monitor = True
            result = v36.monitor_positions(settings, api=api, now_ms=now_ms + 30_000)
            self.assertEqual(result['priced'], 0)
            self.assertIn('onvoldoende orderboekdiepte', result['errors'][0])
            self.assertEqual(v36.build_report(settings, now_ms + 30_000)['portfolio']['open'], 1)

    def test_full_universe_runs_once_per_new_closed_5m_candle(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(tmp)
            now_ms = 300_000_000
            v36.ensure_portfolio(settings, now_ms)
            api = StrongPublicApi(now_ms)
            first = v36.evaluate_new_five_minute_cycle(settings, api=api, now_ms=now_ms)
            self.assertTrue(first['evaluated'])
            self.assertEqual(first['universe_count'], 2)
            self.assertEqual(first['decisions'], 2)
            first_market_calls = len([
                call for call in api.candle_calls if call[0] in {'AAA-EUR', 'BBB-EUR'}
            ])
            self.assertEqual(first_market_calls, 6)
            second = v36.evaluate_new_five_minute_cycle(settings, api=api, now_ms=now_ms)
            self.assertFalse(second['evaluated'])
            second_market_calls = len([
                call for call in api.candle_calls if call[0] in {'AAA-EUR', 'BBB-EUR'}
            ])
            self.assertEqual(second_market_calls, 6)

    def test_sixth_approved_market_is_logged_as_missed_when_five_slots_are_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            markets = [f'COIN{index}-EUR' for index in range(6)]
            settings = self.settings(tmp, universe_size=6)
            now_ms = 300_000_000
            v36.ensure_portfolio(settings, now_ms)
            result = v36.evaluate_new_five_minute_cycle(
                settings,
                api=StrongPublicApi(now_ms, markets),
                now_ms=now_ms,
            )
            self.assertEqual(result['opened'], 5)
            report = v36.build_report(settings, now_ms)
            self.assertEqual(report['portfolio']['open'], 5)
            self.assertEqual(report['missed_moves']['tracked'], 1)
            conn = sqlite3.connect(settings.db_path)
            row = conn.execute(
                "SELECT blockers_json FROM v36_decisions WHERE action='AFWIJZEN'"
            ).fetchone()
            conn.close()
            self.assertIn('paper_portefeuillegrens_of_dubbele_instap', json.loads(row[0]))

    def test_running_cycle_recovery_does_not_duplicate_or_relabel_existing_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(tmp)
            now_ms = 300_000_000
            v36.ensure_portfolio(settings, now_ms)
            api = StrongPublicApi(now_ms)
            v36.evaluate_new_five_minute_cycle(settings, api=api, now_ms=now_ms)
            conn = sqlite3.connect(settings.db_path)
            conn.execute("UPDATE v36_cycles SET status='RUNNING'")
            conn.commit()
            conn.close()
            recovered = v36.evaluate_new_five_minute_cycle(settings, api=api, now_ms=now_ms)
            self.assertTrue(recovered['evaluated'])
            report = v36.build_report(settings, now_ms)
            self.assertEqual(report['portfolio']['open'], 2)
            self.assertTrue(all(
                item['action'] == 'PAPER BUY'
                for item in report['jury']['latest_decisions']
                if item['evaluation_kind'] == 'NEW_5M'
            ))

    def test_market_api_failure_is_persisted_as_a_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(tmp)
            now_ms = 300_000_000
            v36.ensure_portfolio(settings, now_ms)
            api = StrongPublicApi(now_ms)
            api.fail_market = 'BBB-EUR'
            result = v36.evaluate_new_five_minute_cycle(settings, api=api, now_ms=now_ms)
            self.assertTrue(result['evaluated'])
            conn = sqlite3.connect(settings.db_path)
            action, blockers = conn.execute(
                "SELECT action,blockers_json FROM v36_decisions WHERE market='BBB-EUR'"
            ).fetchone()
            events = conn.execute(
                "SELECT COUNT(*) FROM v36_events WHERE event_type='MARKET_DATA_ERROR'"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(action, 'AFWIJZEN')
            self.assertIn('test API-fout', json.loads(blockers)[0])
            self.assertEqual(events, 1)


if __name__ == '__main__':
    unittest.main()
