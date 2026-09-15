import unittest

from models import Candle
from v40_score_dataset_export import compact_trade, enrich_trade_features


class ScoreDatasetExportTests(unittest.TestCase):
    def test_compact_trade_exports_rich_measurement_fields_only(self):
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
                'distance_to_4h_high_pct': -0.4,
                'extension_atr': 0.7,
                'compression_ratio': 0.8,
                'higher_low': True,
                'positive_bars_last_6': 5,
                'unused': 999,
            },
            'status': 'GESLOTEN',
            'events': [
                {'event_ms': 1000, 'action': 'KOPEN'},
                {'event_ms': 2000, 'action': 'VERKOPEN', 'reason': 'TRAIL'},
            ],
        }
        row = compact_trade(trade)
        features = row['entry_features']
        self.assertEqual(row['close_ms'], 2000)
        self.assertEqual(row['exit_reason'], 'TRAIL')
        self.assertEqual(row['result_pct'], 2.0)
        self.assertTrue(features['trend_up'])
        self.assertTrue(features['higher_low'])
        self.assertEqual(features['positive_bars_last_6'], 5)
        self.assertEqual(features['momentum_accel_15m_vs_60m'], 1.0)
        self.assertEqual(features['momentum_accel_60m_vs_4h'], 2.0)
        self.assertNotIn('unused', features)
        self.assertNotIn('events', row)

    def test_enrichment_uses_only_candles_through_signal(self):
        candles = []
        price = 100.0
        for index in range(120):
            close = price + index * 0.1
            candles.append(Candle(
                timestamp_ms=index * 300_000,
                open=close - 0.05,
                high=close + 0.1,
                low=close - 0.1,
                close=close,
                volume=100.0 + index,
            ))
        signal_ms = candles[-1].timestamp_ms
        trade = {
            'signal_ms': signal_ms,
            'entry_features': {'return_15m_pct': -999.0},
        }
        enriched = enrich_trade_features(candles, trade)
        features = enriched['entry_features']
        self.assertNotEqual(features['return_15m_pct'], -999.0)
        self.assertIn('distance_to_4h_high_pct', features)
        self.assertIn('extension_atr', features)
        self.assertIn('compression_ratio', features)
        self.assertIn('higher_low', features)
        self.assertIn('positive_bars_last_6', features)


if __name__ == '__main__':
    unittest.main()
