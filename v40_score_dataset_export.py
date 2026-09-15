from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from bitvavo_public import BitvavoPublic
from v40_human_engine import candle_features
from v40_replay import DAY_MS, replay_market, simulate_signal_trade


FIVE_MINUTE_MS = 300_000
RICH_FEATURE_KEYS = (
    'return_15m_pct',
    'return_60m_pct',
    'return_4h_pct',
    'atr_pct',
    'volume_ratio',
    'trend_up',
    'distance_to_4h_high_pct',
    'extension_atr',
    'compression_ratio',
    'higher_low',
    'positive_bars_last_6',
)


def _write_report(path_text: str, report: dict[str, Any]) -> None:
    path = Path(path_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def enrich_trade_features(candles: Sequence[Any], trade: dict[str, Any]) -> dict[str, Any]:
    """Voeg alleen kenmerken toe die op het historische signaalmoment al bekend waren."""
    signal_ms = int(trade.get('signal_ms', 0))
    rows = sorted((c for c in candles if c.is_valid), key=lambda c: c.timestamp_ms)
    signal_index = next(
        (index for index, candle in enumerate(rows) if candle.timestamp_ms == signal_ms),
        None,
    )
    if signal_index is None:
        raise ValueError('signaalmoment ontbreekt voor feature-export')
    features = candle_features(rows[max(0, signal_index - 119):signal_index + 1])
    if not bool(features.get('valid')):
        raise ValueError('onvoldoende gesloten candles voor feature-export')
    enriched = dict(trade)
    merged = dict(trade.get('entry_features') or {})
    for key in RICH_FEATURE_KEYS:
        merged[key] = features.get(key)
    enriched['entry_features'] = merged
    return enriched


def compact_trade(trade: dict[str, Any]) -> dict[str, Any]:
    events = list(trade.get('events') or [])
    closed = str(trade.get('status', '')).upper() == 'GESLOTEN' and len(events) > 1
    close_ms = int(events[-1].get('event_ms', 0)) if closed else None
    features = trade.get('entry_features') or {}
    m15 = float(features.get('return_15m_pct') or 0.0)
    m60 = float(features.get('return_60m_pct') or 0.0)
    m4h = float(features.get('return_4h_pct') or 0.0)
    exported_features = {
        key: features.get(key)
        for key in RICH_FEATURE_KEYS
    }
    exported_features['trend_up'] = bool(features.get('trend_up'))
    exported_features['higher_low'] = bool(features.get('higher_low'))
    exported_features['momentum_accel_15m_vs_60m'] = round(m15 * 4.0 - m60, 8)
    exported_features['momentum_accel_60m_vs_4h'] = round(m60 * 4.0 - m4h, 8)
    return {
        'market': str(trade.get('market', '')),
        'signal_ms': int(trade.get('signal_ms', 0)),
        'close_ms': close_ms,
        'route': str(trade.get('route', 'ONBEKEND')),
        'base_score': float(trade.get('score') or 0.0),
        'position_eur': float(trade.get('position_eur') or 0.0),
        'result_eur': float(trade.get('result_eur') or 0.0),
        'result_pct': float(trade.get('result_pct') or 0.0),
        'exit_reason': str(events[-1].get('reason', 'OPEN')) if closed else 'OPEN',
        'relative_strength_vs_btc_1h_pct': trade.get('relative_strength_vs_btc_1h_pct'),
        'net_reward_risk': trade.get('net_reward_risk'),
        'btc_return_1h_pct': trade.get('btc_return_1h_pct'),
        'entry_features': exported_features,
        'status': 'GESLOTEN' if closed else 'OPEN_EINDE_PERIODE',
    }


def run_export(
    api: BitvavoPublic,
    *,
    days: int = 90,
    end_ms: int | None = None,
    markets: Sequence[str] | None = None,
    output_path: str | None = None,
) -> dict[str, Any]:
    if days != 90:
        raise ValueError('score-dataset export vereist exact 90 dagen')
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
        raise RuntimeError('onvoldoende BTC-historie voor score-dataset export')

    candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    markets_completed = 0
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
            for signal in replay['signals']:
                trade = simulate_signal_trade(rows, signal)
                trade = enrich_trade_features(rows, trade)
                candidates.append(compact_trade(trade))
            markets_completed += 1
        except Exception as exc:
            errors.append(f'{market}: {type(exc).__name__}: {exc}')

    candidates.sort(key=lambda item: (int(item['signal_ms']), str(item['market'])))
    report = {
        'version': '4.0-score-dataset-2-rich-context',
        'component': 'V40_SCORE_CALIBRATION_DATASET',
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'period': {
            'days': days,
            'signal_start_ms': signal_start,
            'end_ms': end,
            'warmup_days': 1,
            'candle_interval': '5m',
            'split_intent': '60D_TRAINING_15D_VALIDATION_15D_UNTOUCHED_TEST',
        },
        'mode': 'OFFLINE_MEASUREMENT_ONLY',
        'execution_enabled': False,
        'live_orders_possible': False,
        'active_paper_changed': False,
        'markets_requested': len(active),
        'markets_completed': markets_completed,
        'candidates': len(candidates),
        'feature_set': list(RICH_FEATURE_KEYS) + [
            'momentum_accel_15m_vs_60m',
            'momentum_accel_60m_vs_4h',
        ],
        'errors': errors,
        'rows': candidates,
        'notes': [
            'Dit bestand exporteert uitsluitend historische PAPER-kandidaten en uitkomsten.',
            'Er wordt geen selectieregel gewijzigd en er kunnen geen orders worden geplaatst.',
            'Alle EUR-markten worden gelijk behandeld; er zijn geen munt-specifieke uitzonderingen.',
            'Extra context wordt opnieuw berekend uit uitsluitend gesloten candles tot en met het signaalmoment.',
            'De dataset is bedoeld voor een vooraf vastgelegde 60/15/15 score-herkalibratie.',
        ],
    }
    if output_path:
        _write_report(output_path, report)
    return report


def print_status(report: dict[str, Any]) -> None:
    print('=== v4.0 SCORE-CALIBRATIE DATASET EXPORT | RIJKE CONTEXT ===')
    print('UITVOERING : UIT / OFFLINE METING')
    print(f"MARKTEN    : {report['markets_completed']}/{report['markets_requested']}")
    print(f"KANDIDATEN : {report['candidates']}")
    print(f"FEATURES   : {len(report['feature_set'])}")
    print(f"FOUTEN     : {len(report['errors'])}")


def main() -> int:
    parser = argparse.ArgumentParser(description='v4.0 score-calibratie dataset export')
    parser.add_argument('--days', type=int, default=90)
    parser.add_argument('--markets', default='', help='optioneel: komma-gescheiden EUR-markten')
    parser.add_argument('--output', default='cryptobot_v40_score_dataset.json')
    args = parser.parse_args()
    api = BitvavoPublic('https://api.bitvavo.com/v2', timeout_seconds=20, retries=4)
    markets = [item.strip().upper() for item in args.markets.split(',') if item.strip()] or None
    report = run_export(api, days=args.days, markets=markets, output_path=args.output)
    print_status(report)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
