import unittest
import tempfile
from pathlib import Path

import autonomous_v38 as v38
from v38_discovery import human_discovery_decision, movement_features, proposed_paper_size


class V38DiscoveryTests(unittest.TestCase):
    def test_early_vet_like_move_is_seen(self):
        now = 60 * 60_000
        prices = [(minute * 60_000, 0.00645 * (1 + 0.0007 * minute)) for minute in range(61)]
        features = movement_features(prices, now)
        decision = human_discovery_decision('VET-EUR', features, volume_quote=2_000_000)
        self.assertEqual(decision['action'], 'DOOR_NAAR_MENSELIJKE_JURY')
        self.assertGreater(proposed_paper_size(decision), 0)

    def test_late_pump_is_not_chased(self):
        features = {'valid': True, 'momentum_5m_pct': 3.2, 'momentum_15m_pct': 7.0,
                    'momentum_60m_pct': 15.0, 'acceleration_pct': 0.8}
        decision = human_discovery_decision('FAST-EUR', features, volume_quote=5_000_000)
        self.assertEqual(decision['action'], 'ALLEEN_VOLGEN')
        self.assertIn('mogelijk_te_laat_achter_pump', decision['reasons'])
        self.assertEqual(proposed_paper_size(decision), 0.0)

    def test_illiquid_coin_is_rejected(self):
        features = {'valid': True, 'momentum_5m_pct': .4, 'momentum_15m_pct': .8,
                    'momentum_60m_pct': 1.0, 'acceleration_pct': .13}
        decision = human_discovery_decision('TINY-EUR', features, volume_quote=10_000)
        self.assertIn('24u_euromarkt_te_illiquide', decision['reasons'])

    def test_observer_never_executes_and_discovers_vet_after_history(self):
        class Api:
            now = 0
            def quote_market_tickers(self, quote):
                self.assert_quote = quote
                minute = self.now // 60_000
                return [{'market': 'VET-EUR', 'last': 0.00645 * (1 + minute * .0007),
                         'volume_quote': 2_000_000.0}]

        with tempfile.TemporaryDirectory() as tmp:
            old_db, old_report = v38.DB_PATH, v38.REPORT_PATH
            v38.DB_PATH = str(Path(tmp) / 'v38.db')
            v38.REPORT_PATH = str(Path(tmp) / 'v38.json')
            try:
                api = Api()
                report = None
                for minute in range(17):
                    api.now = minute * 60_000
                    report = v38.scan_once(api=api, now_ms=api.now)
                self.assertIsNotNone(report)
                self.assertFalse(report['execution_enabled'])
                self.assertEqual(report['paper_execution'], 'UIT')
                self.assertEqual(report['ready_for_human_jury'][0]['market'], 'VET-EUR')
            finally:
                v38.DB_PATH, v38.REPORT_PATH = old_db, old_report


if __name__ == '__main__':
    unittest.main()
