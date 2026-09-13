import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import autonomous_v40 as v40


NOW_MS = 2_000_000_000


def buy_scan(cycle_ms=NOW_MS):
    del cycle_ms
    return {
        'markets_seen': 2,
        'markets_evaluated': 2,
        'errors': [],
        'decisions': [
            {
                'market': 'TEST-EUR', 'action': 'KOOPKANS',
                'route': 'SWING_OPBOUW', 'score': 85.0,
                'reasons': [], 'proposed_paper_eur': 400.0,
                'entry_reference': 100.0, 'stop_reference': 97.0,
                'target_reference': 110.0, 'gross_stop_pct': 3.0,
                'gross_target_pct': 10.0, 'estimated_roundtrip_cost_pct': .78,
                'net_reward_risk': 2.0, 'relative_strength_vs_btc_1h_pct': 2.0,
                'features': {'return_60m_pct': 2.5},
                'execution_enabled': False, 'live_orders_possible': False,
            },
            {
                'market': 'THIN-EUR', 'action': 'AFWIJZEN',
                'route': 'GEEN_SETUP', 'score': 0.0,
                'reasons': ['24u_eurovolume_te_laag'], 'proposed_paper_eur': 0.0,
                'execution_enabled': False, 'live_orders_possible': False,
            },
        ],
    }


class StablePublicApi:
    sell_price = 102.0

    def depth_book(self, market, notional):
        self.last_depth = (market, notional)
        return {
            'buy_vwap': 100.0, 'sell_vwap': 99.92,
            'execution_spread_pct': .08, 'near_book_imbalance': .20,
            'notional_quote': notional,
        }

    def sell_vwap_for_base(self, market, base_amount):
        self.last_sell = (market, base_amount)
        return {'sell_vwap': self.sell_price, 'base_amount': base_amount}


class AutonomousV40Tests(unittest.TestCase):
    def settings(self, root: Path):
        return replace(
            v40.V40RuntimeSettings(),
            db_path=str(root / 'v40.db'), report_path=str(root / 'v40.json'),
        )

    def test_runtime_has_fixed_3600_euro_safety_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = self.settings(Path(temporary))
            v40.ensure_runtime(settings, NOW_MS)
            with sqlite3.connect(settings.db_path) as conn:
                meta = dict(conn.execute('SELECT key,value FROM v40_meta'))
            self.assertEqual(meta['paper_start_eur'], '3600')
            self.assertEqual(meta['reserve_eur'], '200')
            self.assertEqual(meta['execution_enabled'], '0')
            self.assertEqual(meta['live_orders_possible'], '0')
            self.assertEqual(meta['existing_assets_excluded'], '1')

    def test_candidate_needs_three_l2_samples_over_one_minute(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = self.settings(Path(temporary))
            v40.ensure_runtime(settings, NOW_MS)
            stored = v40.ingest_scan(settings, buy_scan(), now_ms=NOW_MS)
            self.assertEqual(stored['queued_l2'], 1)
            api = StablePublicApi()
            first = v40.recheck_candidates(settings, api=api, now_ms=NOW_MS)
            second = v40.recheck_candidates(settings, api=api, now_ms=NOW_MS + 30_000)
            third = v40.recheck_candidates(settings, api=api, now_ms=NOW_MS + 60_000)
            self.assertEqual(first['confirmed'], [])
            self.assertEqual(second['confirmed'], [])
            self.assertEqual(third['confirmed'], ['TEST-EUR'])
            with sqlite3.connect(settings.db_path) as conn:
                alert = conn.execute(
                    'SELECT market,alert_type,proposed_paper_eur,buy_vwap,stop_reference '
                    'FROM v40_alerts'
                ).fetchone()
            self.assertEqual(alert[0], 'TEST-EUR')
            self.assertEqual(alert[1], 'KOOPKANS')
            self.assertEqual(alert[2], 400.0)
            self.assertAlmostEqual(alert[3], 100.0)
            self.assertAlmostEqual(alert[4], 97.0)

    def test_market_cooldown_prevents_duplicate_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = self.settings(Path(temporary))
            v40.ensure_runtime(settings, NOW_MS)
            first = v40.ingest_scan(settings, buy_scan(), now_ms=NOW_MS)
            second = v40.ingest_scan(settings, buy_scan(), now_ms=NOW_MS + 300_000)
            self.assertEqual(first['queued_l2'], 1)
            self.assertEqual(second['queued_l2'], 0)
            with sqlite3.connect(settings.db_path) as conn:
                count = conn.execute('SELECT COUNT(*) FROM v40_candidates').fetchone()[0]
            self.assertEqual(count, 1)

    def test_market_can_be_reconsidered_after_three_hours(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = self.settings(Path(temporary))
            v40.ensure_runtime(settings, NOW_MS)
            first = v40.ingest_scan(settings, buy_scan(), now_ms=NOW_MS)
            self.assertEqual(first['queued_l2'], 1)
            with sqlite3.connect(settings.db_path) as conn:
                conn.execute("UPDATE v40_candidates SET status='BEVESTIGD'")
                conn.commit()
            second = v40.ingest_scan(
                settings, buy_scan(), now_ms=NOW_MS + v40.CANDIDATE_COOLDOWN_MS + 1,
            )
            self.assertEqual(second['queued_l2'], 1)
            with sqlite3.connect(settings.db_path) as conn:
                count = conn.execute('SELECT COUNT(*) FROM v40_candidates').fetchone()[0]
            self.assertEqual(count, 2)

    def test_unresolved_candidate_remains_blocked_after_three_hours(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = self.settings(Path(temporary))
            v40.ensure_runtime(settings, NOW_MS)
            v40.ingest_scan(settings, buy_scan(), now_ms=NOW_MS)
            second = v40.ingest_scan(
                settings, buy_scan(), now_ms=NOW_MS + v40.CANDIDATE_COOLDOWN_MS + 1,
            )
            self.assertEqual(second['queued_l2'], 0)

    def test_old_signal_chain_is_removed_after_ninety_days(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = self.settings(Path(temporary))
            old_ms = NOW_MS
            current = old_ms + v40.HISTORY_RETENTION_MS + 1
            v40.ensure_runtime(settings, old_ms)
            v40.ingest_scan(settings, buy_scan(), now_ms=old_ms)
            api = StablePublicApi()
            for offset in (0, 30_000, 60_000):
                v40.recheck_candidates(settings, api=api, now_ms=old_ms + offset)
            v40.monitor_outcomes(settings, api=api, now_ms=old_ms + 15 * 60_000)
            stored = v40.ingest_scan(settings, {'decisions': []}, now_ms=current)
            self.assertEqual(stored['removed_history'], 1)
            with sqlite3.connect(settings.db_path) as conn:
                counts = [
                    conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                    for table in ('v40_candidates', 'v40_l2', 'v40_alerts', 'v40_outcomes')
                ]
            self.assertEqual(counts, [0, 0, 0, 0])

    def test_notifications_are_removed_after_thirty_days(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = self.settings(Path(temporary))
            old_ms = NOW_MS
            current = old_ms + v40.NOTIFICATION_RETENTION_MS + 1
            v40.ensure_runtime(settings, old_ms)
            scan = buy_scan()
            follow = dict(scan['decisions'][0])
            follow.update({'market': 'WATCH-EUR', 'action': 'VOLGEN', 'score': 72.0})
            scan['decisions'].append(follow)
            v40.ingest_scan(settings, scan, now_ms=old_ms)
            stored = v40.ingest_scan(settings, {'decisions': []}, now_ms=current)
            self.assertEqual(stored['removed_notifications'], 1)
            with sqlite3.connect(settings.db_path) as conn:
                count = conn.execute('SELECT COUNT(*) FROM v40_notifications').fetchone()[0]
            self.assertEqual(count, 0)

    def test_outcome_and_report_remain_observe_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = self.settings(Path(temporary))
            v40.ensure_runtime(settings, NOW_MS)
            v40.ingest_scan(settings, buy_scan(), now_ms=NOW_MS)
            api = StablePublicApi()
            for offset in (0, 30_000, 60_000):
                v40.recheck_candidates(settings, api=api, now_ms=NOW_MS + offset)
            result = v40.monitor_outcomes(
                settings, api=api, now_ms=NOW_MS + 60_000 + 15 * 60_000,
            )
            self.assertEqual(result['measured'], 1)
            report = v40.build_report(settings, NOW_MS + 60_000 + 15 * 60_000)
            self.assertFalse(report['safety']['execution_enabled'])
            self.assertFalse(report['safety']['live_orders_possible'])
            self.assertEqual(report['paper_portfolio']['start_eur'], 3600.0)
            self.assertEqual(report['paper_portfolio']['buffer_at_maximum_allocation_eur'], 1100)
            self.assertEqual(len(report['alerts_last_24h']), 1)
            self.assertEqual(report['prospective_outcomes']['15']['samples'], 1)
            self.assertAlmostEqual(report['prospective_outcomes']['15']['average_net_pct'], 1.34)

    def test_isolated_paper_position_opens_and_closes_without_live_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = self.settings(Path(temporary))
            v40.ensure_runtime(settings, NOW_MS)
            v40.ingest_scan(settings, buy_scan(), now_ms=NOW_MS)
            api = StablePublicApi()
            for offset in (0, 30_000, 60_000):
                v40.recheck_candidates(settings, api=api, now_ms=NOW_MS + offset)
            opened = v40.simulate_paper_portfolio(
                settings, api=api, now_ms=NOW_MS + 61_000,
            )
            self.assertEqual(opened['opened'], ['TEST-EUR'])
            with sqlite3.connect(settings.db_path) as conn:
                cash = conn.execute('SELECT cash_eur FROM v40_paper_account').fetchone()[0]
                remaining = conn.execute(
                    "SELECT remaining_base FROM v40_paper_positions WHERE status='OPEN'"
                ).fetchone()[0]
            self.assertEqual(cash, 3200.0)
            self.assertAlmostEqual(remaining, 3.99)

            api.sell_price = 96.0
            exited = v40.simulate_paper_portfolio(
                settings, api=api, now_ms=NOW_MS + 62_000,
            )
            self.assertEqual(exited['closed'], ['TEST-EUR'])
            report = v40.build_report(settings, NOW_MS + 62_000)
            self.assertEqual(report['paper_portfolio']['open_positions'], 0)
            self.assertLess(report['paper_portfolio']['realized_pnl_eur'], 0.0)
            with sqlite3.connect(settings.db_path) as conn:
                events = [row[0] for row in conn.execute(
                    'SELECT event_type FROM v40_paper_events ORDER BY id'
                )]
            self.assertEqual(events, ['KOPEN', 'VERKOPEN'])

    def test_notification_feed_contains_follow_buy_and_sell_events_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = self.settings(Path(temporary))
            settings = replace(settings, notification_path=str(Path(temporary) / 'notices.json'))
            v40.ensure_runtime(settings, NOW_MS)
            scan = buy_scan()
            follow = dict(scan['decisions'][0])
            follow.update({'market': 'WATCH-EUR', 'action': 'VOLGEN', 'score': 72.0})
            scan['decisions'].append(follow)
            v40.ingest_scan(settings, scan, now_ms=NOW_MS)
            api = StablePublicApi()
            for offset in (0, 30_000, 60_000):
                v40.recheck_candidates(settings, api=api, now_ms=NOW_MS + offset)
            v40.simulate_paper_portfolio(settings, api=api, now_ms=NOW_MS + 61_000)
            api.sell_price = 96.0
            v40.simulate_paper_portfolio(settings, api=api, now_ms=NOW_MS + 62_000)
            feed = v40.write_notification_feed(settings, now_ms=NOW_MS + 63_000)
            kinds = [item['notification_type'] for item in reversed(feed['notifications'])]
            self.assertEqual(kinds, ['VOLGEN', 'KOOPKANS', 'KOPEN', 'VERKOPEN'])
            self.assertFalse(feed['execution_enabled'])
            self.assertFalse(feed['live_orders_possible'])
            self.assertTrue(Path(settings.notification_path).exists())


if __name__ == '__main__':
    unittest.main()
