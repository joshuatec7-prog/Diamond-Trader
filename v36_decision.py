from __future__ import annotations

import math
from statistics import fmean, median
from typing import Any, Sequence

from models import Candle


FIVE_MINUTES_MS = 5 * 60 * 1000
FIFTEEN_MINUTES_MS = 15 * 60 * 1000
ONE_HOUR_MS = 60 * 60 * 1000
JURY_CATEGORIES = (
    'markt_en_trend',
    'timing_en_momentum',
    'volume_en_bevestiging',
    'bitcoin_en_marktschok',
    'orderboek_en_spread',
    'netto_risico_opbrengst',
    'anti_pump',
    'datakwaliteit',
)


def _finite(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def candle_quality(
    candles: Sequence[Candle],
    *,
    interval_ms: int,
    now_ms: int,
    minimum: int = 30,
    allow_one_small_gap: bool = False,
) -> dict[str, Any]:
    """Controleer gesloten candles fail-closed en geef één klein 5m-gat apart aan."""
    rows = list(candles)
    if len(rows) < minimum:
        return {
            'valid': False,
            'status': 'BLOCK',
            'reason': 'onvoldoende_candles',
            'missing_intervals': 0,
            'score': 0.0,
        }
    rows = rows[-max(minimum, 60):]
    if any(not row.is_valid for row in rows):
        return {
            'valid': False,
            'status': 'BLOCK',
            'reason': 'ongeldige_candlewaarden',
            'missing_intervals': 0,
            'score': 0.0,
        }
    timestamps = [int(row.timestamp_ms) for row in rows]
    if timestamps != sorted(set(timestamps)):
        return {
            'valid': False,
            'status': 'BLOCK',
            'reason': 'dubbele_of_ongeordende_candles',
            'missing_intervals': 0,
            'score': 0.0,
        }
    missing = 0
    largest_gap = interval_ms
    for previous, current in zip(timestamps, timestamps[1:]):
        difference = current - previous
        largest_gap = max(largest_gap, difference)
        if difference <= 0 or difference % interval_ms != 0:
            return {
                'valid': False,
                'status': 'BLOCK',
                'reason': 'onregelmatig_candlegat',
                'missing_intervals': missing,
                'score': 0.0,
            }
        missing += max(0, difference // interval_ms - 1)

    latest_close_ms = timestamps[-1] + interval_ms
    age_ms = now_ms - latest_close_ms
    if age_ms < -5_000:
        return {
            'valid': False,
            'status': 'BLOCK',
            'reason': 'niet_gesloten_recente_candle',
            'missing_intervals': missing,
            'score': 0.0,
        }
    if age_ms > interval_ms + 90_000:
        return {
            'valid': False,
            'status': 'BLOCK',
            'reason': 'recente_candle_ontbreekt',
            'missing_intervals': missing,
            'latest_close_age_seconds': round(age_ms / 1000.0, 1),
            'score': 0.0,
        }
    if missing == 0:
        return {
            'valid': True,
            'status': 'OK',
            'reason': 'compleet',
            'missing_intervals': 0,
            'latest_close_age_seconds': round(max(0, age_ms) / 1000.0, 1),
            'score': 8.0,
        }
    if allow_one_small_gap and missing == 1 and largest_gap == 2 * interval_ms:
        return {
            'valid': True,
            'status': 'WARN',
            'reason': 'een_klein_5m_interval_ontbreekt',
            'missing_intervals': 1,
            'latest_close_age_seconds': round(max(0, age_ms) / 1000.0, 1),
            'score': 4.0,
        }
    return {
        'valid': False,
        'status': 'BLOCK',
        'reason': 'meerdere_of_grote_candlegaten',
        'missing_intervals': missing,
        'score': 0.0,
    }


def timeframe_features(candles: Sequence[Candle]) -> dict[str, Any]:
    """Maak uitlegbare trend-, momentum-, volume- en ATR-kenmerken."""
    rows = list(candles)[-60:]
    if len(rows) < 30:
        return {'valid': False, 'reason': 'onvoldoende_indicatorhistorie'}
    closes = [_finite(row.close) for row in rows]
    volumes = [_finite(row.volume) for row in rows]
    if min(closes) <= 0.0:
        return {'valid': False, 'reason': 'ongeldige_prijs_in_indicatorhistorie'}

    fast = fmean(closes[-5:])
    slow = fmean(closes[-20:])
    atr_values: list[float] = []
    for previous, current in zip(rows[-15:-1], rows[-14:]):
        atr_values.append(max(
            _finite(current.high) - _finite(current.low),
            abs(_finite(current.high) - _finite(previous.close)),
            abs(_finite(current.low) - _finite(previous.close)),
        ))
    atr = fmean(atr_values)
    price = closes[-1]
    atr_pct = atr / price * 100.0
    prior_volume = median(volumes[-21:-1])
    if atr_pct <= 0.0 or prior_volume <= 0.0:
        return {'valid': False, 'reason': 'onbetrouwbare_atr_of_volume'}

    gains = 0.0
    losses = 0.0
    for before, after in zip(closes[-15:-1], closes[-14:]):
        change = after - before
        gains += max(0.0, change)
        losses += max(0.0, -change)
    if losses <= 0.0:
        rsi = 100.0
    else:
        relative_strength = gains / losses
        rsi = 100.0 - 100.0 / (1.0 + relative_strength)

    momentum_3_pct = (price / closes[-4] - 1.0) * 100.0
    last_bar_pct = (price / _finite(rows[-1].open, price) - 1.0) * 100.0
    slope_pct = (fast / fmean(closes[-10:-5]) - 1.0) * 100.0
    volume_ratio = volumes[-1] / prior_volume
    prior_high = max(_finite(row.high) for row in rows[-7:-1])
    extension_pct = max(0.0, (price / slow - 1.0) * 100.0)
    extension_atr = extension_pct / atr_pct
    breakout = price > prior_high
    pullback_resume = (
        _finite(rows[-1].low) <= fast * 1.002
        and price > _finite(rows[-1].open)
        and price > fast
    )
    volume_resume = price > closes[-2] > closes[-3] and volume_ratio >= 1.20
    triggered = breakout or pullback_resume or volume_resume
    values = {
        'close': price,
        'fast': fast,
        'slow': slow,
        'atr_pct': atr_pct,
        'momentum_3_pct': momentum_3_pct,
        'last_bar_pct': last_bar_pct,
        'slope_pct': slope_pct,
        'volume_ratio': volume_ratio,
        'rsi': rsi,
        'extension_atr': extension_atr,
        'prior_high': prior_high,
    }
    if not all(math.isfinite(value) for value in values.values()):
        return {'valid': False, 'reason': 'niet_eindige_indicator'}
    trigger = (
        '5m_breakout' if breakout
        else '5m_pullback_hervatting' if pullback_resume
        else '5m_volume_hervatting' if volume_resume
        else 'geen_5m_trigger'
    )
    return {
        'valid': True,
        **{key: round(value, 8) for key, value in values.items()},
        'trend_up': fast > slow and slope_pct > 0.0,
        'trend_down': fast < slow and slope_pct < 0.0,
        'triggered': triggered,
        'trigger': trigger,
        'latest_candle_ms': int(rows[-1].timestamp_ms),
    }


def _vote(score: float, maximum: float, reason: str, *, hard_pass: bool = True) -> dict[str, Any]:
    return {
        'score': round(max(0.0, min(maximum, score)), 1),
        'maximum': maximum,
        'vote': 'VOOR' if score >= maximum * 0.60 else 'TEGEN',
        'hard_pass': hard_pass,
        'reason': reason,
    }


def evaluate_jury(
    *,
    context: dict[str, Any],
    depth: dict[str, Any] | None,
    entry_score_min: float = 72.0,
    max_spread_pct: float = 0.25,
    minimum_rr: float = 1.15,
    taker_fee_pct: float = 0.25,
    slippage_pct: float = 0.08,
    stop_net_pct: float = -3.0,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Laat acht juryleden afzonderlijk stemmen; veiligheidsveto's blijven hard."""
    blockers: list[str] = []
    jury: dict[str, dict[str, Any]] = {}
    five = context.get('five', {})
    fifteen = context.get('fifteen', {})
    hour = context.get('hour', {})
    bitcoin = context.get('bitcoin', {})
    qualities = context.get('quality', {})
    regime = str(context.get('regime', 'UNKNOWN'))

    context_score = 0.0
    if regime == 'BULL':
        context_score += 8.0
    elif regime == 'SIDEWAYS':
        context_score += 3.0
    else:
        blockers.append('marktregime_blokkeert_long')
    if bool(fifteen.get('trend_up')):
        context_score += 6.0
    if bool(hour.get('trend_up')):
        context_score += 6.0
    context_hard = bool(fifteen.get('trend_up')) and bool(hour.get('trend_up'))
    if not context_hard:
        blockers.append('15m_en_1h_trend_niet_beide_omhoog')
    jury['markt_en_trend'] = _vote(
        context_score, 20.0, f'{regime}; 15m/1h trendcontrole', hard_pass=context_hard
    )

    momentum = _finite(five.get('momentum_3_pct'))
    timing_score = 0.0
    if bool(five.get('trend_up')):
        timing_score += 5.0
    if 0.0 < momentum <= 3.0:
        timing_score += 5.0
    elif 3.0 < momentum <= 4.5:
        timing_score += 2.0
    if bool(five.get('triggered')):
        timing_score += 6.0
    timing_hard = bool(five.get('trend_up')) and momentum > 0.0 and bool(five.get('triggered'))
    if not bool(five.get('trend_up')):
        blockers.append('5m_trend_niet_omhoog')
    if momentum <= 0.0:
        blockers.append('5m_momentum_niet_positief')
    if not bool(five.get('triggered')):
        blockers.append('5m_instaptrigger_ontbreekt')
    jury['timing_en_momentum'] = _vote(
        timing_score,
        16.0,
        str(five.get('trigger', 'geen_5m_trigger')),
        hard_pass=timing_hard,
    )

    volume_ratio = _finite(five.get('volume_ratio'))
    volume_score = 10.0 if volume_ratio >= 1.20 else 6.0 if volume_ratio >= 0.80 else 2.0
    volume_hard = volume_ratio >= 0.60
    if not volume_hard:
        blockers.append('5m_volume_onvoldoende')
    jury['volume_en_bevestiging'] = _vote(
        volume_score, 10.0, f'volumeratio {volume_ratio:.2f}', hard_pass=volume_hard
    )

    btc_momentum = _finite(bitcoin.get('momentum_3_pct'))
    btc_shock = btc_momentum <= -1.50 or _finite(bitcoin.get('last_bar_pct')) <= -1.00
    btc_score = (7.0 if bool(bitcoin.get('trend_up')) else 2.0) + (
        5.0 if btc_momentum >= 0.0 else 2.0 if btc_momentum > -0.75 else 0.0
    )
    if btc_shock:
        blockers.append('bitcoin_marktschok_veto')
    jury['bitcoin_en_marktschok'] = _vote(
        btc_score,
        12.0,
        f'BTC momentum {btc_momentum:+.2f}%',
        hard_pass=not btc_shock and bool(bitcoin.get('valid')),
    )

    spread = 999.0
    imbalance = -999.0
    buy_vwap = 0.0
    sell_vwap = 0.0
    l2_fresh = False
    if isinstance(depth, dict):
        spread = _finite(depth.get('execution_spread_pct'), 999.0)
        imbalance = _finite(depth.get('near_book_imbalance'), -999.0)
        buy_vwap = _finite(depth.get('buy_vwap'))
        sell_vwap = _finite(depth.get('sell_vwap'))
        captured = int(_finite(depth.get('captured_at_ms'), now_ms or 0))
        l2_fresh = now_ms is None or captured <= 0 or abs(int(now_ms) - captured) <= 90_000
    l2_score = 0.0
    if spread <= 0.10:
        l2_score += 6.0
    elif spread <= max_spread_pct:
        l2_score += 3.0
    if imbalance >= 0.10:
        l2_score += 6.0
    elif imbalance >= -0.15:
        l2_score += 3.0
    l2_hard = buy_vwap > 0.0 and sell_vwap > 0.0 and spread <= max_spread_pct and imbalance >= -0.15 and l2_fresh
    if depth is None:
        blockers.append('l2_data_ontbreekt_of_api_fout')
    elif not l2_fresh:
        blockers.append('l2_data_verouderd')
    elif buy_vwap <= 0.0 or sell_vwap <= 0.0:
        blockers.append('l2_uitvoerprijs_ongeldig')
    if spread > max_spread_pct:
        blockers.append('l2_spread_te_hoog')
    if imbalance < -0.15:
        blockers.append('te_veel_verkoopdruk_in_orderboek')
    jury['orderboek_en_spread'] = _vote(
        l2_score,
        12.0,
        f'spread {spread:.3f}%; druk {imbalance:+.3f}',
        hard_pass=l2_hard,
    )

    atr_pct = _finite(five.get('atr_pct'))
    non_book_cost_pct = 2.0 * taker_fee_pct + 2.0 * slippage_pct
    roundtrip_cost_pct = non_book_cost_pct + max(0.0, spread if spread < 999.0 else 0.0)
    gross_reward_pct = min(6.0, max(1.50, 3.0 * atr_pct))
    net_reward_pct = gross_reward_pct - roundtrip_cost_pct
    configured_stop_net_pct = _finite(stop_net_pct, -3.0)
    if configured_stop_net_pct >= 0.0:
        configured_stop_net_pct = -3.0
    actual_net_risk_pct = abs(configured_stop_net_pct)
    net_rr = net_reward_pct / actual_net_risk_pct if net_reward_pct > 0.0 else 0.0
    cost_multiple = gross_reward_pct / roundtrip_cost_pct if roundtrip_cost_pct > 0.0 else 0.0
    rr_score = 14.0 if net_rr >= 1.50 and cost_multiple >= 3.0 else 10.0 if net_rr >= minimum_rr and cost_multiple >= 2.0 else 2.0
    rr_hard = net_rr >= minimum_rr and cost_multiple >= 2.0
    if net_rr < minimum_rr:
        blockers.append('actuele_netto_risico_opbrengst_te_laag')
    if cost_multiple < 2.0:
        blockers.append('te_weinig_bewegingsruimte_na_kosten')
    jury['netto_risico_opbrengst'] = _vote(
        rr_score,
        14.0,
        f'netto R/R {net_rr:.2f}; stop {actual_net_risk_pct:.2f}%; xkosten {cost_multiple:.2f}',
        hard_pass=rr_hard,
    )

    extension = _finite(five.get('extension_atr'), 999.0)
    anti_pump_hard = extension <= 2.50 and momentum <= 4.50
    anti_pump_score = 8.0 if extension <= 1.50 and momentum <= 3.0 else 4.0 if anti_pump_hard else 0.0
    if extension > 2.50:
        blockers.append('koers_te_ver_boven_5m_gemiddelde')
    if momentum > 4.50:
        blockers.append('pump_niet_achterna_jagen')
    jury['anti_pump'] = _vote(
        anti_pump_score,
        8.0,
        f'extensie {extension:.2f} ATR; momentum {momentum:+.2f}%',
        hard_pass=anti_pump_hard,
    )

    quality_score = 8.0
    quality_reasons: list[str] = []
    quality_hard = True
    for name in ('five', 'fifteen', 'hour', 'bitcoin'):
        item = qualities.get(name, {}) if isinstance(qualities, dict) else {}
        if not bool(item.get('valid')):
            quality_hard = False
            quality_score = 0.0
            reason = str(item.get('reason', f'{name}_data_ongeldig'))
            blockers.append(f'datakwaliteit_{name}_{reason}')
            quality_reasons.append(f'{name}: BLOK')
        elif str(item.get('status')) == 'WARN':
            quality_score = min(quality_score, 4.0)
            quality_reasons.append(f"{name}: {item.get('reason')}")
    jury['datakwaliteit'] = _vote(
        quality_score,
        8.0,
        '; '.join(quality_reasons) if quality_reasons else 'alle tijdreeksen actueel en compleet',
        hard_pass=quality_hard,
    )

    score = round(sum(float(value['score']) for value in jury.values()), 1)
    if score < entry_score_min:
        blockers.append('totale_juryscore_te_laag')
    blockers = list(dict.fromkeys(blockers))
    technical_blockers = {
        blocker for blocker in blockers
        if not blocker.startswith('l2_')
        and blocker not in {
            'te_veel_verkoopdruk_in_orderboek',
            'actuele_netto_risico_opbrengst_te_laag',
            'te_weinig_bewegingsruimte_na_kosten',
            'totale_juryscore_te_laag',
        }
    }
    active_candidate = not technical_blockers and score >= max(55.0, entry_score_min - 15.0)
    eligible = not blockers and score >= entry_score_min
    return {
        'market': str(context.get('market', '')),
        'action': 'AUTOMATISCHE PAPER-INSTAP' if eligible else 'AFWIJZEN',
        'eligible': eligible,
        'active_candidate': active_candidate,
        'score': score,
        'entry_score_min': entry_score_min,
        'jury': jury,
        'blockers': blockers,
        'trigger': str(five.get('trigger', 'geen_5m_trigger')),
        'buy_vwap': round(buy_vwap, 10),
        'sell_vwap': round(sell_vwap, 10),
        'execution_spread_pct': round(spread, 6),
        'near_book_imbalance': round(imbalance, 6),
        'non_book_cost_pct': round(non_book_cost_pct, 6),
        'roundtrip_cost_pct': round(roundtrip_cost_pct, 6),
        'stop_net_pct': round(configured_stop_net_pct, 6),
        'net_reward_risk': round(net_rr, 4),
        'cost_multiple': round(cost_multiple, 4),
        'technical_stop_hint': round(
            buy_vwap * (1.0 + (configured_stop_net_pct + non_book_cost_pct) / 100.0),
            10,
        ),
        'technical_reward_hint': round(buy_vwap * (1.0 + gross_reward_pct / 100.0), 10),
        'data_quality_status': str(qualities.get('five', {}).get('status', 'BLOCK')),
    }
