from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from bitvavo_public import BitvavoPublic
from v40_replay import (
    DAY_MS,
    SHADOW_DESK_LOSS_PAUSE_COUNT,
    SHADOW_DESK_LOSS_PAUSE_MS,
    SHADOW_DESK_MAXIMUM_NEW_TRADES_PER_DAY,
    SHADOW_DESK_MINIMUM_CONFIDENCE,
    SHADOW_DESK_MINIMUM_WINNER_MARGIN,
    _shadow_close_ms,
    _shadow_desk_review,
    _shadow_memory_key,
    replay_market,
    simulate_capacity_limited_portfolio,
    simulate_signal_trade,
    summarize_strategy_trades,
)


FIVE_MINUTE_MS = 300_000
SOFT_MEMORY_PENALTY_POINTS = 12.0
MINIMUM_ACCEPTED_TRADES = 50
REQUIRED_PROFIT_FACTOR = 1.20
MAXIMUM_DRAWDOWN_EUR = 360.0
NEGATIVE_MEMORY_VETO = 'geheugen_vergelijkbare_situaties_negatief'
NEGATIVE_MEMORY_PENALTY_REASON = 'geheugen_negatief_straf_12_punten'


def _write_report(path_text: str, report: dict[str, Any]) -> None:
    path = Path(path_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def soften_negative_memory_review(review: dict[str, Any]) -> dict[str, Any]:
    """Vervang uitsluitend de negatieve-geheugen veto door een vaste straf van 12 punten."""
    result = dict(review)
    risk_vetoes = list(review.get('risk_vetoes') or [])
    bear_reasons = list(review.get('bear_reasons') or [])
    penalty = 0.0
    if NEGATIVE_MEMORY_VETO in risk_vetoes:
        risk_vetoes = [reason for reason in risk_vetoes if reason != NEGATIVE_MEMORY_VETO]
        penalty = SOFT_MEMORY_PENALTY_POINTS
        bear_reasons.append(NEGATIVE_MEMORY_PENALTY_REASON)
    confidence = round(max(0.0, float(review.get('confidence') or 0.0) - penalty), 6)
    if confidence < SHADOW_DESK_MINIMUM_CONFIDENCE:
        bear_reasons.append('totale_bewijssterkte_onvoldoende')
    result['confidence'] = confidence
    result['risk_vetoes'] = list(dict.fromkeys(risk_vetoes))
    result['bear_reasons'] = list(dict.fromkeys(bear_reasons))
    result['memory_penalty_points'] = penalty
    result['negative_memory_mode'] = 'SOFT_PENALTY'
    result['eligible'] = not result['risk_vetoes'] and confidence >= SHADOW_DESK_MINIMUM_CONFIDENCE
    return result


def _marketwide_review(
    trade: dict[str, Any],
    memory_cases: Sequence[dict[str, Any]],
    *,
    soft_memory: bool,
) -> dict[str, Any]:
    review = _shadow_desk_review(trade, memory_cases)
    if soft_memory:
        return soften_negative_memory_review(review)
    result = dict(review)
    result['memory_penalty_points'] = 0.0
    result['negative_memory_mode'] = 'HARD_VETO'
    return result


def apply_marketwide_desk(
    trades: Sequence[dict[str, Any]],
    *,
    soft_memory: bool,
) -> dict[str, Any]:
    """Speel dezelfde beslisdesk chronologisch af over alle markten, zonder muntuitzonderingen."""
    clean = sorted(
        trades,
        key=lambda trade: (int(trade.get('signal_ms', 0)), str(trade.get('market', ''))),
    )
    grouped: dict[int, list[dict[str, Any]]] = {}
    for trade in clean:
        grouped.setdefault(int(trade.get('signal_ms', 0)), []).append(trade)

    memory_pending = sorted(
        (trade for trade in clean if _shadow_close_ms(trade) != float('inf')),
        key=lambda trade: (_shadow_close_ms(trade), int(trade.get('signal_ms', 0))),
    )
    memory_cursor = 0
    memory: dict[str, list[dict[str, Any]]] = {}
    selected: list[dict[str, Any]] = []
    selected_pending: list[dict[str, Any]] = []
    daily_entries: Counter[int] = Counter()
    vetoes: Counter[str] = Counter()
    consecutive_losses = 0
    pause_until_ms = 0
    memory_penalty_count = 0

    for signal_ms, candidates in sorted(grouped.items()):
        while (
            memory_cursor < len(memory_pending)
            and _shadow_close_ms(memory_pending[memory_cursor]) <= signal_ms
        ):
            case = memory_pending[memory_cursor]
            memory.setdefault(_shadow_memory_key(case), []).append(case)
            memory_cursor += 1

        closed_selected = [
            item for item in selected_pending if _shadow_close_ms(item) <= signal_ms
        ]
        for item in sorted(closed_selected, key=_shadow_close_ms):
            selected_pending.remove(item)
            if float(item.get('result_eur', 0.0)) < 0.0:
                consecutive_losses += 1
                if consecutive_losses >= SHADOW_DESK_LOSS_PAUSE_COUNT:
                    pause_until_ms = max(
                        pause_until_ms,
                        int(_shadow_close_ms(item)) + SHADOW_DESK_LOSS_PAUSE_MS,
                    )
                    consecutive_losses = 0
            else:
                consecutive_losses = 0

        reviewed: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for trade in candidates:
            review = _marketwide_review(
                trade,
                memory.get(_shadow_memory_key(trade), []),
                soft_memory=soft_memory,
            )
            if float(review.get('memory_penalty_points') or 0.0) > 0.0:
                memory_penalty_count += 1
            reasons = list(review.get('risk_vetoes') or [])
            if not bool(review.get('eligible')):
                reasons.extend(review.get('bear_reasons') or [])
                for reason in dict.fromkeys(reasons):
                    vetoes[str(reason)] += 1
                continue
            reviewed.append((trade, review))

        if not reviewed:
            continue

        utc_day = signal_ms // DAY_MS
        shared_veto = None
        if signal_ms < pause_until_ms:
            shared_veto = 'pauze_na_drie_afgesloten_verliezen'
        elif daily_entries[utc_day] >= SHADOW_DESK_MAXIMUM_NEW_TRADES_PER_DAY:
            shared_veto = 'daglimiet_een_keuze_bereikt'
        if shared_veto:
            vetoes[shared_veto] += len(reviewed)
            continue

        ranked = sorted(
            reviewed,
            key=lambda item: (-float(item[1]['confidence']), str(item[0].get('market', ''))),
        )
        if len(ranked) > 1:
            margin = float(ranked[0][1]['confidence']) - float(ranked[1][1]['confidence'])
            if margin < SHADOW_DESK_MINIMUM_WINNER_MARGIN:
                vetoes['geen_duidelijke_winnaar'] += len(ranked)
                continue

        winner = dict(ranked[0][0])
        winner['shadow_desk'] = ranked[0][1]
        selected.append(winner)
        if _shadow_close_ms(winner) != float('inf'):
            selected_pending.append(winner)
        daily_entries[utc_day] += 1

    return {
        'selected_trades': selected,
        'candidate_trades': len(clean),
        'desk_selected': len(selected),
        'veto_counts': dict(vetoes),
        'negative_memory_mode': 'SOFT_PENALTY' if soft_memory else 'HARD_VETO',
        'memory_penalty_points': SOFT_MEMORY_PENALTY_POINTS if soft_memory else 0.0,
        'memory_penalty_count': memory_penalty_count,
        'future_data_used_for_selection': False,
        'memory_uses_only_cases_closed_before_decision': True,
        'special_market_exceptions': False,
    }


def _strategy_report(
    trades: Sequence[dict[str, Any]],
    *,
    period_end: int,
) -> dict[str, Any]:
    test_start = period_end - 15 * DAY_MS
    validation_start = test_start - 15 * DAY_MS
    portfolio = simulate_capacity_limited_portfolio(trades)
    accepted = list(portfolio.get('accepted_trades') or [])

    def period_result(start: int | None, end: int | None) -> dict[str, Any]:
        rows = [
            trade for trade in accepted
            if (start is None or int(trade['signal_ms']) >= start)
            and (end is None or int(trade['signal_ms']) < end)
        ]
        return summarize_strategy_trades(rows)

    development = period_result(None, validation_start)
    validation = period_result(validation_start, test_start)
    untouched = period_result(test_start, None)
    full = summarize_strategy_trades(accepted)
    criteria = {
        'development_positive': development['total_result_eur'] > 0.0,
        'validation_positive': validation['total_result_eur'] > 0.0,
        'untouched_test_positive': untouched['total_result_eur'] > 0.0,
        'full_minimum_50_trades': full['trades'] >= MINIMUM_ACCEPTED_TRADES,
        'full_positive': full['total_result_eur'] > 0.0,
        'full_pf_above_1_20': (full['profit_factor'] or 0.0) > REQUIRED_PROFIT_FACTOR,
        'full_drawdown_within_360_eur': (
            float(portfolio['maximum_realized_drawdown_eur']) <= MAXIMUM_DRAWDOWN_EUR
        ),
    }
    selected_audit = [
        {
            'market': str(trade.get('market', '')),
            'signal_ms': int(trade.get('signal_ms', 0)),
            'route': str(trade.get('route', '')),
            'score': float(trade.get('score', 0.0)),
            'result_eur': float(trade.get('result_eur', 0.0)),
            'result_pct': float(trade.get('result_pct') or 0.0),
            'entry_price': float(trade.get('entry_price') or 0.0),
            'exit_reason': str(trade.get('exit_reason', 'OPEN')),
            'desk_review': trade.get('selection_review', {}),
        }
        for trade in accepted
    ]
    return {
        'portfolio_accepted': int(portfolio['trades_accepted']),
        'portfolio_rejected': int(portfolio['trades_rejected']),
        'development': development,
        'validation': validation,
        'untouched_test': untouched,
        'full_period': full,
        'maximum_realized_drawdown_eur': float(portfolio['maximum_realized_drawdown_eur']),
        'selected_trade_audit': selected_audit,
        'criteria': criteria,
        'passes_all_criteria': all(criteria.values()),
    }


def build_soft_memory_comparison(trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    clean = sorted(
        trades,
        key=lambda trade: (int(trade.get('signal_ms', 0)), str(trade.get('market', ''))),
    )
    if not clean:
        return {
            'decision': 'ONVOLDOENDE_DATA',
            'execution_enabled': False,
            'live_orders_possible': False,
            'active_paper_changed': False,
        }

    period_end = max(int(trade.get('signal_ms', 0)) for trade in clean) + 1
    hard_desk = apply_marketwide_desk(clean, soft_memory=False)
    soft_desk = apply_marketwide_desk(clean, soft_memory=True)
    hard = _strategy_report(hard_desk['selected_trades'], period_end=period_end)
    soft = _strategy_report(soft_desk['selected_trades'], period_end=period_end)
    hard['desk_selected'] = hard_desk['desk_selected']
    hard['signals_considered'] = hard_desk['candidate_trades']
    hard['veto_counts'] = hard_desk['veto_counts']
    hard['memory_penalty_count'] = 0
    soft['desk_selected'] = soft_desk['desk_selected']
    soft['signals_considered'] = soft_desk['candidate_trades']
    soft['veto_counts'] = soft_desk['veto_counts']
    soft['memory_penalty_count'] = soft_desk['memory_penalty_count']

    hard_total = float(hard['full_period']['total_result_eur'])
    soft_total = float(soft['full_period']['total_result_eur'])
    hard_pf = float(hard['full_period']['profit_factor'] or 0.0)
    soft_pf = float(soft['full_period']['profit_factor'] or 0.0)
    soft_beats_hard = soft_total > hard_total and soft_pf > hard_pf
    decision = (
        'KANDIDAAT_VOOR_VOLGENDE_SHADOW_FASE'
        if soft['passes_all_criteria'] and soft_beats_hard
        else 'AFWIJZEN'
    )
    return {
        'method': 'MARKTBREDE_AB_TEST_NEGATIEF_GEHEUGEN',
        'one_change_only': True,
        'change': {
            'from': 'NEGATIEF_GEHEUGEN_HARD_VETO',
            'to': 'NEGATIEF_GEHEUGEN_MIN_12_CONFIDENCE',
            'penalty_points': SOFT_MEMORY_PENALTY_POINTS,
            'other_hard_safety_vetoes_unchanged': True,
            'special_market_exceptions': False,
        },
        'period_boundaries_ms': {
            'validation_start': period_end - 30 * DAY_MS,
            'untouched_test_start': period_end - 15 * DAY_MS,
            'period_end': period_end,
        },
        'hard_memory_control': hard,
        'soft_memory_challenger': soft,
        'comparison': {
            'soft_minus_hard_selected': soft['desk_selected'] - hard['desk_selected'],
            'soft_minus_hard_total_result_eur': round(soft_total - hard_total, 8),
            'soft_minus_hard_profit_factor': round(soft_pf - hard_pf, 6),
            'soft_beats_hard_on_total_and_pf': soft_beats_hard,
        },
        'criteria': {
            'minimum_accepted_trades': MINIMUM_ACCEPTED_TRADES,
            'required_profit_factor_above': REQUIRED_PROFIT_FACTOR,
            'maximum_drawdown_eur': MAXIMUM_DRAWDOWN_EUR,
            'development_validation_test_must_all_be_positive': True,
        },
        'decision': decision,
        'active_paper_changed': False,
        'execution_enabled': False,
        'live_orders_possible': False,
        'future_data_used_for_selection': False,
    }


def run_lab(
    api: BitvavoPublic,
    *,
    days: int = 90,
    end_ms: int | None = None,
    markets: Sequence[str] | None = None,
    output_path: str | None = None,
) -> dict[str, Any]:
    if not 7 <= days <= 90:
        raise ValueError('days moet tussen 7 en 90 liggen')
    end = int(time.time() * 1000) if end_ms is None else int(end_ms)
    end = end // FIVE_MINUTE_MS * FIVE_MINUTE_MS
    signal_start = end - days * DAY_MS
    fetch_start = signal_start - DAY_MS
    active = sorted(set(markets or api.trading_markets('EUR')))
    if 'BTC-EUR' not in active:
        active.append('BTC-EUR')
        active.sort()

    btc = api.closed_candles_between('BTC-EUR', '5m', fetch_start, end, now_ms=end)
    if len(btc) < 288:
        raise RuntimeError('onvoldoende BTC-historie voor brede replay')

    paper_trades: list[dict[str, Any]] = []
    errors: list[str] = []
    markets_completed = 0
    signals = 0
    for market in active:
        try:
            rows = btc if market == 'BTC-EUR' else api.closed_candles_between(
                market, '5m', fetch_start, end, now_ms=end,
            )
            replay = replay_market(
                market,
                rows,
                btc,
                assumed_spread_pct=.12,
                signal_start_ms=signal_start,
            )
            signals += len(replay['signals'])
            paper_trades.extend(
                simulate_signal_trade(rows, signal) for signal in replay['signals']
            )
            markets_completed += 1
        except Exception as exc:
            errors.append(f'{market}: {type(exc).__name__}: {exc}')

    comparison = build_soft_memory_comparison(paper_trades)
    report = {
        'version': '4.0-soft-memory-1',
        'component': 'V40_SOFT_MEMORY_CHALLENGER',
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'period': {
            'days': days,
            'signal_start_ms': signal_start,
            'end_ms': end,
            'warmup_days': 1,
            'candle_interval': '5m',
        },
        'mode': 'OFFLINE_REPLAY_ONLY',
        'execution_enabled': False,
        'live_orders_possible': False,
        'markets_requested': len(active),
        'markets_completed': markets_completed,
        'signals': signals,
        'paper_trades': len(paper_trades),
        'errors': errors,
        'comparison': comparison,
        'notes': [
            'Exact één beslisregel verandert: negatief geheugen gaat van hard veto naar -12 confidence.',
            'Alle overige harde veiligheidsvetoes, daglimiet, verliespauze en winnaar-marge blijven gelijk.',
            'Alle EUR-markten worden gelijk behandeld; er zijn geen munt-specifieke uitzonderingen.',
            'De vergelijking gebruikt uitsluitend informatie die op het beslismoment beschikbaar was.',
            'Deze labrun wijzigt de actieve PAPER-bot niet en kan geen live orders plaatsen.',
        ],
    }
    if output_path:
        _write_report(output_path, report)
    return report


def print_status(report: dict[str, Any]) -> None:
    comparison = report['comparison']
    hard = comparison.get('hard_memory_control') or {}
    soft = comparison.get('soft_memory_challenger') or {}
    hard_full = hard.get('full_period') or {}
    soft_full = soft.get('full_period') or {}
    print('=== v4.0 MARKTBREDE SOFT-MEMORY CHALLENGER ===')
    print('UITVOERING           : UIT / OFFLINE REPLAY ONLY')
    print(f"MARKTEN              : {report['markets_completed']}/{report['markets_requested']}")
    print(f"SIGNALEN             : {report['signals']}")
    print(
        f"HARD MEMORY          : {hard.get('desk_selected', 0)} desk | "
        f"{hard_full.get('trades', 0)} trades | €{float(hard_full.get('total_result_eur') or 0.0):.2f} "
        f"| PF {hard_full.get('profit_factor')}"
    )
    print(
        f"SOFT MEMORY          : {soft.get('desk_selected', 0)} desk | "
        f"{soft_full.get('trades', 0)} trades | €{float(soft_full.get('total_result_eur') or 0.0):.2f} "
        f"| PF {soft_full.get('profit_factor')}"
    )
    print(f"MEMORY STRAFPUNTEN   : {soft.get('memory_penalty_count', 0)} kandidaten")
    print(f"BESLUIT              : {comparison.get('decision')}")
    if report['errors']:
        print(f"DATAPROBLEMEN        : {len(report['errors'])}")


def main() -> int:
    parser = argparse.ArgumentParser(description='v4.0 marktbrede soft-memory challenger')
    parser.add_argument('--days', type=int, default=90)
    parser.add_argument('--markets', default='', help='optioneel: komma-gescheiden EUR-markten')
    parser.add_argument('--output', default='cryptobot_v40_soft_memory_challenger.json')
    args = parser.parse_args()
    api = BitvavoPublic('https://api.bitvavo.com/v2', timeout_seconds=20, retries=4)
    markets = [item.strip().upper() for item in args.markets.split(',') if item.strip()] or None
    report = run_lab(api, days=args.days, markets=markets, output_path=args.output)
    print_status(report)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
