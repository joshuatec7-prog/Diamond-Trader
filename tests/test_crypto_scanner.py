import unittest
from types import SimpleNamespace

from crypto_scanner import _direction_score, _sideways_score
from crypto_scanner_v2 import _grade_action, _roundtrip_cost_pct, _taker_fee_pct


class CryptoScannerTests(unittest.TestCase):
    def test_aligned_long_scores_higher_than_misaligned(self):
        aligned = {
            'close': 110.0,
            'fast15': 106.0,
            'slow15': 100.0,
            'slope15_pct': 0.30,
            'fast1h': 105.0,
            'slow1h': 100.0,
            'one_hour_gap_pct': 0.75,
            'slope1h_pct': 0.30,
            'momentum_pct': 2.0,
            'breakout_pct': 0.50,
            'breakdown_pct': 0.0,
            'atr_pct': 1.0,
        }
        misaligned = {**aligned, 'close': 99.0, 'fast15': 99.0, 'slope15_pct': -0.10}
        good = _direction_score(aligned, 'LONG', 0.78, 0.12, 0.40)
        bad = _direction_score(misaligned, 'LONG', 0.78, 0.12, 0.40)
        self.assertGreater(good, bad)
        self.assertGreaterEqual(good, 75.0)

    def test_sideways_score_cannot_become_trade_grade(self):
        band_metrics = {
            'close': 100.0,
            'prev_close': 99.0,
            'lower_band': 99.5,
            'middle_band': 105.0,
        }
        score = _sideways_score(band_metrics, 3.0, 0.78, 0.05, 0.40)
        self.assertLessEqual(score, 70.0)
        self.assertEqual(_grade_action('SIDEWAYS', 'LONG', 99.0, 9.0, 0.01), 'GEEN TRADE')

    def test_usdc_fee_is_materially_lower_than_eur(self):
        settings = SimpleNamespace(slippage_pct=0.08)
        eur = _roundtrip_cost_pct(settings, 0.12, 'EUR')
        usdc = _roundtrip_cost_pct(settings, 0.12, 'USDC')
        self.assertEqual(_taker_fee_pct('EUR'), 0.25)
        self.assertEqual(_taker_fee_pct('USDC'), 0.05)
        self.assertAlmostEqual(eur, 0.78)
        self.assertAlmostEqual(usdc, 0.38)
        self.assertLess(usdc, eur)

    def test_trade_grade_requires_three_times_cost_room(self):
        self.assertEqual(_grade_action('BULL', 'LONG', 85.0, 3.2, 0.10), 'LONG TRADE-GRADE')
        self.assertEqual(_grade_action('BEAR', 'SHORT', 85.0, 3.2, 0.10), 'SHORT TRADE-GRADE')
        self.assertEqual(_grade_action('BULL', 'LONG', 85.0, 2.9, 0.10), 'LONG WATCH')
        self.assertNotIn('TRADE-GRADE', _grade_action('BULL', 'SKIP', 95.0, 5.0, 0.01))


if __name__ == '__main__':
    unittest.main()
