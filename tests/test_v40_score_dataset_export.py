import unittest

from v40_score_dataset_export import compact_trade


class ScoreDatasetExportTests(unittest.TestCase):
    def test_compact_trade_exports_measurement_fields_only(self):
        trade = {
            'market': 'TEST-EUR',
            'signal_ms': 1000,
            'route': 'VROEG_MOMENTUM',
            'score': 77.0,
            'position_eur': 250.0,
            'result_eur': 5.0,
            'result_pct': 2.0,
            'relative_strength_vs_btc_1h_pct': 1.2,
            'net_reward_risk': 1.8,
            'btc_return_1h_pct': 0.3,
            'entry_features': {
                'return_15m_pct': 0.5,
                'return_60m_pct': 1.0,
                'return_4h_pct': 2.0,
                'atr_pct': 0.8,
                'volume_ratio': 1.7,
                'trend_up': True,
                'unused': 999,
            },
            'status': 'GESLOTEN',
            'events': [
                {'event_ms': 1000, 'action': 'KOPEN'},
                {'event_ms': 2000, 'action': 'VERKOPEN', 'reason': 'TRAIL'},
            ],
        }
        row = compact_trade(trade)
        self.assertEqual(row['close_ms'], 2000)
        self.assertEqual(row['exit_reason'], 'TRAIL')
        self.assertEqual(row['result_pct'], 2.0)
        self.assertTrue(row['entry_features']['trend_up'])
        self.assertNotIn('unused', row['entry_features'])
        self.assertNotIn('events', row)


if __name__ == '__main__':
    unittest.main()
