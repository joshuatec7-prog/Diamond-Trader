from __future__ import annotations

import math
from statistics import fmean, median
from typing import Any, Sequence

from models import Candle


HUMAN_SHORTLIST_SIZE = 5
HUMAN_TRIGGER_INTERVAL = '5m'
HUMAN_TRIGGER_POLL_SECONDS = 60
HUMAN_ENTRY_SCORE = 70.0
HUMAN_MIN_CONTEXT_SCORE = 55.0
HUMAN_MIN_COST_MULTIPLE = 2.0
HUMAN_MAX_EXECUTION_SPREAD_PCT = 0.25
HUMAN_MIN_VOLUME_RATIO = 0.75
HUMAN_MIN_BOOK_IMBALANCE = -0.15
HUMAN_MAX_EXTENSION_ATR = 2.50
HUMAN_MAX_MOMENTUM_5M_PCT = 4.50
HUMAN_MIN_NET_REWARD_RISK = 1.15

_FIVE_MINUTES_MS = 5 * 60 * 1000


def _finite(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def five_minute_features(
    candles: Sequence[Candle], live_price: float | None = None
) -> dict[str, Any]:
    """Maak een kleine, uitlegbare 5m-marktlezing uit uitsluitend gesloten candles."""
    if len(candles) < 30:
        return {'valid': False, 'reason': 'minder_dan_30_gesloten_5m_candles'}
    rows = list(candles[-30:])
    if any(
        current.timestamp_ms - previous.timestamp_ms != _FIVE_MINUTES_MS
        for previous, current in zip(rows, rows[1:])
    ):
        return {'valid': False, 'reason': 'gat_in_5m_candles'}

    closes = [float(row.close) for row in rows]
    volumes = [float(row.volume) for row in rows]
    price = _finite(live_price, closes[-1])
    if price <= 0.0:
        return {'valid': False, 'reason': 'ongeldige_live_prijs'}

    fast = fmean(closes[-5:])
    slow = fmean(closes[-20:])
    previous_volume = median(volumes[-21:-1])
    if previous_volume <= 0.0:
        return {'valid': False, 'reason': 'onvoldoende_5m_volumehistorie'}

    tr_values: list[float] = []
    for previous, current in zip(rows[-15:-1], rows[-14:]):
        tr_values.append(
            max(
                float(current.high) - float(current.low),
                abs(float(current.high) - float(previous.close)),
                abs(float(current.low) - float(previous.close)),
            )
        )
    atr = fmean(tr_values)
    atr_pct = atr / price * 100.0 if price > 0.0 else 0.0
    if atr_pct <= 0.0:
        return {'valid': False, 'reason': 'ongeldige_5m_atr'}

    momentum_3_pct = (price / closes[-4] - 1.0) * 100.0
    last_bar_pct = (closes[-1] / float(rows[-1].open) - 1.0) * 100.0
    prior_high = max(float(row.high) for row in rows[-7:-1])
    volume_ratio = volumes[-1] / previous_volume
    extension_pct = max(0.0, (price / slow - 1.0) * 100.0)
    extension_atr = extension_pct / atr_pct

    breakout = price > prior_high
    pullback_resume = (
        float(rows[-1].low) <= fast * 1.002
        and closes[-1] > float(rows[-1].open)
        and closes[-1] > fast
        and price >= closes[-1]
    )
    volume_continuation = (
        closes[-1] > closes[-2] > closes[-3]
        and price >= closes[-1]
        and volume_ratio >= 1.20
    )
    if breakout:
        trigger = '5m_breakout'
    elif pullback_resume:
        trigger = '5m_pullback_hervatting'
    elif volume_continuation:
        trigger = '5m_volume_hervatting'
    else:
        trigger = 'wachten_op_5m_trigger'

    values = {
        'fast_5m': fast,
        'slow_5m': slow,
        'momentum_3_pct': momentum_3_pct,
        'last_bar_pct': last_bar_pct,
        'prior_high': prior_high,
        'volume_ratio': volume_ratio,
        'atr_pct': atr_pct,
        'extension_atr': extension_atr,
    }
    if not all(math.isfinite(float(value)) for value in values.values()):
        return {'valid': False, 'reason': 'niet_eindige_5m_berekening'}
    return {
        'valid': True,
        **{key: round(value, 6) for key, value in values.items()},
        'trend_up': fast > slow,
        'breakout': breakout,
        'pullback_resume': pullback_resume,
        'volume_continuation': volume_continuation,
        'trigger': trigger,
        'triggered': trigger != 'wachten_op_5m_trigger',
        'latest_candle_ms': int(rows[-1].timestamp_ms),
    }


def _current_net_reward_risk(
    context: dict[str, Any], entry_price: float
) -> tuple[float, float, float]:
    target = _finite(context.get('target_hint'))
    stop = _finite(context.get('stop_hint'))
    cost = max(0.0, _finite(context.get('roundtrip_cost_pct')))
    if entry_price <= 0.0 or stop <= 0.0 or target <= entry_price or stop >= entry_price:
        return 0.0, 999.0, 0.0
    reward = (target / entry_price - 1.0) * 100.0 - cost
    risk = (entry_price - stop) / entry_price * 100.0 + cost
    ratio = reward / risk if reward > 0.0 and risk > 0.0 else 0.0
    return reward, risk, ratio


def evaluate_human_entry(
    *,
    context: dict[str, Any],
    regime: str,
    five_minute: dict[str, Any],
    bitcoin_five_minute: dict[str, Any],
    depth: dict[str, Any],
) -> dict[str, Any]:
    """Combineer context, timing, volume, BTC en orderboek tot één fail-closed oordeel."""
    blockers: list[str] = []
    positives: list[str] = []
    action = str(context.get('action', 'GEEN TRADE'))
    decision_action = str(context.get('decision_action', 'SKIP'))
    context_score = _finite(context.get('score'))
    cost_multiple = _finite(context.get('cost_multiple'))
    spread = _finite(depth.get('execution_spread_pct'), 999.0)
    buy_vwap = _finite(depth.get('buy_vwap'))
    imbalance_value = depth.get('near_book_imbalance')
    imbalance = _finite(imbalance_value, -999.0)

    context_ok = (
        action in {'LONG WATCH', 'LONG TRADE-GRADE', 'SIDEWAYS WATCH'}
        or decision_action in {'LONG', 'BUY'}
    )
    if regime not in {'BULL', 'SIDEWAYS'}:
        blockers.append('marktregime_niet_geschikt_voor_long')
    if str(context.get('side', 'LONG')) != 'LONG':
        blockers.append('alleen_long_in_deze_papertest')
    if not context_ok:
        blockers.append('15m_context_geeft_geen_longruimte')
    if context_score < HUMAN_MIN_CONTEXT_SCORE:
        blockers.append('15m_contextscore_te_laag')
    if cost_multiple < HUMAN_MIN_COST_MULTIPLE:
        blockers.append('te_weinig_bewegingsruimte_na_kosten')
    if not bool(five_minute.get('valid')):
        blockers.append(str(five_minute.get('reason', 'ongeldige_5m_data')))
    if not bool(bitcoin_five_minute.get('valid')):
        blockers.append('ongeldige_bitcoin_5m_data')
    if buy_vwap <= 0.0:
        blockers.append('ongeldige_l2_koopprijs')
    if spread > HUMAN_MAX_EXECUTION_SPREAD_PCT:
        blockers.append('l2_spread_te_hoog')
    if imbalance_value is None:
        blockers.append('orderboekdruk_ontbreekt')
    elif imbalance < HUMAN_MIN_BOOK_IMBALANCE:
        blockers.append('te_veel_verkoopdruk_in_orderboek')

    if bool(five_minute.get('valid')):
        if not bool(five_minute.get('trend_up')):
            blockers.append('5m_trend_niet_omhoog')
        if not bool(five_minute.get('triggered')):
            blockers.append('nog_geen_5m_instaptrigger')
        momentum = _finite(five_minute.get('momentum_3_pct'))
        if momentum <= 0.0:
            blockers.append('5m_momentum_niet_positief')
        elif momentum > HUMAN_MAX_MOMENTUM_5M_PCT:
            blockers.append('pump_niet_achterna_jagen')
        if _finite(five_minute.get('volume_ratio')) < HUMAN_MIN_VOLUME_RATIO:
            blockers.append('5m_volume_te_zwak')
        if _finite(five_minute.get('extension_atr')) > HUMAN_MAX_EXTENSION_ATR:
            blockers.append('koers_te_ver_boven_5m_gemiddelde')
        if _finite(five_minute.get('last_bar_pct')) <= -1.0:
            blockers.append('plotselinge_5m_daling')

    bitcoin_supportive = False
    if bool(bitcoin_five_minute.get('valid')):
        btc_momentum = _finite(bitcoin_five_minute.get('momentum_3_pct'))
        bitcoin_supportive = bool(bitcoin_five_minute.get('trend_up')) or btc_momentum >= 0.0
        bitcoin_veto = (
            btc_momentum <= -0.75
            or _finite(bitcoin_five_minute.get('last_bar_pct')) <= -1.0
        )
        if bitcoin_veto:
            blockers.append('bitcoin_geeft_marktschok_veto')

    reward, risk, current_rr = _current_net_reward_risk(context, buy_vwap)
    if current_rr < HUMAN_MIN_NET_REWARD_RISK:
        blockers.append('actuele_netto_risico_opbrengst_te_laag')

    score = 0.0
    if context_ok:
        score += 18.0
    score += min(10.0, max(0.0, (context_score - HUMAN_MIN_CONTEXT_SCORE) / 2.0))
    if bool(five_minute.get('trend_up')):
        score += 12.0
        positives.append('5m trend omhoog')
    if bool(five_minute.get('triggered')):
        score += 15.0
        positives.append(str(five_minute.get('trigger', '5m trigger')))
    volume_ratio = _finite(five_minute.get('volume_ratio'))
    if volume_ratio >= 1.20:
        score += 10.0
        positives.append('volume versnelt')
    elif volume_ratio >= HUMAN_MIN_VOLUME_RATIO:
        score += 6.0
    if bitcoin_supportive:
        score += 10.0
        positives.append('Bitcoin ondersteunt')
    elif bool(bitcoin_five_minute.get('valid')):
        score += 4.0
    if imbalance >= 0.10:
        score += 10.0
        positives.append('meer nabije bieddruk')
    elif imbalance >= 0.0:
        score += 7.0
    elif imbalance >= HUMAN_MIN_BOOK_IMBALANCE:
        score += 4.0
    if spread <= 0.10:
        score += 5.0
    elif spread <= HUMAN_MAX_EXECUTION_SPREAD_PCT:
        score += 2.0
    if current_rr >= 1.50:
        score += 8.0
    elif current_rr >= HUMAN_MIN_NET_REWARD_RISK:
        score += 5.0
    if _finite(five_minute.get('extension_atr')) <= 1.50:
        score += 5.0
    score = min(100.0, score)
    if score < HUMAN_ENTRY_SCORE:
        blockers.append('menselijke_beslisscore_te_laag')

    eligible = not blockers
    return {
        'action': 'PAPER ENTRY' if eligible else 'WACHTEN',
        'eligible': eligible,
        'score': round(score, 1),
        'market': str(context.get('market', '')),
        'base': str(context.get('base', '')),
        'buy_vwap': round(buy_vwap, 8),
        'sell_vwap': round(_finite(depth.get('sell_vwap')), 8),
        'execution_spread_pct': round(spread, 4),
        'near_book_imbalance': round(imbalance, 4),
        'current_net_reward_pct': round(reward, 4),
        'current_total_risk_pct': round(risk, 4),
        'current_net_reward_risk': round(current_rr, 3),
        'context_action': action,
        'context_score': round(context_score, 1),
        'context_cost_multiple': round(cost_multiple, 2),
        'trigger': str(five_minute.get('trigger', 'geen')),
        'volume_ratio': round(volume_ratio, 3),
        'bitcoin_supportive': bitcoin_supportive,
        'blockers': blockers,
        'positives': positives,
        'five_minute': five_minute,
        'bitcoin_five_minute': bitcoin_five_minute,
    }
