#!/usr/bin/env python3
from typing import Any, Dict, Iterable, List, Tuple


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_asks(rows: Iterable[Any]) -> List[Tuple[float, float]]:
    asks: List[Tuple[float, float]] = []
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        price = _to_float(row[0], 0.0)
        amount = _to_float(row[1], 0.0)
        if price <= 0 or amount <= 0:
            continue
        asks.append((price, amount))
    asks.sort(key=lambda item: item[0])
    return asks


def evaluate_buy_liquidity(
    order_book: Dict[str, Any],
    stake_quote: float,
    *,
    max_price_impact_pct: float = 0.15,
    depth_band_pct: float = 0.25,
    min_depth_multiple: float = 2.0,
) -> Dict[str, Any]:
    """Fail-closed orderbook gate for a market BUY.

    The bot sizes a spot BUY from quote stake / current ask. To model that
    conservatively, this function converts the requested quote stake to a
    target base amount using the best current ask and walks the ask side of
    the book until that base amount is filled.
    """
    stake = _to_float(stake_quote, 0.0)
    max_impact = max(0.0, _to_float(max_price_impact_pct, 0.15))
    band_pct = max(0.0, _to_float(depth_band_pct, 0.25))
    min_multiple = max(1.0, _to_float(min_depth_multiple, 2.0))

    result: Dict[str, Any] = {
        "allow": False,
        "reason": "invalid_input",
        "stake_quote": stake,
        "best_ask": 0.0,
        "estimated_vwap": 0.0,
        "estimated_price_impact_pct": 0.0,
        "depth_band_quote": 0.0,
        "depth_multiple": 0.0,
        "estimated_quote_cost": 0.0,
    }

    if stake <= 0:
        return result

    asks = _normalize_asks((order_book or {}).get("asks") or [])
    if not asks:
        result["reason"] = "missing_asks"
        return result

    best_ask = asks[0][0]
    target_base = stake / best_ask
    remaining_base = target_base
    filled_base = 0.0
    spent_quote = 0.0

    for price, available_base in asks:
        take_base = min(remaining_base, available_base)
        if take_base <= 0:
            continue
        filled_base += take_base
        spent_quote += take_base * price
        remaining_base -= take_base
        if remaining_base <= 1e-12:
            break

    band_limit = best_ask * (1.0 + band_pct / 100.0)
    depth_band_quote = sum(
        price * amount
        for price, amount in asks
        if price <= band_limit + 1e-15
    )
    depth_multiple = depth_band_quote / stake if stake > 0 else 0.0

    result["best_ask"] = best_ask
    result["depth_band_quote"] = depth_band_quote
    result["depth_multiple"] = depth_multiple
    result["estimated_quote_cost"] = spent_quote

    if remaining_base > max(1e-12, target_base * 1e-9):
        result["reason"] = "insufficient_total_depth"
        return result

    if filled_base <= 0:
        result["reason"] = "zero_fill"
        return result

    vwap = spent_quote / filled_base
    impact_pct = ((vwap - best_ask) / best_ask) * 100.0

    result["estimated_vwap"] = vwap
    result["estimated_price_impact_pct"] = impact_pct

    if impact_pct > max_impact + 1e-12:
        result["reason"] = "price_impact_too_high"
        return result

    if depth_multiple + 1e-12 < min_multiple:
        result["reason"] = "insufficient_nearby_depth"
        return result

    result["allow"] = True
    result["reason"] = "liquidity_ok"
    return result


def self_test() -> None:
    healthy = {
        "asks": [
            [100.00, 2.0],
            [100.05, 2.0],
            [100.10, 2.0],
        ]
    }
    r = evaluate_buy_liquidity(healthy, 130.0)
    assert r["allow"] is True, r
    assert r["reason"] == "liquidity_ok", r

    thin = {"asks": [[100.00, 0.5]]}
    r = evaluate_buy_liquidity(thin, 130.0)
    assert r["allow"] is False, r
    assert r["reason"] == "insufficient_total_depth", r

    impact = {
        "asks": [
            [100.00, 0.10],
            [100.40, 2.00],
            [100.50, 2.00],
        ]
    }
    r = evaluate_buy_liquidity(impact, 130.0)
    assert r["allow"] is False, r
    assert r["reason"] == "price_impact_too_high", r

    nearby_thin = {
        "asks": [
            [100.00, 1.31],
            [101.00, 10.00],
        ]
    }
    r = evaluate_buy_liquidity(nearby_thin, 130.0)
    assert r["allow"] is False, r
    assert r["reason"] == "insufficient_nearby_depth", r

    r = evaluate_buy_liquidity({}, 130.0)
    assert r["allow"] is False, r
    assert r["reason"] == "missing_asks", r


if __name__ == "__main__":
    self_test()
    print("DIAMOND_LIQUIDITY_GATE_SELF_TEST_OK")
