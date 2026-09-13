from __future__ import annotations

import math
from collections import Counter
from statistics import mean, median
from typing import Any, Sequence

from models import Candle
from v40_human_engine import (
    ROUNDTRIP_FIXED_COST_PCT,
    candle_features,
    evaluate_entry,
    evaluate_exit,
)


FIVE_MINUTE_MS = 300_000
DAY_MS = 86_400_000
DEFAULT_HORIZONS_MINUTES = (15, 60, 240, 480, 720, 1440, 2160, 2880)
# Drie uur voorkomt dubbel najagen, maar laat een aantoonbaar nieuwe opbouw
# opnieuw toe. De regel geldt identiek voor alle markten.
SIGNAL_COOLDOWN_MS = 3 * 60 * 60_000
ROLLING_DAY_BARS = 24 * 12
PAPER_FEE_PCT = 0.25


def rolling_quote_volume(candles: Sequence[Candle], end_index: int) -> float:
    """24u EUR-volume tot en met het beslismoment; gebruikt nooit toekomstige candles."""
    start = max(0, end_index - ROLLING_DAY_BARS + 1)
    return sum(c.volume * c.close for c in candles[start:end_index + 1])


def forward_outcomes(
    candles: Sequence[Candle],
    entry_index: int,
    *,
    spread_pct: float,
    horizons_minutes: Sequence[int] = DEFAULT_HORIZONS_MINUTES,
) -> dict[str, dict[str, float | int | None]]:
    entry = candles[entry_index].close
    roundtrip_cost = ROUNDTRIP_FIXED_COST_PCT + max(0.0, spread_pct)
    result: dict[str, dict[str, float | int | None]] = {}
    for horizon in horizons_minutes:
        bars = max(1, math.ceil(int(horizon) / 5))
        target = entry_index + bars
        if target >= len(candles):
            result[str(horizon)] = {'mature': 0, 'gross_pct': None, 'net_pct': None}
            continue
        future = candles[target].close
        gross = (future / entry - 1.0) * 100.0
        result[str(horizon)] = {
            'mature': 1,
            'gross_pct': round(gross, 6),
            'net_pct': round(gross - roundtrip_cost, 6),
        }
    return result


def replay_market(
    market: str,
    candles: Sequence[Candle],
    btc_candles: Sequence[Candle],
    *,
    assumed_spread_pct: float = 0.12,
    horizons_minutes: Sequence[int] = DEFAULT_HORIZONS_MINUTES,
    signal_start_ms: int | None = None,
) -> dict[str, Any]:
    """Prospectieve replay: iedere beslissing ziet alleen data van dat moment en daarvoor."""
    rows = sorted((c for c in candles if c.is_valid), key=lambda c: c.timestamp_ms)
    btc_rows = sorted((c for c in btc_candles if c.is_valid), key=lambda c: c.timestamp_ms)
    btc_by_time = {c.timestamp_ms: index for index, c in enumerate(btc_rows)}
    decisions: Counter[str] = Counter()
    routes: Counter[str] = Counter()
    signals: list[dict[str, Any]] = []
    last_signal_ms = -SIGNAL_COOLDOWN_MS

    for index in range(ROLLING_DAY_BARS - 1, len(rows)):
        candle = rows[index]
        if signal_start_ms is not None and candle.timestamp_ms < signal_start_ms:
            continue
        btc_index = btc_by_time.get(candle.timestamp_ms)
        if btc_index is None or btc_index < 59:
            continue
        volume_quote = rolling_quote_volume(rows, index)
        decision = evaluate_entry(
            market,
            rows[max(0, index - 119):index + 1],
            btc_rows[max(0, btc_index - 119):btc_index + 1],
            volume_quote_eur=volume_quote,
            spread_pct=assumed_spread_pct,
        )
        decisions[str(decision['action'])] += 1
        routes[str(decision['route'])] += 1
        if decision['action'] != 'KOOPKANS':
            continue
        if candle.timestamp_ms - last_signal_ms < SIGNAL_COOLDOWN_MS:
            decisions['KOOPKANS_COOLDOWN'] += 1
            continue
        last_signal_ms = candle.timestamp_ms
        signals.append({
            'market': market,
            'signal_ms': candle.timestamp_ms,
            'entry_reference': candle.close,
            'route': decision['route'],
            'score': decision['score'],
            'proposed_paper_eur': decision['proposed_paper_eur'],
            'stop_reference': decision['stop_reference'],
            'target_reference': decision['target_reference'],
            'outcomes': forward_outcomes(
                rows, index, spread_pct=assumed_spread_pct,
                horizons_minutes=horizons_minutes,
            ),
        })
    return {
        'market': market,
        'mode': 'OFFLINE_REPLAY_ONLY',
        'execution_enabled': False,
        'live_orders_possible': False,
        'candles': len(rows),
        'decision_counts': dict(decisions),
        'route_counts': dict(routes),
        'signals': signals,
        'assumed_spread_pct': assumed_spread_pct,
        'roundtrip_fixed_cost_pct': ROUNDTRIP_FIXED_COST_PCT,
        'horizons_minutes': list(horizons_minutes),
        'future_data_used_for_decisions': False,
    }


def simulate_signal_trade(
    candles: Sequence[Candle],
    signal: dict[str, Any],
    *,
    fee_pct: float = PAPER_FEE_PCT,
) -> dict[str, Any]:
    """Speel één signaal chronologisch af met dezelfde menselijke uitstapregels."""
    rows = sorted((c for c in candles if c.is_valid), key=lambda c: c.timestamp_ms)
    signal_ms = int(signal['signal_ms'])
    start = next((index for index, row in enumerate(rows) if row.timestamp_ms == signal_ms), None)
    if start is None:
        raise ValueError('signaalmoment ontbreekt in candles')
    entry = float(signal.get('entry_reference', rows[start].close))
    notional = float(signal.get('proposed_paper_eur', 0.0))
    if entry <= 0.0 or notional <= 0.0:
        raise ValueError('ongeldige PAPER-instap')
    fee_ratio = max(0.0, float(fee_pct)) / 100.0
    initial_base = notional * (1.0 - fee_ratio) / entry
    remaining = initial_base
    highest = entry
    protected_stop = float(signal.get('stop_reference', entry * .97))
    partial_taken = False
    proceeds = 0.0
    events: list[dict[str, Any]] = [{
        'event_ms': signal_ms, 'action': 'KOPEN', 'price': entry,
        'base_amount': initial_base, 'cash_change_eur': -notional,
        'reason': str(signal.get('route', 'KOOPKANS')),
    }]

    for index in range(start + 1, len(rows)):
        candle = rows[index]
        highest = max(highest, candle.high)
        if candle.low <= protected_stop:
            exit_price = min(candle.open, protected_stop) if candle.open < protected_stop else protected_stop
            decision = {'action': 'VERKOPEN', 'reason': 'beschermde_stop_bereikt'}
        else:
            exit_price = candle.close
            decision = evaluate_exit(
                entry_price=entry, current_price=exit_price, highest_price=highest,
                initial_stop_price=protected_stop,
                held_hours=(candle.timestamp_ms - signal_ms) / 3_600_000,
                current_features=candle_features(rows[max(0, index - 119):index + 1]),
            )
            protected_stop = max(protected_stop, float(decision['protected_stop_price']))
        action = str(decision['action'])
        if action == 'DEEL_VERKOPEN' and partial_taken:
            continue
        if action not in {'DEEL_VERKOPEN', 'VERKOPEN'}:
            continue
        quantity = remaining / 2.0 if action == 'DEEL_VERKOPEN' else remaining
        cash_change = quantity * exit_price * (1.0 - fee_ratio)
        proceeds += cash_change
        remaining = max(0.0, remaining - quantity)
        partial_taken = partial_taken or action == 'DEEL_VERKOPEN'
        events.append({
            'event_ms': candle.timestamp_ms, 'action': action, 'price': exit_price,
            'base_amount': quantity, 'cash_change_eur': cash_change,
            'reason': str(decision['reason']),
        })
        if action == 'VERKOPEN':
            break

    last = rows[-1].close
    open_value = remaining * last * (1.0 - fee_ratio)
    closed = remaining <= initial_base * 1e-12
    total_value = proceeds + open_value
    return {
        'market': str(signal.get('market', '')),
        'signal_ms': signal_ms,
        'route': str(signal.get('route', '')),
        'score': float(signal.get('score', 0.0)),
        'position_eur': notional,
        'entry_price': entry,
        'initial_base': initial_base,
        'status': 'GESLOTEN' if closed else 'OPEN',
        'remaining_base': remaining,
        'events': events,
        'realized_proceeds_eur': round(proceeds, 8),
        'open_value_eur': round(open_value, 8),
        'result_eur': round(total_value - notional, 8),
        'result_pct': round((total_value / notional - 1.0) * 100.0, 6),
        'fee_pct_per_side': fee_pct,
        'future_data_used_for_entry': False,
    }


def audit_large_moves(
    market: str,
    candles: Sequence[Candle],
    signals: Sequence[dict[str, Any]],
    *,
    minimum_gain_pct: float = 15.0,
) -> dict[str, Any]:
    """Controleer per UTC-dag of een grote stijging vroeg door een signaal is gezien."""
    days: dict[int, list[Candle]] = {}
    for candle in sorted((c for c in candles if c.is_valid), key=lambda c: c.timestamp_ms):
        days.setdefault(candle.timestamp_ms // DAY_MS, []).append(candle)
    events = []
    for day, rows in sorted(days.items()):
        if len(rows) < 144:  # Een halve dag ontbrekende data is geen betrouwbare auditdag.
            continue
        opening = rows[0].open
        peak = max(rows, key=lambda candle: candle.high)
        gain = (peak.high / opening - 1.0) * 100.0
        if gain < minimum_gain_pct:
            continue
        eligible = sorted(
            (
                signal for signal in signals
                if rows[0].timestamp_ms <= int(signal.get('signal_ms', -1)) <= peak.timestamp_ms
            ),
            key=lambda signal: int(signal.get('signal_ms', 0)),
        )
        first = eligible[0] if eligible else None
        signal_gain = None
        if first is not None:
            signal_gain = (
                float(first.get('entry_reference', opening)) / opening - 1.0
            ) * 100.0
        events.append({
            'market': market,
            'utc_day': day,
            'day_start_ms': rows[0].timestamp_ms,
            'peak_ms': peak.timestamp_ms,
            'gain_to_peak_pct': round(gain, 4),
            'caught': first is not None,
            'caught_early': signal_gain is not None and signal_gain <= 5.0,
            'first_signal_ms': int(first['signal_ms']) if first is not None else None,
            'signal_gain_from_day_open_pct': round(signal_gain, 4) if signal_gain is not None else None,
            'signal_route': str(first.get('route')) if first is not None else None,
        })
    return {
        'market': market,
        'minimum_gain_pct': minimum_gain_pct,
        'large_moves': len(events),
        'caught': sum(1 for event in events if event['caught']),
        'caught_early': sum(1 for event in events if event['caught_early']),
        'missed': sum(1 for event in events if not event['caught']),
        'events': events,
    }


def summarize_replays(replays: Sequence[dict[str, Any]]) -> dict[str, Any]:
    horizon_values: dict[str, list[float]] = {
        str(item): [] for item in DEFAULT_HORIZONS_MINUTES
    }
    markets_with_signals = set()
    route_counts: Counter[str] = Counter()
    total_signals = 0
    for replay in replays:
        for signal in replay.get('signals', []):
            total_signals += 1
            markets_with_signals.add(str(signal.get('market', replay.get('market', ''))))
            route_counts[str(signal.get('route', 'ONBEKEND'))] += 1
            for horizon, outcome in signal.get('outcomes', {}).items():
                value = outcome.get('net_pct') if isinstance(outcome, dict) else None
                if value is not None:
                    horizon_values.setdefault(str(horizon), []).append(float(value))
    outcomes = {}
    for horizon, values in horizon_values.items():
        wins = [value for value in values if value > 0.0]
        losses = [-value for value in values if value < 0.0]
        outcomes[horizon] = {
            'samples': len(values),
            'average_net_pct': round(mean(values), 6) if values else None,
            'median_net_pct': round(median(values), 6) if values else None,
            'win_rate_pct': round(len(wins) / len(values) * 100.0, 3) if values else None,
            'profit_factor': round(sum(wins) / sum(losses), 4) if losses else None,
        }
    return {
        'mode': 'OFFLINE_REPLAY_ONLY',
        'execution_enabled': False,
        'markets_tested': len(replays),
        'markets_with_signals': len(markets_with_signals),
        'signals': total_signals,
        'route_counts': dict(route_counts),
        'outcomes': outcomes,
    }
