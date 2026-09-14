from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import mean
from typing import Any, Sequence

from models import Candle


MIN_CANDLES = 60
MIN_VOLUME_QUOTE_EUR = 50_000.0
ROUNDTRIP_FIXED_COST_PCT = 0.66  # 2x 0,25% fee + 2x 0,08% slippage


@dataclass(frozen=True)
class V40Settings:
    """Vaste onderzoeksgrenzen; deze laag bevat bewust geen orderfunctie."""

    minimum_score: float = 70.0
    minimum_net_reward_risk: float = 1.50
    maximum_spread_pct: float = 0.25
    minimum_volume_quote_eur: float = MIN_VOLUME_QUOTE_EUR
    minimum_position_eur: float = 250.0
    medium_position_eur: float = 400.0
    maximum_position_eur: float = 500.0
    maximum_hold_hours: float = 48.0


def _finite(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _pct(new: float, old: float) -> float:
    return (new / old - 1.0) * 100.0 if new > 0.0 and old > 0.0 else 0.0


def _ema(values: Sequence[float], span: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (span + 1.0)
    result = float(values[0])
    for value in values[1:]:
        result = alpha * float(value) + (1.0 - alpha) * result
    return result


def _atr(candles: Sequence[Candle], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    ranges: list[float] = []
    for previous, current in zip(candles[:-1], candles[1:]):
        ranges.append(max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        ))
    return mean(ranges[-period:]) if ranges else 0.0


def candle_features(candles: Sequence[Candle]) -> dict[str, Any]:
    """Maak uitsluitend kenmerken uit reeds gesloten 5m-candles."""
    valid = sorted((c for c in candles if c.is_valid), key=lambda c: c.timestamp_ms)
    if len(valid) < MIN_CANDLES:
        return {'valid': False, 'reason': 'minimaal_60_gesloten_5m_candles_nodig'}

    closes = [c.close for c in valid]
    latest = valid[-1]
    atr = _atr(valid)
    atr_pct = atr / latest.close * 100.0 if latest.close > 0.0 else 0.0
    ema12 = _ema(closes[-60:], 12)
    ema48 = _ema(closes[-120:], 48)
    previous_high = max(c.high for c in valid[-49:-1])
    distance_to_high = _pct(latest.close, previous_high)

    recent_volume = mean(c.volume for c in valid[-3:])
    baseline_volume = mean(c.volume for c in valid[-23:-3])
    volume_ratio = recent_volume / baseline_volume if baseline_volume > 0.0 else 0.0

    recent_ranges = mean((c.high - c.low) / c.close for c in valid[-12:])
    prior_ranges = mean((c.high - c.low) / c.close for c in valid[-48:-12])
    compression_ratio = recent_ranges / prior_ranges if prior_ranges > 0.0 else 1.0
    recent_low = min(c.low for c in valid[-12:])
    prior_low = min(c.low for c in valid[-24:-12])
    positive_bars = sum(1 for c in valid[-6:] if c.close > c.open)
    extension_atr = (latest.close - ema12) / atr if atr > 0.0 else 0.0

    return {
        'valid': True,
        'timestamp_ms': latest.timestamp_ms,
        'last': latest.close,
        'return_15m_pct': _pct(latest.close, valid[-4].close),
        'return_60m_pct': _pct(latest.close, valid[-13].close),
        'return_4h_pct': _pct(latest.close, valid[-49].close),
        'atr_pct': atr_pct,
        'ema_12': ema12,
        'ema_48': ema48,
        'above_fast_ema': latest.close > ema12,
        'trend_up': latest.close > ema12 > ema48,
        'distance_to_4h_high_pct': distance_to_high,
        'extension_atr': extension_atr,
        'volume_ratio': volume_ratio,
        'compression_ratio': compression_ratio,
        'higher_low': recent_low > prior_low,
        'positive_bars_last_6': positive_bars,
    }


def _route(features: dict[str, Any]) -> tuple[str, list[str], float]:
    m15 = _finite(features.get('return_15m_pct'))
    m60 = _finite(features.get('return_60m_pct'))
    m4h = _finite(features.get('return_4h_pct'))
    volume = _finite(features.get('volume_ratio'))
    near_high = _finite(features.get('distance_to_4h_high_pct'), -999.0)
    compression = _finite(features.get('compression_ratio'), 9.0)
    extension = _finite(features.get('extension_atr'))
    positive = int(_finite(features.get('positive_bars_last_6')))
    trend_up = bool(features.get('trend_up'))

    if m15 > 6.0 or m60 > 12.0 or extension > 4.5:
        return 'PUMP_TE_LAAT', ['koers_is_al_te_ver_uitgelopen'], 0.0

    early = (
        trend_up and 0.20 <= m15 <= 3.00 and 0.30 <= m60 <= 8.00
        and volume >= 1.30 and near_high >= -0.75 and positive >= 4
    )
    swing = (
        trend_up and 0.75 <= m4h <= 12.0 and -0.50 <= m60 <= 4.00
        and volume >= 0.90 and compression <= 1.10
        and bool(features.get('higher_low')) and near_high >= -2.00
    )
    continuation = (
        trend_up and m4h >= 1.50 and -1.00 <= m60 <= 2.50
        and m15 > 0.0 and volume >= 0.80 and positive >= 3
    )

    if early:
        return 'VROEG_MOMENTUM', [], 25.0
    if swing:
        return 'SWING_OPBOUW', [], 22.0
    if continuation:
        return 'PULLBACK_HERVATTING', [], 20.0
    return 'GEEN_SETUP', ['geen_herkenbare_vroege_opbouw'], 0.0


def _score(features: dict[str, Any], route_bonus: float, relative_strength_pct: float) -> float:
    volume = _finite(features.get('volume_ratio'))
    near_high = _finite(features.get('distance_to_4h_high_pct'), -99.0)
    m60 = _finite(features.get('return_60m_pct'))
    m4h = _finite(features.get('return_4h_pct'))
    positive = _finite(features.get('positive_bars_last_6'))
    compression = _finite(features.get('compression_ratio'), 9.0)
    score = route_bonus
    score += min(20.0, max(0.0, volume - 0.75) * 14.0)
    score += min(15.0, max(0.0, relative_strength_pct + 0.50) * 5.0)
    score += min(15.0, max(0.0, m60) * 3.0)
    score += min(10.0, max(0.0, m4h) * 1.25)
    score += min(8.0, positive)
    score += 5.0 if near_high >= -0.50 else 0.0
    score += 5.0 if compression <= 0.90 else 0.0
    return round(min(100.0, max(0.0, score)), 3)


def proposed_position_eur(score: float, settings: V40Settings | None = None) -> float:
    cfg = settings or V40Settings()
    if score >= 90.0:
        return cfg.maximum_position_eur
    if score >= 80.0:
        return cfg.medium_position_eur
    if score >= cfg.minimum_score:
        return cfg.minimum_position_eur
    return 0.0


def resolve_market_regime(
    decisions: Sequence[dict[str, Any]],
    *,
    previous_regime: str = '',
    pending_regime: str = '',
    pending_count: int = 0,
    confirmations: int = 2,
) -> dict[str, Any]:
    """Bepaal een breed marktregime en voorkom omslaan op een enkele scan."""
    returns = [
        _finite(item.get('features', {}).get('return_60m_pct'))
        for item in decisions
        if isinstance(item.get('features'), dict) and item.get('features', {}).get('valid', True)
    ]
    btc_item = next(
        (item for item in decisions if str(item.get('market')).upper() == 'BTC-EUR'), None
    )
    btc_return = _finite(
        btc_item.get('features', {}).get('return_60m_pct') if btc_item else None
    )
    breadth = sum(value > 0.0 for value in returns) / len(returns) * 100.0 if returns else 0.0
    if len(returns) < 20 or btc_item is None:
        raw = 'DATA_UNCERTAIN'
    elif btc_return <= -2.0 or breadth < 35.0:
        raw = 'BEAR'
    elif btc_return > 0.0 and breadth >= 60.0:
        raw = 'BULL'
    else:
        raw = 'SIDEWAYS'

    previous = str(previous_regime).upper()
    pending = str(pending_regime).upper()
    stable_values = {'BULL', 'SIDEWAYS', 'BEAR', 'DATA_UNCERTAIN'}
    if previous not in stable_values:
        stable, visible, next_pending, next_count = raw, raw, '', 0
    elif raw == previous:
        stable, visible, next_pending, next_count = previous, previous, '', 0
    else:
        count = int(pending_count) + 1 if pending == raw else 1
        if count >= max(1, int(confirmations)):
            stable, visible, next_pending, next_count = raw, raw, '', 0
        else:
            stable, visible, next_pending, next_count = previous, 'TRANSITION', raw, count
    return {
        'regime': visible,
        'stable_regime': stable,
        'raw_regime': raw,
        'pending_regime': next_pending,
        'pending_count': next_count,
        'breadth_positive_1h_pct': round(breadth, 3),
        'btc_return_1h_pct': round(btc_return, 4),
        'markets_used': len(returns),
    }


def evaluate_human_challenger(
    decision: dict[str, Any],
    *,
    regime_state: dict[str, Any],
    consecutive_losses: int = 0,
    pause_active: bool = False,
    daily_realized_pnl_eur: float = 0.0,
) -> dict[str, Any]:
    """Tweede, observationele beoordeling; wijzigt de actieve PAPER-route nooit."""
    action = str(decision.get('action', 'AFWIJZEN'))
    route = str(decision.get('route', 'GEEN_SETUP'))
    score = _finite(decision.get('score'))
    net_rr = _finite(decision.get('net_reward_risk'))
    relative = _finite(decision.get('relative_strength_vs_btc_1h_pct'))
    features = decision.get('features', {}) if isinstance(decision.get('features'), dict) else {}
    volume_ratio = _finite(features.get('volume_ratio'))
    regime = str(regime_state.get('regime', 'DATA_UNCERTAIN')).upper()

    confidence = min(40.0, score * 0.40)
    confidence += min(25.0, max(0.0, net_rr - 1.0) * 25.0)
    confidence += min(20.0, max(0.0, relative) * 10.0)
    confidence += min(15.0, max(0.0, volume_ratio - 0.75) * 15.0)
    confidence = round(min(100.0, max(0.0, confidence)), 3)
    evidence = 'STERK' if confidence >= 75.0 else 'REDELIJK' if confidence >= 60.0 else 'ZWAK'

    vetoes: list[str] = []
    uncertainty: list[str] = []
    if pause_active:
        vetoes.append('pauze_na_verliesreeks_of_dagverlies')
    if regime in {'BEAR', 'DATA_UNCERTAIN', 'TRANSITION'}:
        vetoes.append(f'marktregime_{regime.lower()}')
    if score < 75.0:
        uncertainty.append('score_te_dicht_bij_ondergrens')
    if net_rr < 1.65:
        uncertainty.append('netto_rr_te_dicht_bij_ondergrens')
    if relative < 0.50:
        uncertainty.append('relatieve_sterkte_nauwelijks_bevestigd')
    if evidence == 'ZWAK':
        vetoes.append('bewijssterkte_zwak')
    if len(uncertainty) >= 2:
        vetoes.append('meerdere_onzekere_randgevallen')

    if action == 'KOOPKANS':
        review_action = 'AFZIEN' if vetoes else 'PAPER_KANDIDAAT'
    elif action == 'VOLGEN' and route != 'GEEN_SETUP':
        review_action = 'BLIJVEN_VOLGEN'
    else:
        review_action = 'GEEN_ACTIE'
    return {
        'review_action': review_action,
        'evidence_strength': evidence,
        'confidence_score': confidence,
        'regime': regime,
        'vetoes': list(dict.fromkeys(vetoes)),
        'uncertainty': list(dict.fromkeys(uncertainty)),
        'performance_context': {
            'consecutive_losses': int(consecutive_losses),
            'pause_active': bool(pause_active),
            'daily_realized_pnl_eur': round(float(daily_realized_pnl_eur), 2),
        },
        'thesis': {
            'setup': route,
            'why_now': 'trend_volume_relatieve_sterkte_en_kostenrand',
            'expected_horizon_hours': float(decision.get('maximum_hold_hours', 48.0)),
            'invalidation_price': _finite(decision.get('stop_reference')),
            'target_reference': _finite(decision.get('target_reference')),
        },
        'calibrated_probability': False,
        'active_paper_changed': False,
        'execution_enabled': False,
        'live_orders_possible': False,
    }


def evaluate_dynamic_l2_challenger(
    snapshots: Sequence[dict[str, Any]],
    *,
    atr_pct: float,
) -> dict[str, Any]:
    """Beoordeel stabiliteit en richting van L2; uitsluitend als shadow-veto."""
    clean: list[dict[str, float]] = []
    for item in snapshots:
        try:
            spread = _finite(item['spread_pct'], 999.0)
            imbalance = _finite(item['imbalance'], -999.0)
            buy = _finite(item['buy_vwap'])
        except (KeyError, TypeError):
            continue
        if spread >= 0.0 and buy > 0.0:
            clean.append({'spread': spread, 'imbalance': imbalance, 'buy': buy})
    if len(clean) < 3:
        return {
            'status': 'WACHTEN', 'vetoes': ['minimaal_drie_l2_metingen_nodig'],
            'active_paper_changed': False,
        }
    spreads = [item['spread'] for item in clean]
    imbalances = [item['imbalance'] for item in clean]
    buys = [item['buy'] for item in clean]
    drift_pct = (max(buys) / min(buys) - 1.0) * 100.0
    dynamic_drift_limit = min(0.40, max(0.15, max(0.0, float(atr_pct)) * 0.25))
    pressure_change = imbalances[-1] - imbalances[0]
    vetoes: list[str] = []
    if max(spreads) - min(spreads) > 0.10:
        vetoes.append('l2_spread_wordt_instabiel')
    if min(imbalances) < -0.35:
        vetoes.append('l2_tijdelijke_verkooppiek')
    if pressure_change < -0.30:
        vetoes.append('l2_koopdruk_verzwakt_snel')
    if drift_pct > dynamic_drift_limit:
        vetoes.append('l2_prijsdrift_te_groot_voor_volatiliteit')
    return {
        'status': 'AFZIEN' if vetoes else 'BEVESTIGD',
        'vetoes': vetoes,
        'samples': len(clean),
        'spread_range_pct': round(max(spreads) - min(spreads), 6),
        'median_imbalance': round(float(sorted(imbalances)[len(imbalances) // 2]), 6),
        'pressure_change': round(pressure_change, 6),
        'buy_vwap_drift_pct': round(drift_pct, 6),
        'dynamic_drift_limit_pct': round(dynamic_drift_limit, 6),
        'active_paper_changed': False,
    }


def evaluate_entry(
    market: str,
    candles: Sequence[Candle],
    btc_candles: Sequence[Candle],
    *,
    volume_quote_eur: float,
    spread_pct: float,
    settings: V40Settings | None = None,
) -> dict[str, Any]:
    """Beoordeel één markt. Resultaat is advies/onderzoek en kan niets uitvoeren."""
    cfg = settings or V40Settings()
    features = candle_features(candles)
    btc = candle_features(btc_candles)
    reasons: list[str] = []
    if not features.get('valid'):
        reasons.append(str(features.get('reason', 'marktdata_ongeldig')))
    if not btc.get('valid'):
        reasons.append('bitcoin_context_ongeldig')
    if volume_quote_eur < cfg.minimum_volume_quote_eur:
        reasons.append('24u_eurovolume_te_laag')
    if spread_pct < 0.0 or spread_pct > cfg.maximum_spread_pct:
        reasons.append('spread_te_hoog')
    if reasons:
        return {
            'market': market, 'action': 'AFWIJZEN', 'route': 'GEEN_SETUP',
            'score': 0.0, 'reasons': reasons, 'features': features,
            'proposed_paper_eur': 0.0,
            'execution_enabled': False, 'live_orders_possible': False,
        }

    route, route_reasons, bonus = _route(features)
    if route == 'PUMP_TE_LAAT':
        return {
            'market': market, 'action': 'PUMP_TE_LAAT', 'route': route,
            'score': 0.0, 'reasons': route_reasons, 'features': features,
            'proposed_paper_eur': 0.0,
            'execution_enabled': False, 'live_orders_possible': False,
        }

    m60 = _finite(features.get('return_60m_pct'))
    btc60 = _finite(btc.get('return_60m_pct'))
    relative = m60 - btc60
    if btc60 <= -2.0:
        route_reasons.append('bitcoin_1u_marktschok')
    if relative < 0.25:
        route_reasons.append('niet_sterker_dan_bitcoin')
    score = _score(features, bonus, relative)

    atr_pct = _finite(features.get('atr_pct'))
    gross_stop = min(5.0, max(1.50, atr_pct * 1.8))
    roundtrip = ROUNDTRIP_FIXED_COST_PCT + max(0.0, spread_pct)
    gross_target = min(15.0, max(4.0, gross_stop * 2.5 + roundtrip))
    net_reward = gross_target - roundtrip
    net_risk = gross_stop + roundtrip
    net_rr = net_reward / net_risk if net_risk > 0.0 else 0.0
    if net_rr < cfg.minimum_net_reward_risk:
        route_reasons.append('netto_risico_opbrengst_te_laag')
    if score < cfg.minimum_score:
        route_reasons.append('score_onder_70')

    action = 'KOOPKANS' if route != 'GEEN_SETUP' and not route_reasons else 'VOLGEN'
    price = _finite(features.get('last'))
    return {
        'market': market,
        'action': action,
        'route': route,
        'score': score,
        'reasons': list(dict.fromkeys(route_reasons)),
        'relative_strength_vs_btc_1h_pct': round(relative, 4),
        'proposed_paper_eur': proposed_position_eur(score, cfg) if action == 'KOOPKANS' else 0.0,
        'entry_reference': price,
        'stop_reference': round(price * (1.0 - gross_stop / 100.0), 12),
        'target_reference': round(price * (1.0 + gross_target / 100.0), 12),
        'gross_stop_pct': round(gross_stop, 4),
        'gross_target_pct': round(gross_target, 4),
        'estimated_roundtrip_cost_pct': round(roundtrip, 4),
        'net_reward_risk': round(net_rr, 4),
        'maximum_hold_hours': cfg.maximum_hold_hours,
        'features': features,
        'bitcoin_features': btc,
        'execution_enabled': False,
        'live_orders_possible': False,
    }


def evaluate_exit(
    *,
    entry_price: float,
    current_price: float,
    highest_price: float,
    initial_stop_price: float,
    held_hours: float,
    current_features: dict[str, Any],
    maximum_hold_hours: float = 48.0,
) -> dict[str, Any]:
    """Menselijke winstbescherming: vasthouden zolang de beweging gezond blijft."""
    if min(entry_price, current_price, highest_price, initial_stop_price) <= 0.0:
        raise ValueError('prijzen moeten positief zijn')
    profit_pct = _pct(current_price, entry_price)
    peak_profit_pct = _pct(highest_price, entry_price)
    pullback_from_peak_pct = max(0.0, -_pct(current_price, highest_price))
    volume_ratio = _finite(current_features.get('volume_ratio'))
    m15 = _finite(current_features.get('return_15m_pct'))
    trend_up = bool(current_features.get('trend_up'))

    if current_price <= initial_stop_price:
        action, reason = 'VERKOPEN', 'initiele_stop_bereikt'
    elif peak_profit_pct >= 20.0 and (pullback_from_peak_pct >= 3.0 or m15 < 0.0):
        action, reason = 'VERKOPEN', 'grote_winst_beschermen'
    elif profit_pct >= 15.0 and volume_ratio >= 2.50 and m15 >= 3.0:
        action, reason = 'DEEL_VERKOPEN', 'versnellende_pump_winst_deels_vastleggen'
    elif peak_profit_pct >= 8.0 and pullback_from_peak_pct >= 3.0:
        action, reason = 'DEEL_VERKOPEN', 'winst_terugval_vanaf_top'
    elif held_hours >= maximum_hold_hours and not trend_up:
        action, reason = 'VERKOPEN', 'maximale_swingduur_en_trend_gebroken'
    elif not trend_up and profit_pct < 0.0 and held_hours >= 4.0:
        action, reason = 'VERKOPEN', 'verliest_na_vier_uur_zonder_trend'
    else:
        action, reason = 'VASTHOUDEN', 'trend_en_winstbescherming_nog_geldig'

    trail_pct = 0.0
    if peak_profit_pct >= 20.0:
        trail_pct = 4.0
    elif peak_profit_pct >= 10.0:
        trail_pct = 3.0
    elif peak_profit_pct >= 5.0:
        trail_pct = 2.0
    trailing_stop = highest_price * (1.0 - trail_pct / 100.0) if trail_pct else initial_stop_price
    protected_stop = max(initial_stop_price, trailing_stop)
    return {
        'action': action,
        'reason': reason,
        'profit_pct': round(profit_pct, 4),
        'peak_profit_pct': round(peak_profit_pct, 4),
        'pullback_from_peak_pct': round(pullback_from_peak_pct, 4),
        'protected_stop_price': round(protected_stop, 12),
        'held_hours': round(held_hours, 3),
        'execution_enabled': False,
        'live_orders_possible': False,
    }
