import json
import os
import tempfile
import time
import urllib.request
from pathlib import Path
from statistics import median
from typing import Any, Callable, Dict, List, Optional, Tuple

STATE_FILE = Path("/var/data/diamond_market_crash_guard.json")
BITVAVO_TICKER_URL = "https://api.bitvavo.com/v2/ticker/price"

MARKETS = (
    "BTC-EUR",
    "ETH-EUR",
    "SOL-EUR",
    "XRP-EUR",
    "ADA-EUR",
)

MIN_REFERENCE_AGE_SECONDS = 120.0
MAX_REFERENCE_AGE_SECONDS = 720.0
SAMPLE_RETENTION_SECONDS = 1800.0
BLOCK_SECONDS = 900.0

BTC_CRASH_DROP_PCT = -1.50
BROAD_CRASH_DROP_PCT = -1.25
MEDIAN_CRASH_DROP_PCT = -1.00


def _to_price(value: Any) -> float:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def fetch_bitvavo_prices(timeout: float = 5.0) -> Dict[str, float]:
    request = urllib.request.Request(
        BITVAVO_TICKER_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": "Diamond-Trader-Crash-Guard/1.0",
        },
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if isinstance(payload, dict):
        payload = [payload]

    prices: Dict[str, float] = {}
    for row in payload if isinstance(payload, list) else []:
        if not isinstance(row, dict):
            continue
        market = str(row.get("market") or "").upper()
        if market not in MARKETS:
            continue
        price = _to_price(row.get("price"))
        if price > 0:
            prices[market] = price

    return prices


def _default_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "samples": [],
        "block_until": 0.0,
        "last_status": "INIT",
        "last_reason": "",
        "last_changes_pct": {},
        "last_checked_at": 0.0,
    }


def _load_state(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state is geen dictionary")
    except FileNotFoundError:
        return _default_state()
    except Exception:
        state = _default_state()
        state["last_status"] = "STATE_ERROR"
        state["last_reason"] = "crash_guard_state_unreadable"
        return state

    state = _default_state()
    state.update(data)
    if not isinstance(state.get("samples"), list):
        state["samples"] = []
    return state


def _atomic_write(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
    ) as handle:
        json.dump(state, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        temporary_name = handle.name
    os.replace(temporary_name, path)


def _valid_sample(row: Any) -> Optional[Tuple[float, Dict[str, float]]]:
    if not isinstance(row, dict):
        return None
    try:
        ts = float(row.get("ts") or 0.0)
    except (TypeError, ValueError):
        return None
    raw_prices = row.get("prices")
    if ts <= 0 or not isinstance(raw_prices, dict):
        return None

    prices: Dict[str, float] = {}
    for market in MARKETS:
        price = _to_price(raw_prices.get(market))
        if price > 0:
            prices[market] = price
    if not prices:
        return None
    return ts, prices


def _reference_sample(
    samples: List[Any],
    now_ts: float,
) -> Optional[Tuple[float, Dict[str, float]]]:
    candidates: List[Tuple[float, Dict[str, float]]] = []
    for row in samples:
        parsed = _valid_sample(row)
        if parsed is None:
            continue
        ts, prices = parsed
        age = now_ts - ts
        if MIN_REFERENCE_AGE_SECONDS <= age <= MAX_REFERENCE_AGE_SECONDS:
            candidates.append((ts, prices))

    if not candidates:
        return None

    return max(candidates, key=lambda item: item[0])


def _price_changes_pct(
    reference: Dict[str, float],
    current: Dict[str, float],
) -> Dict[str, float]:
    result: Dict[str, float] = {}
    for market in MARKETS:
        old = _to_price(reference.get(market))
        new = _to_price(current.get(market))
        if old <= 0 or new <= 0:
            continue
        result[market] = ((new - old) / old) * 100.0
    return result


def evaluate_crash(
    reference: Dict[str, float],
    current: Dict[str, float],
) -> Dict[str, Any]:
    changes = _price_changes_pct(reference, current)

    if "BTC-EUR" not in changes or len(changes) < 3:
        return {
            "crash": False,
            "data_ok": False,
            "reason": "insufficient_market_data",
            "changes_pct": changes,
        }

    btc_change = changes["BTC-EUR"]
    values = list(changes.values())
    broad_count = sum(
        value <= BROAD_CRASH_DROP_PCT
        for value in values
    )
    negative_count = sum(value < 0.0 for value in values)
    median_change = median(values)

    reasons: List[str] = []
    if btc_change <= BTC_CRASH_DROP_PCT:
        reasons.append("btc_fast_drop")
    if broad_count >= 3:
        reasons.append("broad_fast_drop")
    if (
        len(values) >= 4
        and negative_count >= 4
        and median_change <= MEDIAN_CRASH_DROP_PCT
    ):
        reasons.append("market_median_fast_drop")

    return {
        "crash": bool(reasons),
        "data_ok": True,
        "reason": "+".join(reasons) if reasons else "market_ok",
        "changes_pct": changes,
        "btc_change_pct": btc_change,
        "median_change_pct": median_change,
        "broad_drop_count": broad_count,
    }


def poll_market_crash_guard(
    state_path: Path = STATE_FILE,
    fetcher: Callable[[], Dict[str, float]] = fetch_bitvavo_prices,
    now_ts: Optional[float] = None,
) -> Dict[str, Any]:
    now_value = float(now_ts if now_ts is not None else time.time())
    state = _load_state(Path(state_path))

    try:
        current = fetcher()
    except Exception as exc:
        state["last_checked_at"] = now_value
        state["last_status"] = "DATA_ERROR"
        state["last_reason"] = f"ticker_fetch_failed:{type(exc).__name__}"
        _atomic_write(Path(state_path), state)
        return {
            "allow_long": False,
            "status": state["last_status"],
            "reason": state["last_reason"],
            "block_until": float(state.get("block_until") or 0.0),
            "changes_pct": {},
        }

    current = {
        market: _to_price(current.get(market))
        for market in MARKETS
        if _to_price(current.get(market)) > 0
    }

    old_samples = list(state.get("samples") or [])
    reference = _reference_sample(old_samples, now_value)

    retained: List[Dict[str, Any]] = []
    for row in old_samples:
        parsed = _valid_sample(row)
        if parsed is None:
            continue
        ts, prices = parsed
        if 0.0 <= (now_value - ts) <= SAMPLE_RETENTION_SECONDS:
            retained.append({"ts": ts, "prices": prices})

    retained.append({"ts": now_value, "prices": current})
    state["samples"] = retained[-20:]
    state["last_checked_at"] = now_value

    block_until = float(state.get("block_until") or 0.0)
    if block_until > now_value:
        state["last_status"] = "BLOCKED"
        if not state.get("last_reason"):
            state["last_reason"] = "crash_cooldown"
        _atomic_write(Path(state_path), state)
        return {
            "allow_long": False,
            "status": "BLOCKED",
            "reason": str(state.get("last_reason") or "crash_cooldown"),
            "block_until": block_until,
            "changes_pct": dict(state.get("last_changes_pct") or {}),
        }

    if "BTC-EUR" not in current or len(current) < 3:
        state["last_status"] = "DATA_INCOMPLETE"
        state["last_reason"] = "insufficient_current_market_data"
        state["last_changes_pct"] = {}
        _atomic_write(Path(state_path), state)
        return {
            "allow_long": False,
            "status": state["last_status"],
            "reason": state["last_reason"],
            "block_until": 0.0,
            "changes_pct": {},
        }

    if reference is None:
        state["last_status"] = "WARMUP"
        state["last_reason"] = "waiting_for_reference_sample"
        state["last_changes_pct"] = {}
        _atomic_write(Path(state_path), state)
        return {
            "allow_long": False,
            "status": "WARMUP",
            "reason": state["last_reason"],
            "block_until": 0.0,
            "changes_pct": {},
        }

    _, reference_prices = reference
    decision = evaluate_crash(reference_prices, current)
    state["last_changes_pct"] = decision.get("changes_pct") or {}

    if not decision.get("data_ok"):
        state["last_status"] = "DATA_INCOMPLETE"
        state["last_reason"] = str(decision.get("reason") or "insufficient_market_data")
        _atomic_write(Path(state_path), state)
        return {
            "allow_long": False,
            "status": state["last_status"],
            "reason": state["last_reason"],
            "block_until": 0.0,
            "changes_pct": state["last_changes_pct"],
        }

    if decision.get("crash"):
        block_until = now_value + BLOCK_SECONDS
        state["block_until"] = block_until
        state["last_status"] = "BLOCKED"
        state["last_reason"] = str(decision.get("reason") or "market_crash")
        _atomic_write(Path(state_path), state)
        return {
            "allow_long": False,
            "status": "BLOCKED",
            "reason": state["last_reason"],
            "block_until": block_until,
            "changes_pct": state["last_changes_pct"],
        }

    state["block_until"] = 0.0
    state["last_status"] = "OK"
    state["last_reason"] = "market_ok"
    _atomic_write(Path(state_path), state)
    return {
        "allow_long": True,
        "status": "OK",
        "reason": "market_ok",
        "block_until": 0.0,
        "changes_pct": state["last_changes_pct"],
    }


def self_test() -> None:
    base = {
        "BTC-EUR": 100.0,
        "ETH-EUR": 100.0,
        "SOL-EUR": 100.0,
        "XRP-EUR": 100.0,
        "ADA-EUR": 100.0,
    }

    normal = {market: 99.7 for market in MARKETS}
    decision = evaluate_crash(base, normal)
    assert decision["data_ok"] is True
    assert decision["crash"] is False

    btc_crash = dict(base)
    btc_crash["BTC-EUR"] = 98.4
    decision = evaluate_crash(base, btc_crash)
    assert decision["crash"] is True
    assert "btc_fast_drop" in decision["reason"]

    broad_crash = dict(base)
    broad_crash["BTC-EUR"] = 99.0
    broad_crash["ETH-EUR"] = 98.6
    broad_crash["SOL-EUR"] = 98.6
    broad_crash["XRP-EUR"] = 98.6
    decision = evaluate_crash(base, broad_crash)
    assert decision["crash"] is True
    assert "broad_fast_drop" in decision["reason"]

    incomplete = evaluate_crash(
        {"BTC-EUR": 100.0, "ETH-EUR": 100.0},
        {"BTC-EUR": 99.0, "ETH-EUR": 99.0},
    )
    assert incomplete["data_ok"] is False

    print("DIAMOND_MARKET_CRASH_GUARD_SELF_TEST_OK")


if __name__ == "__main__":
    self_test()
