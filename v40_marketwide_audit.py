from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _pct(part: float, whole: float) -> float | None:
    if not whole:
        return None
    return round((part / whole) * 100.0, 3)


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def build_marketwide_audit(report: dict[str, Any]) -> dict[str, Any]:
    signals = report.get('signal_summary') or {}
    capacity = report.get('capacity_diagnostics') or {}
    shadow = report.get('shadow_decision_desk') or {}
    large_moves = report.get('large_move_audit') or {}

    total_signals = int(signals.get('signals') or 0)
    signals_considered = int(shadow.get('signals_considered') or 0)
    desk_selected = int(shadow.get('desk_selected') or 0)
    memory_blocked = int((shadow.get('veto_counts') or {}).get('geheugen_vergelijkbare_situaties_negatief') or 0)

    large_events = int(large_moves.get('events') or 0)
    caught = int(large_moves.get('caught') or 0)
    caught_early = int(large_moves.get('caught_early') or 0)
    missed = int(large_moves.get('missed') or 0)

    capacity_overall = capacity.get('overall') or {}
    capacity_pf = _safe_float(capacity_overall.get('profit_factor'))

    score_bands = capacity.get('by_score') or {}
    score_quality = []
    for band, row in score_bands.items():
        score_quality.append(
            {
                'band': band,
                'trades': int(row.get('trades') or 0),
                'average_result_eur': _safe_float(row.get('average_result_eur')),
                'profit_factor': _safe_float(row.get('profit_factor')),
                'win_rate_pct': _safe_float(row.get('win_rate_pct')),
            }
        )

    horizon_outcomes = []
    for horizon, row in (signals.get('outcomes') or {}).items():
        horizon_outcomes.append(
            {
                'minutes': int(horizon),
                'samples': int(row.get('samples') or 0),
                'average_net_pct': _safe_float(row.get('average_net_pct')),
                'median_net_pct': _safe_float(row.get('median_net_pct')),
                'win_rate_pct': _safe_float(row.get('win_rate_pct')),
                'profit_factor': _safe_float(row.get('profit_factor')),
            }
        )
    horizon_outcomes.sort(key=lambda row: row['minutes'])

    score_70 = score_bands.get('70_79') or {}
    score_90 = score_bands.get('90_PLUS') or {}
    avg_70 = _safe_float(score_70.get('average_result_eur'))
    avg_90 = _safe_float(score_90.get('average_result_eur'))
    score_separates_quality = None
    if avg_70 is not None and avg_90 is not None:
        score_separates_quality = avg_90 > avg_70

    memory_block_rate = _pct(memory_blocked, signals_considered)
    selected_rate = _pct(desk_selected, signals_considered)
    early_capture_rate = _pct(caught_early, large_events)
    catch_rate = _pct(caught, large_events)
    miss_rate = _pct(missed, large_events)

    flags = {
        'capacity_profitable': bool(capacity_pf is not None and capacity_pf > 1.0),
        'score_separates_quality': score_separates_quality,
        'memory_blocks_more_than_90_pct': bool(memory_block_rate is not None and memory_block_rate > 90.0),
        'desk_selects_less_than_1_pct': bool(selected_rate is not None and selected_rate < 1.0),
        'early_large_move_capture_below_50_pct': bool(early_capture_rate is not None and early_capture_rate < 50.0),
    }

    priorities: list[str] = []
    if not flags['capacity_profitable']:
        priorities.append('selectiekwaliteit_voor_nieuwe_strategie')
    if flags['score_separates_quality'] is False:
        priorities.append('score_opnieuw_kalibreren')
    if flags['memory_blocks_more_than_90_pct']:
        priorities.append('geheugen_van_hard_veto_naar_zachte_bewijskracht_onderzoeken')
    if flags['early_large_move_capture_below_50_pct']:
        priorities.append('vroegere_bevestiging_en_timing_onderzoeken')
    priorities.append('afgewezen_kandidaten_marktbreed_met_15m_1u_4u_24u_resultaat_vergelijken')

    return {
        'component': 'V40_MARKETWIDE_AUDIT',
        'source_component': report.get('component'),
        'source_generated_at_utc': report.get('generated_at_utc'),
        'mode': 'ANALYSE_ONLY',
        'execution_enabled': False,
        'live_orders_possible': False,
        'scope': {
            'markets_requested': report.get('markets_requested'),
            'markets_completed': report.get('markets_completed'),
            'markets_tested': signals.get('markets_tested'),
            'markets_with_signals': signals.get('markets_with_signals'),
            'signals': total_signals,
        },
        'large_move_coverage': {
            'minimum_gain_pct': large_moves.get('minimum_gain_pct'),
            'events': large_events,
            'caught': caught,
            'caught_early': caught_early,
            'missed': missed,
            'catch_rate_pct': catch_rate,
            'early_capture_rate_pct': early_capture_rate,
            'miss_rate_pct': miss_rate,
        },
        'selection_funnel': {
            'signals_considered': signals_considered,
            'desk_selected': desk_selected,
            'selected_rate_pct': selected_rate,
            'memory_blocked': memory_blocked,
            'memory_block_rate_pct': memory_block_rate,
        },
        'capacity_overall': capacity_overall,
        'by_route': capacity.get('by_route') or {},
        'by_score': score_quality,
        'by_relative_strength': capacity.get('by_relative_strength') or {},
        'by_volume_ratio': capacity.get('by_volume_ratio') or {},
        'by_btc_context': capacity.get('by_btc_context') or {},
        'signal_horizon_outcomes': horizon_outcomes,
        'shadow_full_period': shadow.get('full_period') or {},
        'shadow_validation': shadow.get('validation') or {},
        'shadow_untouched_test': shadow.get('untouched_test') or {},
        'flags': flags,
        'priorities': priorities,
        'decision': 'ANALYSEER_SELECTIE_EN_TIMING_MARKTBREED',
        'active_paper_changed': False,
    }


def render_markdown(audit: dict[str, Any]) -> str:
    scope = audit['scope']
    moves = audit['large_move_coverage']
    funnel = audit['selection_funnel']
    cap = audit['capacity_overall']
    shadow = audit['shadow_full_period']
    flags = audit['flags']

    lines = [
        '## Marktbrede v4.0 audit',
        f"- Markten getest: {scope.get('markets_tested')} | met signalen: {scope.get('markets_with_signals')}",
        f"- Signalen: {scope.get('signals')}",
        f"- Grote bewegingen >= {moves.get('minimum_gain_pct')}%: {moves.get('events')}",
        f"- Gezien: {moves.get('caught')} ({moves.get('catch_rate_pct')}%)",
        f"- Vroeg gezien: {moves.get('caught_early')} ({moves.get('early_capture_rate_pct')}%)",
        f"- Gemist: {moves.get('missed')} ({moves.get('miss_rate_pct')}%)",
        f"- Desk geselecteerd: {funnel.get('desk_selected')}/{funnel.get('signals_considered')} ({funnel.get('selected_rate_pct')}%)",
        f"- Door geheugen geblokkeerd: {funnel.get('memory_blocked')} ({funnel.get('memory_block_rate_pct')}%)",
        f"- Capaciteit: {cap.get('trades')} trades | EUR {float(cap.get('total_result_eur') or 0):.2f} | PF {cap.get('profit_factor')}",
        f"- Shadow Desk: {shadow.get('trades')} trades | EUR {float(shadow.get('total_result_eur') or 0):.2f} | PF {shadow.get('profit_factor')}",
        f"- Score onderscheidt kwaliteit: {flags.get('score_separates_quality')}",
        '- Geen enkele munt krijgt een aparte uitzonderingsstatus in deze audit.',
        '- Besluit: ANALYSEER_SELECTIE_EN_TIMING_MARKTBREED',
    ]
    return '\n'.join(lines) + '\n'


def main() -> int:
    parser = argparse.ArgumentParser(description='Maak een marktbrede audit van de v4.0 historische replay.')
    parser.add_argument('--report', default='cryptobot_v40_historical_replay.json')
    parser.add_argument('--output', default='cryptobot_v40_marketwide_audit.json')
    parser.add_argument('--markdown', default='cryptobot_v40_marketwide_audit.md')
    args = parser.parse_args()

    report = json.loads(Path(args.report).read_text(encoding='utf-8'))
    audit = build_marketwide_audit(report)
    Path(args.output).write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    Path(args.markdown).write_text(render_markdown(audit), encoding='utf-8')
    print(render_markdown(audit), end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
