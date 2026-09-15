import unittest

from v40_soft_memory_lab import (
    NEGATIVE_MEMORY_PENALTY_REASON,
    NEGATIVE_MEMORY_VETO,
    SOFT_MEMORY_PENALTY_POINTS,
    apply_marketwide_desk,
    build_soft_memory_comparison,
    soften_negative_memory_review,
)


class SoftMemoryLabTests(unittest.TestCase):
    def test_negative_memory_becomes_penalty_instead_of_hard_veto(self):
        review = {
            'confidence': 90.0,
            'bull_reasons': ['trend_opwaarts'],
            'bear_reasons': [],
            'risk_vetoes': [NEGATIVE_MEMORY_VETO],
            'memory': {'cases': 20},
            'eligible': False,
        }
        softened = soften_negative_memory_review(review)
        self.assertNotIn(NEGATIVE_MEMORY_VETO, softened['risk_vetoes'])
        self.assertIn(NEGATIVE_MEMORY_PENALTY_REASON, softened['bear_reasons'])
        self.assertEqual(softened['memory_penalty_points'], SOFT_MEMORY_PENALTY_POINTS)
        self.assertEqual(softened['confidence'], 78.0)
        self.assertTrue(softened['eligible'])

    def test_other_hard_safety_veto_stays_hard(self):
        review = {
            'confidence': 95.0,
            'bull_reasons': [],
            'bear_reasons': [],
            'risk_vetoes': [NEGATIVE_MEMORY_VETO, 'bitcoin_marktschok'],
            'memory': {'cases': 20},
            'eligible': False,
        }
        softened = soften_negative_memory_review(review)
        self.assertEqual(softened['confidence'], 83.0)
        self.assertNotIn(NEGATIVE_MEMORY_VETO, softened['risk_vetoes'])
        self.assertIn('bitcoin_marktschok', softened['risk_vetoes'])
        self.assertFalse(softened['eligible'])

    def test_marketwide_desk_has_no_special_market_exceptions(self):
        result = apply_marketwide_desk([], soft_memory=True)
        self.assertFalse(result['special_market_exceptions'])
        self.assertFalse(result['future_data_used_for_selection'])
        self.assertEqual(result['negative_memory_mode'], 'SOFT_PENALTY')

    def test_empty_comparison_never_enables_execution(self):
        result = build_soft_memory_comparison([])
        self.assertEqual(result['decision'], 'ONVOLDOENDE_DATA')
        self.assertFalse(result['execution_enabled'])
        self.assertFalse(result['live_orders_possible'])
        self.assertFalse(result['active_paper_changed'])


if __name__ == '__main__':
    unittest.main()
