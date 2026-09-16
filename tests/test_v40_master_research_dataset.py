import unittest

from models import Candle
from v40_master_research_dataset import (
    accumulate_early_market_context,
    attach_candidate_cross_section,
    barrier_label_48h,
    early_candidate_features,
    finalize_early_market_context,
    forward_path_labels,
)


class MasterResearchDatasetTests(unittest.TestCase):
    def _candles(self, count=700, slope=0.05):
        rows = []
        for index in range(count):
            close = 100.0 + index * slope
            rows.append(Candle(timestamp_ms=index * 300_000, open=close - 0.01, high=close + 0.10, low=close - 0.10, close=close, volume=100.0 + index))
        return rows

    def test_early_features_use_only_signal_and_prior_candles(self):
        rows = self._candles(100)
        signal_ms = rows[80].timestamp_ms
        features = early_candidate_features(rows, signal_ms)
        self.assertGreater(features['return_5m_pct'], 0.0)
        self.assertGreater(features['return_10m_pct'], 0.0)
        self.assertIn('momentum_accel_5m_vs_15m', features)
        self.assertGreater(features['volume_accel_ratio'], 0.0)

    def test_market_context_has_5m_breadth_and_change(self):
        aggregates = {}
        for slope in (0.10, -0.05):
            rows = self._candles(100, slope=slope)
            accumulate_early_market_context(rows, 0, rows[-1].timestamp_ms + 1, aggregates)
        context = finalize_early_market_context(aggregates)
        ts = 90 * 300_000
        item = context[ts]
        self.assertEqual(item['markets_used'], 2)
        self.assertEqual(item['breadth_positive_5m_pct'], 50.0)
        self.assertEqual(item['breadth_positive_10m_pct'], 50.0)
        self.assertIn('breadth_change_5m_pp', item)
        self.assertGreater(item['dispersion_return_5m_pct'], 0.0)

    def test_forward_labels_are_complete_with_48h_tail(self):
        rows = self._candles(700, slope=0.02)
        signal_index = 100
        signal = {'signal_ms': rows[signal_index].timestamp_ms, 'entry_reference': rows[signal_index].close, 'stop_reference': rows[signal_index].close * 0.97, 'target_reference': rows[signal_index].close * 1.04}
        labels = forward_path_labels(rows, signal)
        self.assertEqual(labels['2880']['mature'], 1)
        self.assertGreater(labels['60']['mfe_pct'], 0.0)
        self.assertLess(labels['60']['mae_pct'], labels['60']['mfe_pct'])

    def test_barrier_label_is_chronological_and_stop_first(self):
        rows = self._candles(700, slope=0.0)
        signal_index = 100
        entry = rows[signal_index].close
        rows[signal_index + 1] = Candle(timestamp_ms=rows[signal_index + 1].timestamp_ms, open=entry, high=entry * 1.05, low=entry * 0.95, close=entry, volume=100.0)
        signal = {'signal_ms': rows[signal_index].timestamp_ms, 'entry_reference': entry, 'stop_reference': entry * 0.97, 'target_reference': entry * 1.04}
        label = barrier_label_48h(rows, signal)
        self.assertEqual(label['outcome'], 'STOP')

    def test_cross_section_ranks_simultaneous_candidates(self):
        candidates = [
            {'signal_ms': 1000, 'base_score': 80.0, 'relative_strength_vs_btc_1h_pct': 1.0, 'entry_features': {'return_5m_pct': 1.0, 'momentum_accel_5m_vs_15m': 0.8, 'volume_accel_ratio': 2.0}},
            {'signal_ms': 1000, 'base_score': 75.0, 'relative_strength_vs_btc_1h_pct': 0.5, 'entry_features': {'return_5m_pct': 0.5, 'momentum_accel_5m_vs_15m': 0.2, 'volume_accel_ratio': 1.5}},
        ]
        attach_candidate_cross_section(candidates)
        self.assertEqual(candidates[0]['cross_section']['simultaneous_candidate_count'], 2)
        self.assertEqual(candidates[0]['cross_section']['rank_base_score'], 1)
        self.assertEqual(candidates[1]['cross_section']['rank_base_score'], 2)


if __name__ == '__main__':
    unittest.main()
