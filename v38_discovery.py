from __future__ import annotations

import math
from typing import Any, Sequence


def _finite(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def movement_features(prices: Sequence[tuple[int, float]], now_ms: int) -> dict[str, float]:
    valid = sorted(
        (int(ts), _finite(price)) for ts, price in prices
        if int(ts) <= now_ms and _finite(price) > 0
    )
    if not valid:
        return {'valid': False}
    latest_ts, latest = valid[-1]

    def price_at(age_ms: int) -> float | None:
        target = now_ms - age_ms
        eligible = [(ts, price) for ts, price in valid if ts <= target]
        if not eligible:
            return None
        ts, price = eligible[-1]
        return price if target - ts <= 120_000 else None

    old5, old15, old60 = (price_at(n * 60_000) for n in (5, 15, 60))
    if old5 is None or old15 is None:
        return {'valid': False, 'latest_age_seconds': (now_ms - latest_ts) / 1000.0}

    def change(old: float | None) -> float:
        return (latest / old - 1.0) * 100.0 if old else 0.0

    m5, m15, m60 = change(old5), change(old15), change(old60)
    return {
        'valid': True,
        'last': latest,
        'momentum_5m_pct': m5,
        'momentum_15m_pct': m15,
        'momentum_60m_pct': m60,
        'acceleration_pct': m5 - m15 / 3.0,
        'latest_age_seconds': (now_ms - latest_ts) / 1000.0,
    }


def human_discovery_decision(
    market: str,
    features: dict[str, Any],
    *,
    volume_quote: float,
) -> dict[str, Any]:
    reasons: list[str] = []
    if not features.get('valid'):
        reasons.append('onvoldoende_korte_prijshistorie')
    m5 = _finite(features.get('momentum_5m_pct'))
    m15 = _finite(features.get('momentum_15m_pct'))
    m60 = _finite(features.get('momentum_60m_pct'))
    acceleration = _finite(features.get('acceleration_pct'))
    liquidity = _finite(volume_quote)
    if liquidity < 50_000:
        reasons.append('24u_euromarkt_te_illiquide')
    if m5 < 0.10 or m15 < 0.20:
        reasons.append('beweging_nog_niet_bevestigd')
    if acceleration < -0.05:
        reasons.append('korte_beweging_vertraagt_duidelijk')
    if m5 > 2.50 or m15 > 6.00 or m60 > 12.00:
        reasons.append('mogelijk_te_laat_achter_pump')
    liquidity_score = min(20.0, max(0.0, math.log10(max(liquidity, 1.0)) - 4.0) * 10.0)
    score = (
        min(30.0, max(0.0, m5) * 12.0)
        + min(25.0, max(0.0, m15) * 4.0)
        + min(15.0, max(0.0, acceleration) * 10.0)
        + liquidity_score
        + (10.0 if 0.0 < m60 <= 8.0 else 0.0)
    )
    return {
        'market': market,
        'action': 'DOOR_NAAR_MENSELIJKE_JURY' if not reasons else 'ALLEEN_VOLGEN',
        'discovery_score': round(score, 3),
        'reasons': reasons,
        'features': features,
        'volume_quote': round(liquidity, 2),
        'execution_enabled': False,
    }


def proposed_paper_size(decision: dict[str, Any]) -> float:
    """Alleen een onderzoeksvoorstel; deze laag kan geen positie openen."""
    if decision.get('action') != 'DOOR_NAAR_MENSELIJKE_JURY':
        return 0.0
    score = _finite(decision.get('discovery_score'))
    if score >= 80.0:
        return 500.0
    if score >= 65.0:
        return 400.0
    return 300.0
