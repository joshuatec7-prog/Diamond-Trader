from typing import Any, Dict


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {
        "1", "true", "yes", "ja", "on"
    }


def selective_candidate_key(row: Dict[str, Any]) -> str:
    return "|".join([
        str(row.get("symbol") or "").upper(),
        str(row.get("strategy") or ""),
        str(row.get("side") or "").upper(),
        str(row.get("candle_timestamp") or ""),
    ])


def selective_accepts(row: Dict[str, Any]) -> bool:
    """
    Canonieke SELECTIVE-regel.

    Geen execution, geen orders en geen instellingen.
    Deze functie bepaalt uitsluitend of een bestaand
    scanner-signaal tot SELECTIVE behoort.
    """
    if not to_bool(row.get("shadow_eligible"), False):
        return False

    side = str(row.get("side") or "").upper()
    strategy = str(row.get("strategy") or "")
    regime = str(row.get("market_regime") or "").upper()

    if side == "LONG" and strategy == "trend_breakout":
        return True

    if side == "SHORT" and regime == "BEARISH_WEAK":
        return True

    if side == "SHORT" and strategy in {
        "momentum",
        "pullback_retest",
    }:
        return True

    return False


def execution_signal(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Zet één goedgekeurd SELECTIVE scanner-signaal om
    naar een read-only execution-contract.
    """
    if not selective_accepts(row):
        raise ValueError("NOT_SELECTIVE")

    return {
        "candidate_key": selective_candidate_key(row),
        "detected_at": str(row.get("detected_at") or ""),
        "candle_timestamp": str(row.get("candle_timestamp") or ""),
        "symbol": str(row.get("symbol") or "").upper(),
        "side": str(row.get("side") or "").upper(),
        "strategy": str(row.get("strategy") or ""),
        "market_regime": str(row.get("market_regime") or ""),
        "score": to_float(row.get("score")),
        "entry_price": to_float(row.get("entry_price")),
        "take_profit": to_float(row.get("take_profit")),
        "stop_loss": to_float(row.get("stop_loss")),
        "spread_pct": to_float(row.get("spread_pct")),
        "reward_risk": to_float(row.get("reward_risk")),
        "selection_reason": str(
            row.get("selection_reason") or "UNKNOWN"
        ),
    }


def self_test() -> None:
    long_ok = {
        "symbol": "ENA/EUR",
        "strategy": "trend_breakout",
        "side": "LONG",
        "market_regime": "BULLISH",
        "shadow_eligible": True,
        "candle_timestamp": "2026-08-21T09:00:00+00:00",
    }

    short_ok = {
        "symbol": "BTC/EUR",
        "strategy": "momentum",
        "side": "SHORT",
        "market_regime": "BEARISH",
        "shadow_eligible": True,
        "candle_timestamp": "2026-08-21T09:00:00+00:00",
    }

    long_bad = {
        "symbol": "BTC/EUR",
        "strategy": "momentum",
        "side": "LONG",
        "market_regime": "BULLISH",
        "shadow_eligible": True,
    }

    assert selective_accepts(long_ok)
    assert selective_accepts(short_ok)
    assert not selective_accepts(long_bad)
    assert execution_signal(long_ok)["symbol"] == "ENA/EUR"


if __name__ == "__main__":
    self_test()
    print("DIAMOND_SELECTIVE_RULES_SELF_TEST_OK")
