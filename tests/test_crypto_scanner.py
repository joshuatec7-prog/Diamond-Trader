import unittest

from crypto_scanner import _direction_score, _sideways_score


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


if __name__ == '__main__':
    unittest.main()
