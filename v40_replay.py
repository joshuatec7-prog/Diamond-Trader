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
PAPER_SLIPPAGE_PCT = 0.08
PAPER_ASSUMED_SPREAD_PCT = 0.12
HISTORICAL_COST_PER_SIDE_PCT = (
    PAPER_FEE_PCT + PAPER_SLIPPAGE_PCT + PAPER_ASSUMED_SPREAD_PCT / 2.0
)
RUNNER_PARTIAL_TRIGGER_PCT = 25.0
RUNNER_TRAIL_FROM_PEAK_PCT = 30.0
RUNNER_MINIMUM_TRADES = 50
RUNNER_MAX_DRAWDOWN_EUR = 360.0


def rolling_quote_volume(candles: Sequence[Candle], end_index: int) -> float:
    """24u EUR-volume tot en met het beslismoment; gebruikt nooit toekomstige candles."""
    start = max(0, end_index - ROLLING_DAY_BARS + 1)
    return sum(c.volume * c.close for c in candles[start:end_index + 1])


def rolling_quote_volume_series(candles: Sequence[Candle]) -> list[float]:
    """Bereken alle 24u-volumes in één doorloop voor grote historische replays."""
    values: list[float] = []
    running = 0.0
    for index, candle in enumerate(candles):
        running += candle.volume * candle.close
        if index >= ROLLING_DAY_BARS:
            expired = candles[index - ROLLING_DAY_BARS]
            running -= expired.volume * expired.close
        values.append(running)
    return values


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
    rolling_volumes = rolling_quote_volume_series(rows)
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
        volume_quote = rolling_volumes[index]
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
            'relative_strength_vs_btc_1h_pct': decision.get('relative_strength_vs_btc_1h_pct'),
            'net_reward_risk': decision.get('net_reward_risk'),
            'entry_features': {
                key: decision.get('features', {}).get(key)
                for key in (
                    'return_15m_pct', 'return_60m_pct', 'return_4h_pct',
                    'atr_pct', 'volume_ratio', 'trend_up',
                )
            },
            'btc_return_1h_pct': decision.get('bitcoin_features', {}).get('return_60m_pct'),
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
    execution_cost_per_side_pct: float = HISTORICAL_COST_PER_SIDE_PCT,
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
    cost_ratio = max(0.0, float(execution_cost_per_side_pct)) / 100.0
    initial_base = notional * (1.0 - cost_ratio) / entry
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
        cash_change = quantity * exit_price * (1.0 - cost_ratio)
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
    open_value = remaining * last * (1.0 - cost_ratio)
    closed = remaining <= initial_base * 1e-12
    total_value = proceeds + open_value
    return {
        'market': str(signal.get('market', '')),
        'signal_ms': signal_ms,
        'route': str(signal.get('route', '')),
        'score': float(signal.get('score', 0.0)),
        'relative_strength_vs_btc_1h_pct': signal.get('relative_strength_vs_btc_1h_pct'),
        'net_reward_risk': signal.get('net_reward_risk'),
        'entry_features': signal.get('entry_features', {}),
        'btc_return_1h_pct': signal.get('btc_return_1h_pct'),
        'outcomes': signal.get('outcomes', {}),
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
        'execution_cost_pct_per_side': execution_cost_per_side_pct,
        'assumed_roundtrip_cost_pct': execution_cost_per_side_pct * 2.0,
        'future_data_used_for_entry': False,
    }


def simulate_broad_runner_trade(
    candles: Sequence[Candle],
    signal: dict[str, Any],
    *,
    execution_cost_per_side_pct: float = HISTORICAL_COST_PER_SIDE_PCT,
    partial_trigger_pct: float = RUNNER_PARTIAL_TRIGGER_PCT,
    trail_from_peak_pct: float = RUNNER_TRAIL_FROM_PEAK_PCT,
) -> dict[str, Any]:
    """Test een brede runner zonder de actieve v4.0-uitstapregels te wijzigen.

    De helft wordt bij +25% verkocht. Het restant krijgt daarna een ruime stop
    van 30% onder de hoogste koers. Bij onduidelijke volgorde binnen één 5m-candle
    wordt defensief aangenomen dat een geraakte stop ook werkelijk wordt gevuld.
    """
    if not 0.0 < partial_trigger_pct < 1000.0:
        raise ValueError('runner partial-trigger is ongeldig')
    if not 0.0 < trail_from_peak_pct < 100.0:
        raise ValueError('runner trailing afstand is ongeldig')
    rows = sorted((c for c in candles if c.is_valid), key=lambda c: c.timestamp_ms)
    signal_ms = int(signal['signal_ms'])
    start = next((index for index, row in enumerate(rows) if row.timestamp_ms == signal_ms), None)
    if start is None:
        raise ValueError('signaalmoment ontbreekt in candles')
    entry = float(signal.get('entry_reference', rows[start].close))
    notional = float(signal.get('proposed_paper_eur', 0.0))
    if entry <= 0.0 or notional <= 0.0:
        raise ValueError('ongeldige PAPER-instap')

    cost_ratio = max(0.0, float(execution_cost_per_side_pct)) / 100.0
    initial_base = notional * (1.0 - cost_ratio) / entry
    remaining = initial_base
    partial_price = entry * (1.0 + partial_trigger_pct / 100.0)
    protected_stop = float(signal.get('stop_reference', entry * .97))
    highest = entry
    partial_taken = False
    proceeds = 0.0
    events: list[dict[str, Any]] = [{
        'event_ms': signal_ms, 'action': 'KOPEN', 'price': entry,
        'base_amount': initial_base, 'cash_change_eur': -notional,
        'reason': str(signal.get('route', 'KOOPKANS')),
    }]

    def sell(event_ms: int, action: str, price: float, quantity: float, reason: str) -> None:
        nonlocal proceeds, remaining
        cash_change = quantity * price * (1.0 - cost_ratio)
        proceeds += cash_change
        remaining = max(0.0, remaining - quantity)
        events.append({
            'event_ms': event_ms, 'action': action, 'price': price,
            'base_amount': quantity, 'cash_change_eur': cash_change, 'reason': reason,
        })

    for candle in rows[start + 1:]:
        # Reeds geldende stop gaat altijd vóór nieuwe intrabar winstinformatie.
        if candle.low <= protected_stop:
            stop_fill = min(candle.open, protected_stop) if candle.open < protected_stop else protected_stop
            sell(candle.timestamp_ms, 'VERKOPEN', stop_fill, remaining, 'runner_beschermingsstop')
            break

        highest = max(highest, candle.high)
        if not partial_taken and candle.high >= partial_price:
            sell(
                candle.timestamp_ms, 'DEEL_VERKOPEN', partial_price, remaining / 2.0,
                'runner_25pct_halve_winstname',
            )
            partial_taken = True

        if partial_taken:
            new_stop = max(
                protected_stop,
                highest * (1.0 - trail_from_peak_pct / 100.0),
            )
            # De exacte high/low-volgorde binnen een 5m-candle is onbekend.
            # Een nieuwe stop die dezelfde candle raakt, wordt defensief uitgevoerd.
            if new_stop > protected_stop and candle.low <= new_stop:
                protected_stop = new_stop
                sell(
                    candle.timestamp_ms, 'VERKOPEN', protected_stop, remaining,
                    'runner_30pct_topstop',
                )
                break
            protected_stop = new_stop

    last = rows[-1].close
    open_value = remaining * last * (1.0 - cost_ratio)
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
        'highest_price': round(highest, 12),
        'protected_stop_price': round(protected_stop, 12),
        'partial_taken': partial_taken,
        'partial_trigger_pct': partial_trigger_pct,
        'trail_from_peak_pct': trail_from_peak_pct,
        'execution_cost_pct_per_side': execution_cost_per_side_pct,
        'assumed_roundtrip_cost_pct': execution_cost_per_side_pct * 2.0,
        'future_data_used_for_entry': False,
        'intrabar_assumption': 'STOP_DEFENSIEF',
    }


def summarize_strategy_trades(
    trades: Sequence[dict[str, Any]],
    *,
    starting_capital_eur: float = 3600.0,
) -> dict[str, Any]:
    """Vat onafhankelijke signaaltrades samen, inclusief volgorde-drawdown."""
    ordered = sorted(
        trades,
        key=lambda trade: (int(trade.get('signal_ms', 0)), str(trade.get('market', ''))),
    )
    values = [float(trade.get('result_eur', 0.0)) for trade in ordered]
    wins = [value for value in values if value > 0.0]
    losses = [-value for value in values if value < 0.0]
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    return {
        'trades': len(ordered),
        'markets': len({str(trade.get('market', '')) for trade in ordered}),
        'wins': len(wins),
        'losses': len(losses),
        'flat': len(values) - len(wins) - len(losses),
        'open_at_period_end': sum(1 for trade in ordered if trade.get('status') == 'OPEN'),
        'total_result_eur': round(sum(values), 8),
        'average_result_eur': round(mean(values), 8) if values else None,
        'median_result_eur': round(median(values), 8) if values else None,
        'win_rate_pct': round(len(wins) / len(values) * 100.0, 3) if values else None,
        'profit_factor': round(sum(wins) / sum(losses), 4) if losses else None,
        'worst_trade_eur': round(min(values), 8) if values else None,
        'maximum_signal_sequence_drawdown_eur': round(max_drawdown, 8),
        'maximum_signal_sequence_drawdown_pct_of_3600': round(
            max_drawdown / starting_capital_eur * 100.0, 4,
        ) if starting_capital_eur > 0.0 else None,
    }


def build_runner_validation(
    baseline_trades: Sequence[dict[str, Any]],
    runner_trades: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Vergelijk runner en normale route, zowel met als zonder LSK."""
    baseline_all = summarize_strategy_trades(baseline_trades)
    runner_all = summarize_strategy_trades(runner_trades)
    baseline_without_lsk = summarize_strategy_trades([
        trade for trade in baseline_trades if str(trade.get('market')) != 'LSK-EUR'
    ])
    runner_without_lsk = summarize_strategy_trades([
        trade for trade in runner_trades if str(trade.get('market')) != 'LSK-EUR'
    ])
    enough = runner_without_lsk['trades'] >= RUNNER_MINIMUM_TRADES
    positive = runner_without_lsk['total_result_eur'] > 0.0
    beats_baseline = (
        runner_without_lsk['total_result_eur']
        > baseline_without_lsk['total_result_eur']
    )
    controlled = (
        runner_without_lsk['maximum_signal_sequence_drawdown_eur']
        <= RUNNER_MAX_DRAWDOWN_EUR
    )
    candidate = enough and positive and beats_baseline and controlled
    return {
        'policy': {
            'partial_sale_pct': 50.0,
            'partial_trigger_profit_pct': RUNNER_PARTIAL_TRIGGER_PCT,
            'remaining_trail_from_peak_pct': RUNNER_TRAIL_FROM_PEAK_PCT,
            'minimum_trades_without_lsk': RUNNER_MINIMUM_TRADES,
            'maximum_signal_sequence_drawdown_eur': RUNNER_MAX_DRAWDOWN_EUR,
            'intrabar_assumption': 'STOP_DEFENSIEF',
        },
        'all_markets': {
            'baseline': baseline_all,
            'runner': runner_all,
            'runner_minus_baseline_eur': round(
                runner_all['total_result_eur'] - baseline_all['total_result_eur'], 8,
            ),
        },
        'without_lsk': {
            'baseline': baseline_without_lsk,
            'runner': runner_without_lsk,
            'runner_minus_baseline_eur': round(
                runner_without_lsk['total_result_eur']
                - baseline_without_lsk['total_result_eur'], 8,
            ),
        },
        'criteria': {
            'enough_trades_without_lsk': enough,
            'positive_without_lsk': positive,
            'beats_baseline_without_lsk': beats_baseline,
            'drawdown_within_limit_without_lsk': controlled,
        },
        'decision': (
            'KANDIDAAT_VOOR_APARTE_PAPERTEST' if candidate
            else 'ONVOLDOENDE_DATA' if not enough
            else 'AFWIJZEN'
        ),
        'active_bot_changed': False,
        'execution_enabled': False,
        'live_orders_possible': False,
    }



# Deze comparator is uitsluitend voor historische beoordeling. Hij bepaalt welke
# signalen een menselijk beperkte PAPER-portefeuille daadwerkelijk had kunnen nemen.
CAPACITY_STARTING_CAPITAL_EUR = 3600.0
CAPACITY_RESERVE_EUR = 200.0
CAPACITY_MAX_OPEN_POSITIONS = 5
CAPACITY_MAX_DEPLOYED_EUR = 2500.0


def simulate_capacity_limited_portfolio(
    trades: Sequence[dict[str, Any]],
    *,
    starting_capital_eur: float = CAPACITY_STARTING_CAPITAL_EUR,
    reserve_eur: float = CAPACITY_RESERVE_EUR,
    maximum_open_positions: int = CAPACITY_MAX_OPEN_POSITIONS,
    maximum_deployed_eur: float = CAPACITY_MAX_DEPLOYED_EUR,
) -> dict[str, Any]:
    """Speel signaaltrades af als één begrensde portefeuille, zonder voorkennis."""
    if starting_capital_eur <= reserve_eur:
        raise ValueError('startkapitaal moet groter zijn dan de reserve')
    if maximum_open_positions < 1 or maximum_deployed_eur <= 0.0:
        raise ValueError('portefeuillegrenzen zijn ongeldig')

    ordered = sorted(
        trades,
        key=lambda item: (
            int(item.get('signal_ms', 0)),
            -float(item.get('score', 0.0)),
            str(item.get('market', '')),
        ),
    )
    cash = float(starting_capital_eur)
    deployed = 0.0
    realized_pnl = 0.0
    peak_realized_pnl = 0.0
    maximum_realized_drawdown = 0.0
    accepted: list[dict[str, Any]] = []
    rejections: Counter[str] = Counter()
    rejections_by_market: dict[str, Counter[str]] = {}
    open_positions: list[dict[str, Any]] = []

    def record_rejection(market: str, reason: str) -> None:
        rejections[reason] += 1
        rejections_by_market.setdefault(market or 'ONBEKEND', Counter())[reason] += 1

    def close_due(until_ms: int) -> None:
        nonlocal cash, deployed, realized_pnl, peak_realized_pnl, maximum_realized_drawdown
        due = sorted(
            (position for position in open_positions if position['close_ms'] <= until_ms),
            key=lambda position: (position['close_ms'], position['market']),
        )
        for position in due:
            open_positions.remove(position)
            cash += position['final_value_eur']
            deployed -= position['notional_eur']
            pnl = position['final_value_eur'] - position['notional_eur']
            realized_pnl += pnl
            peak_realized_pnl = max(peak_realized_pnl, realized_pnl)
            maximum_realized_drawdown = max(
                maximum_realized_drawdown, peak_realized_pnl - realized_pnl,
            )

    for trade in ordered:
        signal_ms = int(trade.get('signal_ms', 0))
        close_due(signal_ms)
        market = str(trade.get('market', ''))
        notional = float(trade.get('position_eur', 0.0))
        if not market or notional <= 0.0:
            record_rejection(market, 'ongeldig_signaal')
            continue
        if any(position['market'] == market for position in open_positions):
            record_rejection(market, 'dubbele_munt')
            continue
        if len(open_positions) >= maximum_open_positions:
            record_rejection(market, 'maximaal_vijf_posities')
            continue
        if deployed + notional > maximum_deployed_eur + 1e-9:
            record_rejection(market, 'maximale_inzet_2500')
            continue
        if cash - notional < reserve_eur - 1e-9:
            record_rejection(market, 'reserve_200')
            continue

        events = trade.get('events', [])
        closed = str(trade.get('status', '')).upper() == 'GESLOTEN' and len(events) > 1
        close_ms = int(events[-1].get('event_ms', signal_ms)) if closed else math.inf
        final_value = float(trade.get('realized_proceeds_eur', 0.0))
        if not closed:
            final_value += float(trade.get('open_value_eur', 0.0))
        cash -= notional
        deployed += notional
        open_positions.append({
            'market': market,
            'close_ms': close_ms,
            'notional_eur': notional,
            'final_value_eur': final_value,
        })
        accepted.append({
            'market': market,
            'signal_ms': signal_ms,
            'score': round(float(trade.get('score', 0.0)), 3),
            'position_eur': round(notional, 2),
            'route': str(trade.get('route', 'ONBEKEND')),
            'result_eur': round(final_value - notional, 8),
            'holding_hours': round((close_ms - signal_ms) / 3_600_000, 3) if closed else None,
            'relative_strength_vs_btc_1h_pct': trade.get('relative_strength_vs_btc_1h_pct'),
            'net_reward_risk': trade.get('net_reward_risk'),
            'entry_features': trade.get('entry_features', {}),
            'btc_return_1h_pct': trade.get('btc_return_1h_pct'),
            'entry_price': trade.get('entry_price'),
            'result_pct': trade.get('result_pct'),
            'close_ms': close_ms if closed else None,
            'exit_reason': str(events[-1].get('reason', 'OPEN')) if closed else 'OPEN',
            'outcomes': trade.get('outcomes', {}),
            'selection_review': trade.get('shadow_desk', {}),
            'status': 'GESLOTEN' if closed else 'OPEN_EINDE_PERIODE',
        })

    open_value = sum(position['final_value_eur'] for position in open_positions)
    equity = cash + open_value
    total_result = equity - starting_capital_eur
    selected_without_lsk = [item for item in accepted if item['market'] != 'LSK-EUR']
    # Uitkomst per LSK-vrije selectie wordt apart opnieuw gesimuleerd door de aanroeper.
    return {
        'mode': 'OFFLINE_REPLAY_ONLY',
        'execution_enabled': False,
        'live_orders_possible': False,
        'portfolio_limits': {
            'starting_capital_eur': starting_capital_eur,
            'reserve_eur': reserve_eur,
            'maximum_open_positions': maximum_open_positions,
            'maximum_deployed_eur': maximum_deployed_eur,
            'duplicate_market_allowed': False,
        },
        'trades_considered': len(ordered),
        'trades_accepted': len(accepted),
        'trades_rejected': sum(rejections.values()),
        'rejection_counts': dict(rejections),
        'rejection_counts_by_market': {
            market: dict(counts) for market, counts in sorted(rejections_by_market.items())
        },
        'accepted_trades': accepted,
        'open_at_period_end': len(open_positions),
        'ending_cash_eur': round(cash, 8),
        'ending_open_value_eur': round(open_value, 8),
        'ending_equity_eur': round(equity, 8),
        'total_result_eur': round(total_result, 8),
        'maximum_realized_drawdown_eur': round(maximum_realized_drawdown, 8),
        'selected_without_lsk_count': len(selected_without_lsk),
        'future_data_used_for_entry': False,
    }



def _diagnostic_trade_summary(trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    values = [float(trade.get('result_eur', 0.0)) for trade in trades]
    wins = [value for value in values if value > 0.0]
    losses = [-value for value in values if value < 0.0]
    return {
        'trades': len(values),
        'wins': len(wins),
        'losses': len(losses),
        'total_result_eur': round(sum(values), 8),
        'average_result_eur': round(mean(values), 8) if values else None,
        'win_rate_pct': round(len(wins) / len(values) * 100.0, 3) if values else None,
        'profit_factor': round(sum(wins) / sum(losses), 4) if losses else None,
    }


def _diagnostic_groups(
    trades: Sequence[dict[str, Any]],
    labeler: Any,
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        groups.setdefault(str(labeler(trade)), []).append(trade)
    return {
        label: _diagnostic_trade_summary(items)
        for label, items in sorted(groups.items())
    }


def build_capacity_diagnostics(capacity_validation: dict[str, Any]) -> dict[str, Any]:
    """Verklaar waar de begrensde portefeuille wint en verliest; wijzigt niets."""
    all_report = capacity_validation['all_markets']
    without_lsk_report = capacity_validation['without_lsk']
    selected = list(without_lsk_report.get('accepted_trades', []))
    selected_all = list(all_report.get('accepted_trades', []))

    def score_bucket(trade: dict[str, Any]) -> str:
        score = float(trade.get('score', 0.0))
        return '90_PLUS' if score >= 90.0 else '80_89' if score >= 80.0 else '70_79'

    def strength_bucket(trade: dict[str, Any]) -> str:
        value = trade.get('relative_strength_vs_btc_1h_pct')
        if value is None:
            return 'ONBEKEND'
        value = float(value)
        return 'MIN_0_5' if value < .5 else '0_5_TOT_1' if value < 1.0 else '1_TOT_2' if value < 2.0 else '2_PLUS'

    def volume_bucket(trade: dict[str, Any]) -> str:
        value = trade.get('entry_features', {}).get('volume_ratio')
        if value is None:
            return 'ONBEKEND'
        value = float(value)
        return 'ONDER_1' if value < 1.0 else '1_TOT_1_5' if value < 1.5 else '1_5_TOT_2_5' if value < 2.5 else '2_5_PLUS'

    def holding_bucket(trade: dict[str, Any]) -> str:
        value = trade.get('holding_hours')
        if value is None:
            return 'OPEN_EINDE'
        value = float(value)
        return '0_4U' if value <= 4.0 else '4_12U' if value <= 12.0 else '12_24U' if value <= 24.0 else '24_48U' if value <= 48.0 else '48U_PLUS'

    def btc_bucket(trade: dict[str, Any]) -> str:
        value = trade.get('btc_return_1h_pct')
        if value is None:
            return 'ONBEKEND'
        value = float(value)
        return 'BTC_SCHOK' if value <= -2.0 else 'BTC_ZWAK' if value <= 0.0 else 'BTC_POSITIEF' if value < 2.0 else 'BTC_STERK'

    markets = _diagnostic_groups(selected, lambda trade: trade.get('market', 'ONBEKEND'))
    ranked = sorted(
        markets.items(),
        key=lambda item: float(item[1]['total_result_eur']),
        reverse=True,
    )
    controls = {}
    rejection_markets = all_report.get('rejection_counts_by_market', {})
    for market in ('VET-EUR', 'VTHO-EUR', 'LSK-EUR'):
        controls[market] = {
            'selected': _diagnostic_trade_summary([
                trade for trade in selected_all if trade.get('market') == market
            ]),
            'rejection_counts': rejection_markets.get(market, {}),
        }

    return {
        'population': 'CAPACITY_ACCEPTED_WITHOUT_LSK',
        'selected_trades': len(selected),
        'overall': _diagnostic_trade_summary(selected),
        'by_route': _diagnostic_groups(selected, lambda trade: trade.get('route', 'ONBEKEND')),
        'by_score': _diagnostic_groups(selected, score_bucket),
        'by_relative_strength': _diagnostic_groups(selected, strength_bucket),
        'by_volume_ratio': _diagnostic_groups(selected, volume_bucket),
        'by_holding_time': _diagnostic_groups(selected, holding_bucket),
        'by_btc_context': _diagnostic_groups(selected, btc_bucket),
        'best_markets': [
            {'market': market, **summary} for market, summary in ranked[:15]
        ],
        'worst_markets': [
            {'market': market, **summary} for market, summary in reversed(ranked[-15:])
        ],
        'control_markets': controls,
        'active_paper_changed': False,
        'execution_enabled': False,
        'live_orders_possible': False,
    }


def build_capacity_validation(trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Vergelijk dezelfde signalen met echte menselijke portefeuillegrenzen."""
    all_markets = simulate_capacity_limited_portfolio(trades)
    without_lsk = simulate_capacity_limited_portfolio(
        [trade for trade in trades if str(trade.get('market')) != 'LSK-EUR']
    )
    enough = without_lsk['trades_accepted'] >= RUNNER_MINIMUM_TRADES
    positive = without_lsk['total_result_eur'] > 0.0
    controlled = without_lsk['maximum_realized_drawdown_eur'] <= RUNNER_MAX_DRAWDOWN_EUR
    return {
        'all_markets': all_markets,
        'without_lsk': without_lsk,
        'criteria': {
            'minimum_accepted_trades': RUNNER_MINIMUM_TRADES,
            'enough_accepted_trades_without_lsk': enough,
            'positive_result_without_lsk': positive,
            'drawdown_within_360_eur_without_lsk': controlled,
        },
        'decision': (
            'KANDIDAAT_VOOR_APARTE_PAPER_CHALLENGER'
            if enough and positive and controlled else 'AFWIJZEN'
        ),
        'active_paper_changed': False,
        'execution_enabled': False,
        'live_orders_possible': False,
    }



TOURNAMENT_POLICIES = (
    {
        'name': 'BREED_SELECTIEF',
        'minimum_score': 80.0,
        'minimum_relative_strength_pct': 0.50,
        'minimum_volume_ratio': 1.30,
        'minimum_net_reward_risk': 1.65,
        'maximum_new_trades_per_day': 2,
        'routes': ('VROEG_MOMENTUM', 'SWING_OPBOUW', 'PULLBACK_HERVATTING'),
    },
    {
        'name': 'KWALITEIT',
        'minimum_score': 85.0,
        'minimum_relative_strength_pct': 1.00,
        'minimum_volume_ratio': 1.50,
        'minimum_net_reward_risk': 1.80,
        'maximum_new_trades_per_day': 2,
        'routes': ('VROEG_MOMENTUM', 'SWING_OPBOUW', 'PULLBACK_HERVATTING'),
    },
    {
        'name': 'UITZONDERLIJK',
        'minimum_score': 90.0,
        'minimum_relative_strength_pct': 1.50,
        'minimum_volume_ratio': 2.00,
        'minimum_net_reward_risk': 2.00,
        'maximum_new_trades_per_day': 1,
        'routes': ('VROEG_MOMENTUM', 'SWING_OPBOUW', 'PULLBACK_HERVATTING'),
    },
    {
        'name': 'PULLBACK_FOCUS',
        'minimum_score': 80.0,
        'minimum_relative_strength_pct': 0.50,
        'minimum_volume_ratio': 1.00,
        'minimum_net_reward_risk': 1.65,
        'maximum_new_trades_per_day': 2,
        'routes': ('PULLBACK_HERVATTING',),
    },
    {
        'name': 'OPBOUW_ZONDER_PUMP',
        'minimum_score': 85.0,
        'minimum_relative_strength_pct': 1.00,
        'minimum_volume_ratio': 1.50,
        'minimum_net_reward_risk': 1.80,
        'maximum_new_trades_per_day': 2,
        'routes': ('SWING_OPBOUW', 'PULLBACK_HERVATTING'),
    },
    {
        'name': 'MOMENTUM_ZEER_STRENG',
        'minimum_score': 90.0,
        'minimum_relative_strength_pct': 2.00,
        'minimum_volume_ratio': 2.50,
        'minimum_net_reward_risk': 2.00,
        'maximum_new_trades_per_day': 1,
        'routes': ('VROEG_MOMENTUM',),
    },
)


# De Shadow Decision Desk gebruikt bewust één vooraf vastgelegde methode. Er
# wordt dus niet opnieuw achteraf het minst slechte profiel uit dezelfde data
# gekozen. De grens voor netto RR is gelijk aan de bestaande entrygrens; 1,65
# bleek onverenigbaar met veel geldige signalen die door de kostenformule op
# circa 1,645 uitkomen.
SHADOW_DESK_MINIMUM_CONFIDENCE = 75.0
SHADOW_DESK_MINIMUM_WINNER_MARGIN = 5.0
SHADOW_DESK_MAXIMUM_NEW_TRADES_PER_DAY = 1
SHADOW_DESK_MEMORY_MINIMUM_CASES = 10
SHADOW_DESK_LOSS_PAUSE_COUNT = 3
SHADOW_DESK_LOSS_PAUSE_MS = DAY_MS


def _shadow_memory_key(trade: dict[str, Any]) -> str:
    features = trade.get('entry_features', {})
    m60 = float(features.get('return_60m_pct') or 0.0)
    m4h = float(features.get('return_4h_pct') or 0.0)
    btc = float(trade.get('btc_return_1h_pct') or 0.0)
    speed = 'VROEG' if m60 <= 2.5 else 'SNEL'
    extension = 'LAAG' if m4h <= 4.0 else 'HOOG'
    context = 'BTC_ZWAK' if btc < 0.0 else 'BTC_POSITIEF'
    return '|'.join((str(trade.get('route', 'ONBEKEND')), speed, extension, context))


def _shadow_close_ms(trade: dict[str, Any]) -> int | float:
    events = trade.get('events', [])
    if str(trade.get('status', '')).upper() != 'GESLOTEN' or len(events) < 2:
        return math.inf
    return int(events[-1].get('event_ms', math.inf))


def _shadow_memory_summary(cases: Sequence[dict[str, Any]]) -> dict[str, Any]:
    values = [float(case.get('result_eur', 0.0)) for case in cases]
    wins = [value for value in values if value > 0.0]
    losses = [-value for value in values if value < 0.0]
    return {
        'cases': len(values),
        'average_result_eur': round(mean(values), 8) if values else None,
        'win_rate_pct': round(len(wins) / len(values) * 100.0, 3) if values else None,
        'profit_factor': round(sum(wins) / sum(losses), 4) if losses else None,
    }


def _shadow_desk_review(
    trade: dict[str, Any],
    memory_cases: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Laat vaste specialisten stemmen met alleen informatie van het signaalmoment."""
    features = trade.get('entry_features', {})
    route = str(trade.get('route', 'ONBEKEND'))
    score = float(trade.get('score', 0.0))
    relative = float(trade.get('relative_strength_vs_btc_1h_pct') or 0.0)
    net_rr = float(trade.get('net_reward_risk') or 0.0)
    btc = float(trade.get('btc_return_1h_pct') or 0.0)
    m15 = float(features.get('return_15m_pct') or 0.0)
    m60 = float(features.get('return_60m_pct') or 0.0)
    m4h = float(features.get('return_4h_pct') or 0.0)
    atr = float(features.get('atr_pct') or 0.0)
    volume = float(features.get('volume_ratio') or 0.0)
    trend_up = bool(features.get('trend_up'))

    confidence = 0.0
    bull_reasons: list[str] = []
    bear_reasons: list[str] = []
    risk_vetoes: list[str] = []

    route_points = {
        'PULLBACK_HERVATTING': 14.0,
        'SWING_OPBOUW': 9.0,
        'VROEG_MOMENTUM': 5.0,
    }.get(route, 0.0)
    confidence += route_points
    if route_points:
        bull_reasons.append(f'herkenbare_route_{route.lower()}')
    else:
        risk_vetoes.append('route_niet_toegestaan')

    if trend_up:
        confidence += 10.0
        bull_reasons.append('trend_opwaarts')
    else:
        risk_vetoes.append('trend_niet_opwaarts')
    if 0.50 <= relative <= 3.50:
        confidence += 12.0
        bull_reasons.append('relatieve_sterkte_gezond')
    elif relative > 5.0:
        risk_vetoes.append('relatieve_sterkte_mogelijk_doorgeschoten')
    else:
        bear_reasons.append('relatieve_sterkte_onvoldoende_of_extreem')
    if 1.20 <= volume <= 5.00:
        confidence += 10.0
        bull_reasons.append('volume_bevestigt_zonder_extreme_piek')
    elif volume > 8.0:
        risk_vetoes.append('volume_piek_mogelijk_te_laat')
    else:
        bear_reasons.append('volume_bevestigt_niet')
    if 0.20 <= m15 <= 1.80:
        confidence += 15.0
        bull_reasons.append('beweging_begint_net')
    elif m15 > 3.0:
        risk_vetoes.append('kwartierbeweging_te_ver_doorgeschoten')
    else:
        bear_reasons.append('kwartiermomentum_niet_ideaal')
    if 0.30 <= m60 <= 3.00:
        confidence += 15.0
        bull_reasons.append('uurtempo_gezond')
    elif m60 > 6.0:
        risk_vetoes.append('uurbeweging_te_ver_doorgeschoten')
    else:
        bear_reasons.append('uurtempo_niet_ideaal')
    if 0.75 <= m4h <= 4.00:
        confidence += 12.0
        bull_reasons.append('vieruursopbouw_nog_vroeg')
    elif m4h > 10.0:
        risk_vetoes.append('vieruursbeweging_te_ver_doorgeschoten')
    else:
        bear_reasons.append('vieruursopbouw_niet_ideaal')
    if -1.0 <= btc <= 1.5:
        confidence += 7.0
        bull_reasons.append('bitcoin_context_stabiel')
    elif btc <= -2.0:
        risk_vetoes.append('bitcoin_marktschok')
    else:
        bear_reasons.append('bitcoin_context_minder_gunstig')
    if net_rr >= 1.50:
        confidence += 10.0
        bull_reasons.append('netto_rr_boven_bestaande_entrygrens')
    else:
        risk_vetoes.append('netto_rr_onder_bestaande_entrygrens')
    if 0.25 <= atr <= 1.50:
        confidence += 8.0
        bull_reasons.append('volatiliteit_beheersbaar')
    elif atr > 2.5:
        risk_vetoes.append('volatiliteit_te_hoog')
    else:
        bear_reasons.append('volatiliteit_niet_ideaal')
    if score >= 70.0:
        confidence += 4.0
    else:
        risk_vetoes.append('basisscore_onder_70')

    memory = _shadow_memory_summary(memory_cases)
    memory_pf = memory.get('profit_factor')
    memory_average = memory.get('average_result_eur')
    if memory['cases'] >= SHADOW_DESK_MEMORY_MINIMUM_CASES:
        effective_memory_pf = (
            float(memory_pf) if memory_pf is not None
            else math.inf if float(memory_average or 0.0) > 0.0
            else 0.0
        )
        if effective_memory_pf < 0.80 or float(memory_average or 0.0) < -1.0:
            risk_vetoes.append('geheugen_vergelijkbare_situaties_negatief')
        elif effective_memory_pf >= 1.20 and float(memory_average or 0.0) > 0.0:
            confidence += 5.0
            bull_reasons.append('geheugen_vergelijkbare_situaties_positief')

    confidence = round(confidence, 6)
    if confidence < SHADOW_DESK_MINIMUM_CONFIDENCE:
        bear_reasons.append('totale_bewijssterkte_onvoldoende')
    return {
        'confidence': confidence,
        'bull_reasons': bull_reasons,
        'bear_reasons': bear_reasons,
        'risk_vetoes': list(dict.fromkeys(risk_vetoes)),
        'memory_key': _shadow_memory_key(trade),
        'memory': memory,
        'eligible': not risk_vetoes and confidence >= SHADOW_DESK_MINIMUM_CONFIDENCE,
    }


def apply_shadow_decision_desk(trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Chronologische observe-only commissie met geheugen en harde onthouding."""
    clean = sorted(
        (trade for trade in trades if str(trade.get('market')) != 'LSK-EUR'),
        key=lambda trade: (int(trade.get('signal_ms', 0)), str(trade.get('market', ''))),
    )
    grouped: dict[int, list[dict[str, Any]]] = {}
    for trade in clean:
        grouped.setdefault(int(trade.get('signal_ms', 0)), []).append(trade)

    memory_pending = sorted(
        (trade for trade in clean if _shadow_close_ms(trade) != math.inf),
        key=lambda trade: (_shadow_close_ms(trade), int(trade.get('signal_ms', 0))),
    )
    memory_cursor = 0
    memory: dict[str, list[dict[str, Any]]] = {}
    selected: list[dict[str, Any]] = []
    selected_pending: list[dict[str, Any]] = []
    daily_entries: Counter[int] = Counter()
    vetoes: Counter[str] = Counter()
    vtho_seen = vtho_eligible = vtho_selected = 0
    vtho_vetoes: Counter[str] = Counter()
    vtho_lost_to: Counter[str] = Counter()
    vtho_cases: list[dict[str, Any]] = []
    consecutive_losses = 0
    pause_until_ms = 0

    for signal_ms, candidates in sorted(grouped.items()):
        while memory_cursor < len(memory_pending) and _shadow_close_ms(memory_pending[memory_cursor]) <= signal_ms:
            case = memory_pending[memory_cursor]
            memory.setdefault(_shadow_memory_key(case), []).append(case)
            memory_cursor += 1
        closed_selected = [item for item in selected_pending if _shadow_close_ms(item) <= signal_ms]
        for item in sorted(closed_selected, key=_shadow_close_ms):
            selected_pending.remove(item)
            if float(item.get('result_eur', 0.0)) < 0.0:
                consecutive_losses += 1
                if consecutive_losses >= SHADOW_DESK_LOSS_PAUSE_COUNT:
                    pause_until_ms = max(pause_until_ms, int(_shadow_close_ms(item)) + SHADOW_DESK_LOSS_PAUSE_MS)
                    consecutive_losses = 0
            else:
                consecutive_losses = 0

        reviewed: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for trade in candidates:
            market = str(trade.get('market', ''))
            if market == 'VTHO-EUR':
                vtho_seen += 1
            review = _shadow_desk_review(trade, memory.get(_shadow_memory_key(trade), []))
            vtho_case = None
            if market == 'VTHO-EUR':
                vtho_case = {
                    'signal_ms': int(trade.get('signal_ms', 0)),
                    'route': str(trade.get('route', 'ONBEKEND')),
                    'score': float(trade.get('score', 0.0)),
                    'confidence': float(review['confidence']),
                    'eligible': bool(review['eligible']),
                    'selected': False,
                    'bull_reasons': list(review['bull_reasons']),
                    'bear_reasons': list(review['bear_reasons']),
                    'risk_vetoes': list(review['risk_vetoes']),
                    'memory': dict(review['memory']),
                    'result_eur': float(trade.get('result_eur', 0.0)),
                    'outcomes': trade.get('outcomes', {}),
                }
                vtho_cases.append(vtho_case)
            reasons = list(review['risk_vetoes'])
            if not review['eligible']:
                reasons.extend(review['bear_reasons'])
                for reason in dict.fromkeys(reasons):
                    vetoes[reason] += 1
                    if market == 'VTHO-EUR':
                        vtho_vetoes[reason] += 1
                continue
            reviewed.append((trade, review))
            if market == 'VTHO-EUR':
                vtho_eligible += 1
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
            for trade, _ in reviewed:
                if str(trade.get('market')) == 'VTHO-EUR':
                    vtho_vetoes[shared_veto] += 1
            continue

        ranked = sorted(
            reviewed,
            key=lambda item: (-float(item[1]['confidence']), str(item[0].get('market', ''))),
        )
        if len(ranked) > 1:
            margin = float(ranked[0][1]['confidence']) - float(ranked[1][1]['confidence'])
            if margin < SHADOW_DESK_MINIMUM_WINNER_MARGIN:
                vetoes['geen_duidelijke_winnaar'] += len(ranked)
                for trade, _ in ranked:
                    if str(trade.get('market')) == 'VTHO-EUR':
                        vtho_vetoes['geen_duidelijke_winnaar'] += 1
                continue

        winner = dict(ranked[0][0])
        winner['shadow_desk'] = ranked[0][1]
        selected.append(winner)
        if _shadow_close_ms(winner) != math.inf:
            selected_pending.append(winner)
        daily_entries[utc_day] += 1
        if str(winner.get('market')) == 'VTHO-EUR':
            vtho_selected += 1
            for case in reversed(vtho_cases):
                if int(case['signal_ms']) == signal_ms:
                    case['selected'] = True
                    break
        elif any(str(trade.get('market')) == 'VTHO-EUR' for trade, _ in ranked):
            vtho_lost_to[str(winner.get('market', 'ONBEKEND'))] += 1
            for case in reversed(vtho_cases):
                if int(case['signal_ms']) == signal_ms and bool(case['eligible']):
                    case['lost_to_market'] = str(winner.get('market', 'ONBEKEND'))
                    break

    return {
        'selected_trades': selected,
        'candidate_trades': len(clean),
        'desk_selected': len(selected),
        'veto_counts': dict(vetoes),
        'vtho_audit': {
            'candidates': vtho_seen,
            'eligible': vtho_eligible,
            'selected': vtho_selected,
            'veto_counts': dict(vtho_vetoes),
            'lost_to_markets': dict(vtho_lost_to.most_common(15)),
            'cases': vtho_cases,
        },
        'future_data_used_for_selection': False,
        'memory_uses_only_cases_closed_before_decision': True,
    }


def build_shadow_decision_desk(trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Beoordeel één vooraf vastgelegde desk over 60d/15d/15d zonder tuning."""
    clean = sorted(
        (trade for trade in trades if str(trade.get('market')) != 'LSK-EUR'),
        key=lambda trade: (int(trade.get('signal_ms', 0)), str(trade.get('market', ''))),
    )
    if not clean:
        return {
            'decision': 'ONVOLDOENDE_DATA', 'active_paper_changed': False,
            'execution_enabled': False, 'live_orders_possible': False,
        }
    period_end = max(int(trade.get('signal_ms', 0)) for trade in clean) + 1
    test_start = period_end - 15 * DAY_MS
    validation_start = test_start - 15 * DAY_MS
    desk = apply_shadow_decision_desk(clean)
    portfolio = simulate_capacity_limited_portfolio(desk['selected_trades'])
    accepted = portfolio['accepted_trades']

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
    selected_audit = []
    for trade in accepted:
        selected_audit.append({
            'market': str(trade.get('market', '')),
            'signal_ms': int(trade.get('signal_ms', 0)),
            'route': str(trade.get('route', '')),
            'score': float(trade.get('score', 0.0)),
            'result_eur': float(trade.get('result_eur', 0.0)),
            'result_pct': float(trade.get('result_pct') or 0.0),
            'entry_price': float(trade.get('entry_price') or 0.0),
            'exit_reason': str(trade.get('exit_reason', 'OPEN')),
            'outcomes': trade.get('outcomes', {}),
            'desk_review': trade.get('selection_review', {}),
        })
    criteria = {
        'development_positive': development['total_result_eur'] > 0.0,
        'validation_positive': validation['total_result_eur'] > 0.0,
        'untouched_test_positive': untouched['total_result_eur'] > 0.0,
        'full_minimum_50_trades': full['trades'] >= 50,
        'full_positive': full['total_result_eur'] > 0.0,
        'full_pf_above_1_20': (full['profit_factor'] or 0.0) > 1.20,
        'full_drawdown_within_360_eur': portfolio['maximum_realized_drawdown_eur'] <= 360.0,
    }
    return {
        'method': 'VASTE_CHRONOLOGISCHE_SHADOW_DECISION_DESK',
        'configuration': {
            'minimum_confidence': SHADOW_DESK_MINIMUM_CONFIDENCE,
            'minimum_winner_margin': SHADOW_DESK_MINIMUM_WINNER_MARGIN,
            'maximum_new_trades_per_day': SHADOW_DESK_MAXIMUM_NEW_TRADES_PER_DAY,
            'memory_minimum_closed_cases': SHADOW_DESK_MEMORY_MINIMUM_CASES,
            'loss_pause_after': SHADOW_DESK_LOSS_PAUSE_COUNT,
            'loss_pause_hours': SHADOW_DESK_LOSS_PAUSE_MS / 3_600_000,
            'net_reward_risk_floor': 1.50,
            'profiles_optimized_on_replay': 0,
        },
        'period_boundaries_ms': {
            'validation_start': validation_start,
            'untouched_test_start': test_start,
            'period_end': period_end,
        },
        'signals_considered': desk['candidate_trades'],
        'desk_selected': desk['desk_selected'],
        'portfolio_accepted': portfolio['trades_accepted'],
        'portfolio_rejected': portfolio['trades_rejected'],
        'development': development,
        'validation': validation,
        'untouched_test': untouched,
        'full_period': full,
        'maximum_realized_drawdown_eur': portfolio['maximum_realized_drawdown_eur'],
        'selected_trade_audit': selected_audit,
        'veto_counts': desk['veto_counts'],
        'vtho_audit': desk['vtho_audit'],
        'criteria': criteria,
        'decision': 'KANDIDAAT_VOOR_APARTE_PAPER_CHALLENGER' if all(criteria.values()) else 'AFWIJZEN',
        'active_paper_changed': False,
        'execution_enabled': False,
        'live_orders_possible': False,
        'future_data_used_for_selection': False,
        'memory_uses_only_cases_closed_before_decision': True,
    }


def _tournament_reasons(trade: dict[str, Any], policy: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    features = trade.get('entry_features', {})
    score = float(trade.get('score', 0.0))
    relative = trade.get('relative_strength_vs_btc_1h_pct')
    volume = features.get('volume_ratio') if isinstance(features, dict) else None
    net_rr = trade.get('net_reward_risk')
    btc_return = trade.get('btc_return_1h_pct')
    route = str(trade.get('route', 'ONBEKEND'))
    if route not in policy['routes']:
        reasons.append('route_niet_toegestaan')
    if score < float(policy['minimum_score']):
        reasons.append('score_te_laag')
    if relative is None or float(relative) < float(policy['minimum_relative_strength_pct']):
        reasons.append('relatieve_sterkte_te_laag')
    if volume is None or float(volume) < float(policy['minimum_volume_ratio']):
        reasons.append('volume_te_laag')
    if net_rr is None or float(net_rr) < float(policy['minimum_net_reward_risk']):
        reasons.append('netto_rr_te_laag')
    if btc_return is None:
        reasons.append('bitcoin_context_ontbreekt')
    elif float(btc_return) <= -2.0:
        reasons.append('bitcoin_marktschok')
    return reasons


def _tournament_rank(trade: dict[str, Any]) -> float:
    """Rangschik uitsluitend informatie die op het instapmoment bekend was."""
    features = trade.get('entry_features', {})
    score = float(trade.get('score', 0.0))
    relative = max(0.0, min(5.0, float(trade.get('relative_strength_vs_btc_1h_pct') or 0.0)))
    volume = max(0.0, min(4.0, float(features.get('volume_ratio') or 0.0)))
    net_rr = max(0.0, min(3.0, float(trade.get('net_reward_risk') or 0.0)))
    m15 = float(features.get('return_15m_pct') or 0.0)
    m60 = float(features.get('return_60m_pct') or 0.0)
    extension_penalty = max(0.0, m15 - 2.0) * 5.0 + max(0.0, m60 - 8.0) * 3.0
    return round(score + relative * 4.0 + volume * 3.0 + net_rr * 5.0 - extension_penalty, 6)


def apply_tournament_policy(
    trades: Sequence[dict[str, Any]],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Kies chronologisch één opvallende winnaar; kijkt nooit vooruit."""
    grouped: dict[int, list[dict[str, Any]]] = {}
    for trade in trades:
        grouped.setdefault(int(trade.get('signal_ms', 0)), []).append(trade)
    selected: list[dict[str, Any]] = []
    daily_entries: Counter[int] = Counter()
    vetoes: Counter[str] = Counter()
    vtho_seen = 0
    vtho_eligible = 0
    vtho_selected = 0
    vtho_vetoes: Counter[str] = Counter()
    vtho_lost_to: Counter[str] = Counter()

    for signal_ms, candidates in sorted(grouped.items()):
        eligible: list[dict[str, Any]] = []
        for trade in candidates:
            market = str(trade.get('market', ''))
            if market == 'LSK-EUR':
                continue
            if market == 'VTHO-EUR':
                vtho_seen += 1
            reasons = _tournament_reasons(trade, policy)
            if reasons:
                for reason in reasons:
                    vetoes[reason] += 1
                    if market == 'VTHO-EUR':
                        vtho_vetoes[reason] += 1
                continue
            eligible.append(trade)
            if market == 'VTHO-EUR':
                vtho_eligible += 1
        if not eligible:
            continue

        utc_day = signal_ms // DAY_MS
        if daily_entries[utc_day] >= int(policy['maximum_new_trades_per_day']):
            vetoes['daglimiet_bereikt'] += len(eligible)
            for trade in eligible:
                if str(trade.get('market')) == 'VTHO-EUR':
                    vtho_vetoes['daglimiet_bereikt'] += 1
            continue

        ranked = sorted(
            eligible,
            key=lambda trade: (
                -_tournament_rank(trade),
                -float(trade.get('score', 0.0)),
                str(trade.get('market', '')),
            ),
        )
        winner = dict(ranked[0])
        winner['tournament_score'] = _tournament_rank(winner)
        selected.append(winner)
        daily_entries[utc_day] += 1
        if str(winner.get('market')) == 'VTHO-EUR':
            vtho_selected += 1
        elif any(str(trade.get('market')) == 'VTHO-EUR' for trade in eligible):
            vtho_lost_to[str(winner.get('market', 'ONBEKEND'))] += 1

    return {
        'selected_trades': selected,
        'candidate_trades': len(trades),
        'tournament_selected': len(selected),
        'veto_counts': dict(vetoes),
        'vtho_audit': {
            'candidates': vtho_seen,
            'eligible': vtho_eligible,
            'selected': vtho_selected,
            'veto_counts': dict(vtho_vetoes),
            'lost_to_markets': dict(vtho_lost_to.most_common(15)),
        },
        'future_data_used_for_selection': False,
    }


def _evaluate_tournament_period(
    trades: Sequence[dict[str, Any]],
    policy: dict[str, Any],
) -> dict[str, Any]:
    tournament = apply_tournament_policy(trades, policy)
    portfolio = simulate_capacity_limited_portfolio(tournament['selected_trades'])
    performance = _diagnostic_trade_summary(portfolio['accepted_trades'])
    return {
        'signals_considered': len(trades),
        'tournament_selected': tournament['tournament_selected'],
        'portfolio_accepted': portfolio['trades_accepted'],
        'portfolio_rejected': portfolio['trades_rejected'],
        'performance': performance,
        'maximum_realized_drawdown_eur': portfolio['maximum_realized_drawdown_eur'],
        'veto_counts': tournament['veto_counts'],
        'vtho_audit': tournament['vtho_audit'],
    }


def build_tournament_challenger(trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Ontwikkel 60d, valideer 15d en beslis op een onaangeraakte laatste 15d."""
    clean = sorted(
        (
            trade for trade in trades
            if str(trade.get('market')) != 'LSK-EUR'
        ),
        key=lambda trade: (int(trade.get('signal_ms', 0)), str(trade.get('market', ''))),
    )
    if not clean:
        return {
            'decision': 'ONVOLDOENDE_DATA',
            'active_paper_changed': False,
            'execution_enabled': False,
            'live_orders_possible': False,
        }
    period_end = max(int(trade.get('signal_ms', 0)) for trade in clean) + 1
    test_start = period_end - 15 * DAY_MS
    validation_start = test_start - 15 * DAY_MS
    periods = {
        'development': [trade for trade in clean if int(trade.get('signal_ms', 0)) < validation_start],
        'validation': [
            trade for trade in clean
            if validation_start <= int(trade.get('signal_ms', 0)) < test_start
        ],
        'untouched_test': [
            trade for trade in clean if int(trade.get('signal_ms', 0)) >= test_start
        ],
    }

    development_results = []
    for policy in TOURNAMENT_POLICIES:
        result = _evaluate_tournament_period(periods['development'], policy)
        development_results.append({'policy': policy, 'result': result})
    eligible = [
        item for item in development_results
        if item['result']['portfolio_accepted'] >= 20
        and item['result']['performance']['total_result_eur'] > 0.0
        and (item['result']['performance']['profit_factor'] or 0.0) > 1.0
    ]
    pool = eligible or development_results
    chosen = max(
        pool,
        key=lambda item: (
            float(item['result']['performance']['total_result_eur']),
            float(item['result']['performance']['profit_factor'] or 0.0),
        ),
    )
    locked_policy = chosen['policy']
    validation = _evaluate_tournament_period(periods['validation'], locked_policy)
    untouched = _evaluate_tournament_period(periods['untouched_test'], locked_policy)
    full = _evaluate_tournament_period(clean, locked_policy)

    criteria = {
        'development_positive': chosen['result']['performance']['total_result_eur'] > 0.0,
        'development_pf_above_1_10': (
            chosen['result']['performance']['profit_factor'] or 0.0
        ) > 1.10,
        'validation_positive': validation['performance']['total_result_eur'] > 0.0,
        'untouched_test_positive': untouched['performance']['total_result_eur'] > 0.0,
        'full_minimum_50_trades': full['portfolio_accepted'] >= 50,
        'full_positive': full['performance']['total_result_eur'] > 0.0,
        'full_pf_above_1_20': (full['performance']['profit_factor'] or 0.0) > 1.20,
        'full_drawdown_within_360_eur': full['maximum_realized_drawdown_eur'] <= 360.0,
    }
    candidate = all(criteria.values())
    return {
        'method': 'CHRONOLOGISCH_KEUZETOERNOOI',
        'period_boundaries_ms': {
            'validation_start': validation_start,
            'untouched_test_start': test_start,
            'period_end': period_end,
        },
        'policies_compared_on_development_only': [
            {
                'name': item['policy']['name'],
                'accepted': item['result']['portfolio_accepted'],
                'total_result_eur': item['result']['performance']['total_result_eur'],
                'profit_factor': item['result']['performance']['profit_factor'],
            }
            for item in development_results
        ],
        'development_had_eligible_policy': bool(eligible),
        'locked_policy': locked_policy,
        'development': chosen['result'],
        'validation': validation,
        'untouched_test': untouched,
        'full_period': full,
        'criteria': criteria,
        'decision': (
            'KANDIDAAT_VOOR_APARTE_PAPER_CHALLENGER' if candidate else 'AFWIJZEN'
        ),
        'active_paper_changed': False,
        'execution_enabled': False,
        'live_orders_possible': False,
        'future_data_used_for_selection': False,
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
