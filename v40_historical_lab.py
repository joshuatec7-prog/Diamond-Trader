from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from bitvavo_public import BitvavoPublic
from v40_replay import (
    DAY_MS,
    audit_large_moves,
    build_runner_validation,
    build_capacity_validation,
    replay_market,
    simulate_broad_runner_trade,
    simulate_signal_trade,
    summarize_replays,
)


FIVE_MINUTE_MS = 300_000


def _write_report(path_text: str, report: dict[str, Any]) -> None:
    path = Path(path_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def run_historical_lab(
    api: BitvavoPublic,
    *,
    days: int = 30,
    end_ms: int | None = None,
    markets: Sequence[str] | None = None,
    output_path: str | None = None,
) -> dict[str, Any]:
    if not 7 <= days <= 90:
        raise ValueError('days moet tussen 7 en 90 liggen')
    end = int(time.time() * 1000) if end_ms is None else int(end_ms)
    end = end // FIVE_MINUTE_MS * FIVE_MINUTE_MS
    signal_start = end - days * DAY_MS
    fetch_start = signal_start - DAY_MS  # Eén volledige dag zonder voorkennis opwarmen.
    active = sorted(set(markets or api.trading_markets('EUR')))
    if 'BTC-EUR' not in active:
        active.append('BTC-EUR')
        active.sort()
    btc = api.closed_candles_between(
        'BTC-EUR', '5m', fetch_start, end, now_ms=end,
    )
    if len(btc) < 288:
        raise RuntimeError('onvoldoende BTC-historie voor brede replay')

    replays = []
    audits = []
    errors = []
    for market in active:
        try:
            rows = btc if market == 'BTC-EUR' else api.closed_candles_between(
                market, '5m', fetch_start, end, now_ms=end,
            )
            replay = replay_market(
                market, rows, btc, assumed_spread_pct=.12,
                signal_start_ms=signal_start,
            )
            replay['paper_trades'] = [
                simulate_signal_trade(rows, signal) for signal in replay['signals']
            ]
            replay['runner_trades'] = [
                simulate_broad_runner_trade(rows, signal) for signal in replay['signals']
            ]
            replays.append(replay)
            audits.append(audit_large_moves(
                market,
                [row for row in rows if row.timestamp_ms >= signal_start],
                replay['signals'],
            ))
        except Exception as exc:
            errors.append(f'{market}: {type(exc).__name__}: {exc}')

    summary = summarize_replays(replays)
    paper_trades = [trade for replay in replays for trade in replay['paper_trades']]
    runner_trades = [trade for replay in replays for trade in replay['runner_trades']]
    paper_results = [float(trade['result_eur']) for trade in paper_trades]
    paper_summary = {
        'trades': len(paper_trades),
        'wins': sum(1 for value in paper_results if value > 0.0),
        'losses': sum(1 for value in paper_results if value <= 0.0),
        'total_result_eur': round(sum(paper_results), 8),
        'average_result_eur': round(sum(paper_results) / len(paper_results), 8)
        if paper_results else None,
    }
    events = [event for audit in audits for event in audit['events']]
    events.sort(key=lambda event: (-float(event['gain_to_peak_pct']), str(event['market'])))
    large_move_summary = {
        'minimum_gain_pct': 15.0,
        'events': len(events),
        'caught': sum(1 for event in events if event['caught']),
        'caught_early': sum(1 for event in events if event['caught_early']),
        'missed': sum(1 for event in events if not event['caught']),
        'top_events': events[:100],
    }
    controls = {
        market: {
            'signals': next(
                (replay['signals'] for replay in replays if replay['market'] == market), []
            ),
            'large_moves': next(
                (audit['events'] for audit in audits if audit['market'] == market), []
            ),
            'paper_trades': next(
                (replay['paper_trades'] for replay in replays if replay['market'] == market), []
            ),
            'runner_trades': next(
                (replay['runner_trades'] for replay in replays if replay['market'] == market), []
            ),
        }
        for market in ('VTHO-EUR', 'LSK-EUR')
    }
    report = {
        'version': '4.0-phase-6',
        'component': 'FULL_EUR_HISTORICAL_REPLAY',
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
        'raw_candles_saved': False,
        'markets_requested': len(active),
        'markets_completed': len(replays),
        'errors': errors,
        'signal_summary': summary,
        'paper_trade_summary': paper_summary,
        'runner_validation': build_runner_validation(paper_trades, runner_trades),
        'capacity_validation': build_capacity_validation(paper_trades),
        'large_move_audit': large_move_summary,
        'control_cases': controls,
        'notes': [
            'Iedere beslissing gebruikt uitsluitend gesloten candles tot dat moment.',
            'VTHO en LSK zijn controles; de regels zijn voor alle markten identiek.',
            'Resultaten zijn inclusief 0,66% vaste roundtripkosten en 0,12% aangenomen spread.',
            'De runner is uitsluitend offline vergeleken en wijzigt de actieve PAPER-bot niet.',
            'De runner moet ook zonder LSK positief zijn en de normale route verslaan.',
        ],
    }
    if output_path:
        _write_report(output_path, report)
    return report


def print_status(report: dict[str, Any]) -> None:
    signals = report['signal_summary']
    paper = report['paper_trade_summary']
    moves = report['large_move_audit']
    runner = report['runner_validation']
    capacity = report['capacity_validation']
    print('=== CRYPTOBOT v4.0 FASE 2 | BREDE HISTORISCHE REPLAY ===')
    print('UITVOERING             : UIT / TECHNISCH ONMOGELIJK')
    print(f"PERIODE                : {report['period']['days']} dagen + 1 dag opwarming")
    print(f"MARKTEN                : {report['markets_completed']}/{report['markets_requested']}")
    print(f"SIGNALEN               : {signals['signals']} op {signals['markets_with_signals']} markten")
    print(
        f"PAPER-TRADES           : {paper['trades']} | winst {paper['wins']}"
        f" | verlies {paper['losses']} | totaal €{paper['total_result_eur']:.2f}"
    )
    print(
        f"GROTE STIJGINGEN       : {moves['events']} | vroeg gezien {moves['caught_early']}"
        f" | gemist {moves['missed']}"
    )
    without_lsk = runner['without_lsk']
    print(
        f"RUNNER ZONDER LSK      : {without_lsk['runner']['trades']} trades"
        f" | totaal €{without_lsk['runner']['total_result_eur']:.2f}"
        f" | verschil €{without_lsk['runner_minus_baseline_eur']:.2f}"
    )
    print(f"RUNNERBESLUIT          : {runner['decision']}")
    constrained = capacity['without_lsk']
    print(
        f"MENSELIJKE PORTEFEUILLE: {constrained['trades_accepted']} genomen"
        f" | {constrained['trades_rejected']} afgewezen"
        f" | totaal €{constrained['total_result_eur']:.2f}"
    )
    print(f"PORTEFEUILLEBESLUIT    : {capacity['decision']}")
    for market, control in report['control_cases'].items():
        print(
            f"{market:<22}: {len(control['signals'])} signalen"
            f" | {len(control['large_moves'])} grote bewegingen"
        )
        for trade in control['paper_trades']:
            moment = datetime.fromtimestamp(int(trade['signal_ms']) / 1000, timezone.utc)
            print(
                f"  KOOP {moment.isoformat()} | €{trade['entry_price']:.8f}"
                f" | positie €{trade['position_eur']:.0f} | resultaat €{trade['result_eur']:.2f}"
            )
            for event in trade['events'][1:]:
                event_time = datetime.fromtimestamp(int(event['event_ms']) / 1000, timezone.utc)
                print(
                    f"    {event['action']} {event_time.isoformat()}"
                    f" | €{event['price']:.8f} | {event['reason']}"
                )
    if report['errors']:
        print(f"DATAPROBLEMEN          : {len(report['errors'])}")


def main() -> int:
    parser = argparse.ArgumentParser(description='CryptoBot v4.0 brede historische replay')
    parser.add_argument('--days', type=int, default=30)
    parser.add_argument('--markets', default='', help='optioneel: komma-gescheiden EUR-markten')
    parser.add_argument('--output', default='data/cryptobot_v40_historical_lab.json')
    args = parser.parse_args()
    api = BitvavoPublic('https://api.bitvavo.com/v2', timeout_seconds=20, retries=4)
    markets = [item.strip().upper() for item in args.markets.split(',') if item.strip()] or None
    report = run_historical_lab(
        api, days=args.days, markets=markets, output_path=args.output,
    )
    print_status(report)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
