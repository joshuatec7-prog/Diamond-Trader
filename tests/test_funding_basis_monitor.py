import unittest

from funding_basis_monitor import (
    TOTAL_ROUNDTRIP_BUFFER_PCT,
    _relative_funding_pct,
    _score_candidate,
)


class FundingBasisMonitorTests(unittest.TestCase):
    def test_roundtrip_buffer_includes_both_legs(self):
        self.assertAlmostEqual(TOTAL_ROUNDTRIP_BUFFER_PCT, 0.35)

    def test_absolute_kraken_rate_is_normalized_by_index(self):
        # $0.25 funding per BTC contract on $50k index = 0.0005% per hour.
        self.assertAlmostEqual(_relative_funding_pct(0.25, 50_000.0), 0.0005)

    def test_monitor_collects_before_watch_label(self):
        score, action, net = _score_candidate(
            funding_hour_pct=0.010,
            predicted_hour_pct=0.009,
            spread_pct=0.03,
            volume_quote=50_000_000,
            basis_pct=0.10,
            history={
                'samples_24h': 10.0,
                'span_hours_24h': 2.0,
                'positive_share_24h': 1.0,
                'avg_funding_hour_pct_24h': 0.010,
            },
        )
        self.assertGreater(score, 0.0)
        self.assertGreater(net, 1.0)
        self.assertEqual(action, 'VERZAMELEN')

    def test_persistent_positive_funding_can_become_strong_watch(self):
        _, action, net = _score_candidate(
            funding_hour_pct=0.010,
            predicted_hour_pct=0.009,
            spread_pct=0.03,
            volume_quote=50_000_000,
            basis_pct=0.10,
            history={
                'samples_24h': 72.0,
                'span_hours_24h': 18.0,
                'positive_share_24h': 0.85,
                'avg_funding_hour_pct_24h': 0.008,
            },
        )
        self.assertGreaterEqual(net, 1.0)
        self.assertEqual(action, 'STERKE CARRY WATCH')

    def test_negative_funding_is_not_short_perp_carry_watch(self):
        _, action, _ = _score_candidate(
            funding_hour_pct=-0.010,
            predicted_hour_pct=-0.005,
            spread_pct=0.02,
            volume_quote=100_000_000,
            basis_pct=0.20,
            history={
                'samples_24h': 72.0,
                'span_hours_24h': 18.0,
                'positive_share_24h': 0.10,
                'avg_funding_hour_pct_24h': -0.008,
            },
        )
        self.assertEqual(action, 'VERZAMELEN')


if __name__ == '__main__':
    unittest.main()
