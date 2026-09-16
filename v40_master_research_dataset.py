from __future__ import annotations

import argparse
import bisect
import json
import math
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Sequence

from bitvavo_public import BitvavoPublic
from v40_human_engine import ROUNDTRIP_FIXED_COST_PCT
from v40_replay import DAY_MS, replay_market, simulate_signal_trade
from v40_score_dataset_export import compact_trade, enrich_trade_features


FIVE_MINUTE_MS = 300_000
LABEL_HORIZONS_MINUTES = (15, 60, 240, 480, 720, 1440, 2160, 2880)
LABEL_TAIL_MINUTES = max(LABEL_HORIZONS_MINUTES)
ASSUMED_SPREAD_PCT = 0.12

EARLY_FEATURE_KEYS = (
    'return_5m_pct',
    'return_10m_pct',
    'return_30m_pct',
    'momentum_accel_5m_vs_15m',
    'momentum_accel_10m_vs_30m',
    'volume_accel_ratio',
)

EARLY_MARKET_CONTEXT_KEYS = (
    'markets_used',
    'breadth_positive_5m_pct',
    'breadth_positive_10m_pct',
    'breadth_positive_15m_pct',
    'breadth_positive_1h_pct',
    'breadth_positive_4h_pct',
    'breadth_strong_5m_pct',
    'breadth_weak_5m_pct',
    'breadth_accelerating_5m_pct',
    'mean_return_5m_pct',
    'mean_return_10m_pct',
    'mean_return_15m_pct',
    'mean_return_1h_pct',
    'mean_return_4h_pct',
    'dispersion_return_5m_pct',
    'dispersion_return_1h_pct',
    'breadth_change_5m_pp',
)

CROSS_SECTION_KEYS = (
    'simultaneous_candidate_count',
    'rank_base_score',
    'rank_return_5m',
    'rank_accel_5m',
    'rank_relative_strength_1h',
    'rank_volume_accel',
)


def _write_report(path_text: str, report: dict[str, Any]) -> None:
    path = Path(path_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def _pct(new: float, old: float) -> float:
    return (new / old - 1.0) * 100.0 if new > 0.0 and old > 0.0 else 0.0


def early_candidate_features(candles: Sequence[Any], signal_ms: int) -> dict[str, float]:
    rows = sorted((c for c in candles if c.is_valid), key=lambda c: c.timestamp_ms)
    index = next((i for i, c in enumerate(rows) if int(c.timestamp_ms) == int(signal_ms)), None)
    if index is None or index < 6:
        raise ValueError('onvoldoende candles voor vroege kandidaatfeatures')
    latest = float(rows[index].close)
    r5 = _pct(latest, float(rows[index - 1].close))
    r10 = _pct(latest, float(rows[index - 2].close))
    r15 = _pct(latest, float(rows[index - 3].close))
    r30 = _pct(latest, float(rows[index - 6].close))
    recent_vol = mean(float(c.volume) for c in rows[index - 1:index + 1])
    prior_slice = rows[max(0, index - 7):index - 1]
    prior_vol = mean(float(c.volume) for c in prior_slice) if prior_slice else 0.0
    return {
        'return_5m_pct': round(r5, 8),
        'return_10m_pct': round(r10, 8),
        'return_30m_pct': round(r30, 8),
        'momentum_accel_5m_vs_15m': round(r5 * 3.0 - r15, 8),
        'momentum_accel_10m_vs_30m': round(r10 * 3.0 - r30, 8),
        'volume_accel_ratio': round(recent_vol / prior_vol, 8) if prior_vol > 0.0 else 0.0,
    }


def accumulate_early_market_context(candles: Sequence[Any], signal_start_ms: int, signal_end_ms: int, aggregates: dict[int, dict[str, float]]) -> None:
    rows = sorted((c for c in candles if c.is_valid), key=lambda c: c.timestamp_ms)
    closes = {int(c.timestamp_ms): float(c.close) for c in rows}
    for candle in rows:
        ts = int(candle.timestamp_ms)
        if ts < signal_start_ms or ts >= signal_end_ms:
            continue
        p5 = closes.get(ts - FIVE_MINUTE_MS)
        p10 = closes.get(ts - 2 * FIVE_MINUTE_MS)
        p15 = closes.get(ts - 3 * FIVE_MINUTE_MS)
        p60 = closes.get(ts - 12 * FIVE_MINUTE_MS)
        p4h = closes.get(ts - 48 * FIVE_MINUTE_MS)
        if None in (p5, p10, p15, p60, p4h):
            continue
        close = float(candle.close)
        r5 = _pct(close, float(p5))
        r10 = _pct(close, float(p10))
        r15 = _pct(close, float(p15))
        r60 = _pct(close, float(p60))
        r4h = _pct(close, float(p4h))
        agg = aggregates.setdefault(ts, defaultdict(float))
        agg['count'] += 1.0
        agg['positive_5m'] += 1.0 if r5 > 0.0 else 0.0
        agg['positive_10m'] += 1.0 if r10 > 0.0 else 0.0
        agg['positive_15m'] += 1.0 if r15 > 0.0 else 0.0
        agg['positive_1h'] += 1.0 if r60 > 0.0 else 0.0
        agg['positive_4h'] += 1.0 if r4h > 0.0 else 0.0
        agg['strong_5m'] += 1.0 if r5 >= 0.50 else 0.0
        agg['weak_5m'] += 1.0 if r5 <= -0.50 else 0.0
        agg['accelerating_5m'] += 1.0 if r5 * 3.0 > r15 else 0.0
        agg['sum_5m'] += r5
        agg['sum_10m'] += r10
        agg['sum_15m'] += r15
        agg['sum_1h'] += r60
        agg['sum_4h'] += r4h
        agg['sum_sq_5m'] += r5 * r5
        agg['sum_sq_1h'] += r60 * r60


def finalize_early_market_context(aggregates: dict[int, dict[str, float]]) -> dict[int, dict[str, float | int]]:
    result: dict[int, dict[str, float | int]] = {}
    for ts, agg in sorted(aggregates.items()):
        count = int(agg.get('count', 0.0))
        if count <= 0:
            continue
        mean5 = agg['sum_5m'] / count
        mean1h = agg['sum_1h'] / count
        var5 = max(0.0, agg['sum_sq_5m'] / count - mean5 * mean5)
        var1h = max(0.0, agg['sum_sq_1h'] / count - mean1h * mean1h)
        result[int(ts)] = {
            'markets_used': count,
            'breadth_positive_5m_pct': round(agg['positive_5m'] / count * 100.0, 6),
            'breadth_positive_10m_pct': round(agg['positive_10m'] / count * 100.0, 6),
            'breadth_positive_15m_pct': round(agg['positive_15m'] / count * 100.0, 6),
            'breadth_positive_1h_pct': round(agg['positive_1h'] / count * 100.0, 6),
            'breadth_positive_4h_pct': round(agg['positive_4h'] / count * 100.0, 6),
            'breadth_strong_5m_pct': round(agg['strong_5m'] / count * 100.0, 6),
            'breadth_weak_5m_pct': round(agg['weak_5m'] / count * 100.0, 6),
            'breadth_accelerating_5m_pct': round(agg['accelerating_5m'] / count * 100.0, 6),
            'mean_return_5m_pct': round(mean5, 8),
            'mean_return_10m_pct': round(agg['sum_10m'] / count, 8),
            'mean_return_15m_pct': round(agg['sum_15m'] / count, 8),
            'mean_return_1h_pct': round(mean1h, 8),
            'mean_return_4h_pct': round(agg['sum_4h'] / count, 8),
            'dispersion_return_5m_pct': round(math.sqrt(var5), 8),
            'dispersion_return_1h_pct': round(math.sqrt(var1h), 8),
            'breadth_change_5m_pp': 0.0,
        }
    for ts, item in result.items():
        previous = result.get(ts - FIVE_MINUTE_MS)
        if previous is not None:
            item['breadth_change_5m_pp'] = round(float(item['breadth_positive_5m_pct']) - float(previous['breadth_positive_5m_pct']), 6)
    return result


def forward_path_labels(candles: Sequence[Any], signal: dict[str, Any], *, spread_pct: float = ASSUMED_SPREAD_PCT, horizons_minutes: Sequence[int] = LABEL_HORIZONS_MINUTES) -> dict[str, dict[str, float | int | None]]:
    rows = sorted((c for c in candles if c.is_valid), key=lambda c: c.timestamp_ms)
    timestamps = [int(c.timestamp_ms) for c in rows]
    signal_ms = int(signal['signal_ms'])
    start = bisect.bisect_left(timestamps, signal_ms)
    if start >= len(rows) or timestamps[start] != signal_ms:
        raise ValueError('signaalmoment ontbreekt voor forward labels')
    entry = float(signal.get('entry_reference', rows[start].close))
    roundtrip_cost = ROUNDTRIP_FIXED_COST_PCT + max(0.0, float(spread_pct))
    result: dict[str, dict[str, float | int | None]] = {}
    for horizon in horizons_minutes:
        target_ms = signal_ms + int(horizon) * 60_000
        mature = timestamps[-1] >= target_ms
        if not mature:
            result[str(horizon)] = {'mature': 0, 'gross_close_pct': None, 'net_close_pct': None, 'mfe_pct': None, 'mae_pct': None}
            continue
        target_index = bisect.bisect_right(timestamps, target_ms) - 1
        target_index = max(start, target_index)
        window = rows[start + 1:target_index + 1]
        close_at_horizon = float(rows[target_index].close)
        gross = _pct(close_at_horizon, entry)
        mfe = max((_pct(float(c.high), entry) for c in window), default=0.0)
        mae = min((_pct(float(c.low), entry) for c in window), default=0.0)
        result[str(horizon)] = {'mature': 1, 'gross_close_pct': round(gross, 6), 'net_close_pct': round(gross - roundtrip_cost, 6), 'mfe_pct': round(mfe, 6), 'mae_pct': round(mae, 6)}
    return result


def barrier_label_48h(candles: Sequence[Any], signal: dict[str, Any], *, spread_pct: float = ASSUMED_SPREAD_PCT) -> dict[str, float | int | str | None]:
    rows = sorted((c for c in candles if c.is_valid), key=lambda c: c.timestamp_ms)
    signal_ms = int(signal['signal_ms'])
    entry = float(signal['entry_reference'])
    stop = float(signal['stop_reference'])
    target = float(signal['target_reference'])
    limit_ms = signal_ms + LABEL_TAIL_MINUTES * 60_000
    roundtrip_cost = ROUNDTRIP_FIXED_COST_PCT + max(0.0, float(spread_pct))
    for candle in rows:
        ts = int(candle.timestamp_ms)
        if ts <= signal_ms:
            continue
        if ts > limit_ms:
            break
        if float(candle.low) <= stop:
            gross = _pct(stop, entry)
            return {'outcome': 'STOP', 'event_ms': ts, 'gross_pct': round(gross, 6), 'net_pct': round(gross - roundtrip_cost, 6)}
        if float(candle.high) >= target:
            gross = _pct(target, entry)
            return {'outcome': 'TARGET', 'event_ms': ts, 'gross_pct': round(gross, 6), 'net_pct': round(gross - roundtrip_cost, 6)}
    timestamps = [int(c.timestamp_ms) for c in rows]
    if not timestamps or timestamps[-1] < limit_ms:
        return {'outcome': 'OPEN', 'event_ms': None, 'gross_pct': None, 'net_pct': None}
    target_index = bisect.bisect_right(timestamps, limit_ms) - 1
    price = float(rows[target_index].close)
    gross = _pct(price, entry)
    return {'outcome': 'TIME', 'event_ms': int(rows[target_index].timestamp_ms), 'gross_pct': round(gross, 6), 'net_pct': round(gross - roundtrip_cost, 6)}


def compact_master_candidate(candles: Sequence[Any], signal: dict[str, Any]) -> dict[str, Any]:
    trade = simulate_signal_trade(candles, signal)
    trade = enrich_trade_features(candles, trade)
    row = compact_trade(trade)
    row['entry_reference'] = float(signal['entry_reference'])
    row['stop_reference'] = float(signal['stop_reference'])
    row['target_reference'] = float(signal['target_reference'])
    row['entry_features'].update(early_candidate_features(candles, int(signal['signal_ms'])))
    row['forward_labels'] = forward_path_labels(candles, signal)
    row['barrier_label_48h'] = barrier_label_48h(candles, signal)
    row['cross_section'] = {}
    return row


def _value(candidate: dict[str, Any], path: str) -> float:
    current: Any = candidate
    for part in path.split('.'):
        if not isinstance(current, dict):
            return float('-inf')
        current = current.get(part)
    try:
        value = float(current)
    except (TypeError, ValueError, OverflowError):
        return float('-inf')
    return value if math.isfinite(value) else float('-inf')


def attach_candidate_cross_section(candidates: Sequence[dict[str, Any]]) -> None:
    by_time: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        by_time[int(candidate['signal_ms'])].append(candidate)
    ranking_fields = {
        'rank_base_score': 'base_score',
        'rank_return_5m': 'entry_features.return_5m_pct',
        'rank_accel_5m': 'entry_features.momentum_accel_5m_vs_15m',
        'rank_relative_strength_1h': 'relative_strength_vs_btc_1h_pct',
        'rank_volume_accel': 'entry_features.volume_accel_ratio',
    }
    for group in by_time.values():
        count = len(group)
        for candidate in group:
            cross: dict[str, int] = {'simultaneous_candidate_count': count}
            for output_key, field in ranking_fields.items():
                own = _value(candidate, field)
                cross[output_key] = 1 + sum(_value(other, field) > own for other in group)
            candidate['cross_section'] = cross


def run_export(api: BitvavoPublic, *, days: int = 90, now_ms: int | None = None, markets: Sequence[str] | None = None, output_path: str | None = None) -> dict[str, Any]:
    if days != 90:
        raise ValueError('master research dataset vereist exact 90 signaaldagen')
    fetch_end = int(time.time() * 1000) if now_ms is None else int(now_ms)
    fetch_end = fetch_end // FIVE_MINUTE_MS * FIVE_MINUTE_MS
    signal_end = fetch_end - LABEL_TAIL_MINUTES * 60_000
    signal_start = signal_end - days * DAY_MS
    fetch_start = signal_start - DAY_MS

    active = sorted(set(markets or api.trading_markets('EUR')))
    if 'BTC-EUR' not in active:
        active.append('BTC-EUR')
        active.sort()

    btc = api.closed_candles_between('BTC-EUR', '5m', fetch_start, fetch_end, now_ms=fetch_end)
    if len(btc) < 288:
        raise RuntimeError('onvoldoende BTC-historie voor master research dataset')

    candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    dropped_incomplete_labels = 0
    context_aggregates: dict[int, dict[str, float]] = {}
    markets_completed = 0

    for market in active:
        try:
            rows = btc if market == 'BTC-EUR' else api.closed_candles_between(market, '5m', fetch_start, fetch_end, now_ms=fetch_end)
            accumulate_early_market_context(rows, signal_start, signal_end, context_aggregates)
            replay = replay_market(market, rows, btc, assumed_spread_pct=ASSUMED_SPREAD_PCT, horizons_minutes=LABEL_HORIZONS_MINUTES, signal_start_ms=signal_start)
            for signal in replay['signals']:
                signal_ms = int(signal['signal_ms'])
                if signal_ms >= signal_end:
                    continue
                row = compact_master_candidate(rows, signal)
                if not all(int(label.get('mature', 0)) == 1 for label in row['forward_labels'].values()):
                    dropped_incomplete_labels += 1
                    continue
                if str(row['barrier_label_48h'].get('outcome')) == 'OPEN':
                    dropped_incomplete_labels += 1
                    continue
                candidates.append(row)
            markets_completed += 1
        except Exception as exc:
            errors.append(f'{market}: {type(exc).__name__}: {exc}')

    context = finalize_early_market_context(context_aggregates)
    kept: list[dict[str, Any]] = []
    for candidate in candidates:
        market_context = context.get(int(candidate['signal_ms']))
        if market_context is None:
            dropped_incomplete_labels += 1
            continue
        candidate['market_context'] = market_context
        kept.append(candidate)
    candidates = kept
    attach_candidate_cross_section(candidates)
    candidates.sort(key=lambda item: (int(item['signal_ms']), str(item['market'])))

    report = {
        'version': '4.0-master-research-dataset-1',
        'component': 'V40_MASTER_RESEARCH_DATASET',
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'mode': 'OFFLINE_MEASUREMENT_ONLY',
        'execution_enabled': False,
        'live_orders_possible': False,
        'active_paper_changed': False,
        'future_data_used_for_features': False,
        'future_data_used_for_labels': True,
        'period': {'signal_days': days, 'signal_start_ms': signal_start, 'signal_end_ms_exclusive': signal_end, 'fetch_start_ms': fetch_start, 'fetch_end_ms': fetch_end, 'warmup_days': 1, 'label_tail_minutes': LABEL_TAIL_MINUTES, 'candle_interval': '5m', 'split_intent': '60D_TRAINING_15D_VALIDATION_15D_UNTOUCHED_TEST', 'purge_required_at_split_boundaries': True, 'maximum_label_horizon_minutes': LABEL_TAIL_MINUTES},
        'markets_requested': len(active),
        'markets_completed': markets_completed,
        'candidates': len(candidates),
        'candidates_dropped_incomplete_labels_or_context': dropped_incomplete_labels,
        'early_feature_set': list(EARLY_FEATURE_KEYS),
        'market_context_set': list(EARLY_MARKET_CONTEXT_KEYS),
        'cross_section_set': list(CROSS_SECTION_KEYS),
        'label_horizons_minutes': list(LABEL_HORIZONS_MINUTES),
        'errors': errors,
        'rows': candidates,
        'notes': ['Eenmalige herbruikbare point-in-time onderzoeksdataset; geen handelslogica wordt gewijzigd.', '5m en 10m zijn de vroege timinglaag; 15m/1u/4u blijven context.', 'Toekomstdata staat uitsluitend in expliciete labelvelden en nooit in beslisfeatures.', 'De laatste 48 uur van de fetch zijn alleen labelstaart zodat alle geëxporteerde kandidaten volledig gemeten zijn.', 'Bij latere 60/15/15-training moet rondom splitgrenzen worden gepurged volgens de gebruikte labelhorizon.', 'Alle EUR-markten volgen dezelfde regels; geen VET/VTHO/LSK-specialisatie.'],
    }
    if output_path:
        _write_report(output_path, report)
    return report


def print_status(report: dict[str, Any]) -> None:
    print('=== v4.0 MASTER RESEARCH DATASET | 5M/10M MOMENT + RANKING ===')
    print('UITVOERING : UIT / OFFLINE METING')
    print(f"MARKTEN    : {report['markets_completed']}/{report['markets_requested']}")
    print(f"KANDIDATEN : {report['candidates']}")
    print(f"DROP LABEL : {report['candidates_dropped_incomplete_labels_or_context']}")
    print(f"VROEG      : {len(report['early_feature_set'])} features")
    print(f"MARKT      : {len(report['market_context_set'])} contextvelden")
    print(f"RANK       : {len(report['cross_section_set'])} cross-sectionvelden")
    print(f"FOUTEN     : {len(report['errors'])}")


def main() -> int:
    parser = argparse.ArgumentParser(description='v4.0 herbruikbare master research dataset')
    parser.add_argument('--days', type=int, default=90)
    parser.add_argument('--markets', default='', help='optioneel: komma-gescheiden EUR-markten')
    parser.add_argument('--output', default='cryptobot_v40_master_research_dataset.json')
    args = parser.parse_args()
    api = BitvavoPublic('https://api.bitvavo.com/v2', timeout_seconds=20, retries=4)
    markets = [item.strip().upper() for item in args.markets.split(',') if item.strip()] or None
    report = run_export(api, days=args.days, markets=markets, output_path=args.output)
    print_status(report)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
