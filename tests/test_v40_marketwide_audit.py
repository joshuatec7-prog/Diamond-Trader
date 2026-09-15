from v40_marketwide_audit import build_marketwide_audit


def test_marketwide_audit_uses_general_population_only():
    report = {
        'component': 'TEST_REPLAY',
        'generated_at_utc': '2026-09-15T00:00:00Z',
        'markets_requested': 100,
        'markets_completed': 99,
        'signal_summary': {
            'markets_tested': 99,
            'markets_with_signals': 90,
            'signals': 1000,
            'outcomes': {
                '15': {'samples': 1000, 'average_net_pct': -0.5, 'median_net_pct': -0.6, 'win_rate_pct': 30, 'profit_factor': 0.7},
                '1440': {'samples': 950, 'average_net_pct': 0.1, 'median_net_pct': -0.2, 'win_rate_pct': 45, 'profit_factor': 1.1},
            },
        },
        'capacity_diagnostics': {
            'overall': {'trades': 100, 'wins': 30, 'losses': 70, 'total_result_eur': -50, 'average_result_eur': -0.5, 'win_rate_pct': 30, 'profit_factor': 0.8},
            'by_route': {},
            'by_score': {
                '70_79': {'trades': 80, 'average_result_eur': -0.2, 'profit_factor': 0.9, 'win_rate_pct': 32},
                '90_PLUS': {'trades': 20, 'average_result_eur': -1.0, 'profit_factor': 0.5, 'win_rate_pct': 20},
            },
            'by_relative_strength': {},
            'by_volume_ratio': {},
            'by_btc_context': {},
        },
        'shadow_decision_desk': {
            'signals_considered': 980,
            'desk_selected': 8,
            'veto_counts': {'geheugen_vergelijkbare_situaties_negatief': 930},
            'full_period': {'trades': 8, 'total_result_eur': -20, 'profit_factor': 0.4},
            'validation': {},
            'untouched_test': {},
        },
        'large_move_audit': {
            'minimum_gain_pct': 15.0,
            'events': 200,
            'caught': 150,
            'caught_early': 70,
            'missed': 50,
        },
    }

    audit = build_marketwide_audit(report)

    assert audit['scope']['markets_tested'] == 99
    assert audit['large_move_coverage']['catch_rate_pct'] == 75.0
    assert audit['large_move_coverage']['early_capture_rate_pct'] == 35.0
    assert audit['selection_funnel']['memory_block_rate_pct'] > 90.0
    assert audit['flags']['score_separates_quality'] is False
    assert audit['flags']['capacity_profitable'] is False
    assert audit['execution_enabled'] is False
    assert audit['active_paper_changed'] is False
