#!/usr/bin/env python3
"""Read-only audit of Diamond Trader market-regime labels at real scanner signal times.

Purpose
-------
Check whether BULLISH/BULLISH_WEAK/NEUTRAL/BEARISH_WEAK/BEARISH labels point
in the right direction before we ever route LIVE strategy by regime.

Method
------
- source: /var/data/diamond_market_signals.csv
- last 7 days by default
- one observation per symbol + candle_timestamp + regime (deduplicated across strategies)
- public Bitvavo 15m candles only
- forward close-to-close returns at 1h and 4h
- bullish labels are correct when forward return > 0
- bearish labels are correct when forward return < 0
- neutral quality is reported as absolute movement, not forced into a direction
- also reports fast opposite-direction moves of >= 1.0% within 4h

No private API, orders, state writes, config changes or LIVE changes.
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import ccxt

SIGNALS = Path("/var/data/diamond_market_signals.csv")
TIMEFRAME_MS = 15 * 60 * 1000
REGIME_ORDER = ["BULLISH", "BULLISH_WEAK", "NEUTRAL", "BEARISH_WEAK", "BEARISH"]


def f(value: Any, default: float = 0.0) -> float:
    try:
        n = float(value)
        return n if math.isfinite(n) else default
    except (TypeError, ValueError):
        return default


def parse_dt(value: Any) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        return None


def load_observations(days: int) -> List[Dict[str, Any]]:
    if not SIGNALS.is_file():
        raise FileNotFoundError(SIGNALS)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    seen = set()
    out: List[Dict[str, Any]] = []
    with SIGNALS.open("r", encoding="utf-8-sig", newline="") as h:
        r = csv.DictReader(h)
        required = {"detected_at", "candle_timestamp", "symbol", "market_regime", "regime_strength"}
        missing = required - set(r.fieldnames or [])
        if missing:
            raise RuntimeError("CSV mist kolommen: " + ", ".join(sorted(missing)))
        for raw in r:
            detected = parse_dt(raw.get("detected_at"))
            candle = parse_dt(raw.get("candle_timestamp"))
            regime = str(raw.get("market_regime") or "").strip().upper()
            symbol = str(raw.get("symbol") or "").strip().upper()
            if detected is None or detected < cutoff or candle is None or not symbol:
                continue
            if regime not in REGIME_ORDER:
                continue
            key = (symbol, int(candle.timestamp()), regime)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "symbol": symbol,
                "candle_dt": candle,
                "regime": regime,
                "strength": f(raw.get("regime_strength")),
            })
    out.sort(key=lambda x: x["candle_dt"])
    return out


def fetch_candles(exchange: ccxt.Exchange, symbol: str, since_ms: int, until_ms: int) -> List[List[Any]]:
    rows: List[List[Any]] = []
    cursor = max(0, since_ms)
    while cursor <= until_ms:
        batch = exchange.fetch_ohlcv(symbol, timeframe="15m", since=cursor, limit=500) or []
        if not batch:
            break
        rows.extend(c for c in batch if c and len(c) >= 6 and int(c[0]) <= until_ms)
        last = int(batch[-1][0])
        nxt = last + TIMEFRAME_MS
        if nxt <= cursor or len(batch) < 500:
            break
        cursor = nxt
        time.sleep(max(0.0, float(getattr(exchange, "rateLimit", 0) or 0) / 1000.0))
    unique = {int(c[0]): c for c in rows}
    return [unique[k] for k in sorted(unique)]


def close_at_or_after(candles: List[List[Any]], target_ms: int) -> Optional[float]:
    for c in candles:
        if int(c[0]) >= target_ms:
            value = f(c[4])
            return value if value > 0 else None
    return None


def adverse_4h(candles: List[List[Any]], start_ms: int, entry: float, bullish: bool) -> float:
    end_ms = start_ms + 4 * 60 * 60 * 1000
    worst = 0.0
    for c in candles:
        stamp = int(c[0])
        if stamp <= start_ms or stamp > end_ms:
            continue
        if bullish:
            low = f(c[3])
            if low > 0:
                worst = min(worst, (low / entry - 1.0) * 100.0)
        else:
            high = f(c[2])
            if high > 0:
                worst = max(worst, (high / entry - 1.0) * 100.0)
    return worst


def median(values: List[float]) -> float:
    return statistics.median(values) if values else 0.0


def mean(values: List[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def audit(days: int) -> int:
    obs = load_observations(days)
    if not obs:
        print("GEEN regime-observaties gevonden")
        return 0

    ex = ccxt.bitvavo({"enableRateLimit": True})
    ex.load_markets()
    now_ms = ex.milliseconds()
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in obs:
        grouped[row["symbol"]].append(row)

    evaluated: List[Dict[str, Any]] = []
    errors: Dict[str, str] = {}
    for i, (symbol, items) in enumerate(sorted(grouped.items()), 1):
        start_ms = int(min(x["candle_dt"] for x in items).timestamp() * 1000) - TIMEFRAME_MS
        end_ms = min(now_ms, int(max(x["candle_dt"] for x in items).timestamp() * 1000) + 5 * 60 * 60 * 1000)
        try:
            candles = fetch_candles(ex, symbol, start_ms, end_ms)
        except Exception as exc:
            errors[symbol] = f"{type(exc).__name__}: {exc}"
            continue
        for item in items:
            start = int(item["candle_dt"].timestamp() * 1000)
            entry = close_at_or_after(candles, start)
            c1 = close_at_or_after(candles, start + 60 * 60 * 1000)
            c4 = close_at_or_after(candles, start + 4 * 60 * 60 * 1000)
            if not entry or not c1 or not c4:
                continue
            r1 = (c1 / entry - 1.0) * 100.0
            r4 = (c4 / entry - 1.0) * 100.0
            regime = item["regime"]
            bull = regime in {"BULLISH", "BULLISH_WEAK"}
            bear = regime in {"BEARISH", "BEARISH_WEAK"}
            adv = adverse_4h(candles, start, entry, bull) if bull else adverse_4h(candles, start, entry, False) if bear else 0.0
            evaluated.append({**item, "r1": r1, "r4": r4, "adverse": adv})
        if i < len(grouped):
            time.sleep(max(0.0, float(getattr(ex, "rateLimit", 0) or 0) / 1000.0))

    print("=" * 106)
    print(" DIAMOND MARKET REGIME SWITCH QUALITY AUDIT")
    print("=" * 106)
    print(f"Periode             : laatste {days} dagen")
    print(f"Unieke observaties  : {len(obs)}")
    print(f"Beoordeeld          : {len(evaluated)}")
    print(f"Markten             : {len(grouped)}")
    print(f"API-fouten          : {len(errors)}")
    print("Bron                : echte scanner-regimelabels bij scanner-signalen")
    print("Forward             : close-to-close 1u en 4u")

    print("\n=== REGIMEKWALITEIT ===")
    for regime in REGIME_ORDER:
        rows = [x for x in evaluated if x["regime"] == regime]
        if not rows:
            print(f"{regime:14} n=0")
            continue
        r1 = [x["r1"] for x in rows]
        r4 = [x["r4"] for x in rows]
        if regime in {"BULLISH", "BULLISH_WEAK"}:
            h1 = sum(x > 0 for x in r1) / len(r1) * 100.0
            h4 = sum(x > 0 for x in r4) / len(r4) * 100.0
            bad = sum(x["adverse"] <= -1.0 for x in rows)
            detail = f"richting-hit 1u={h1:5.1f}% 4u={h4:5.1f}% | >=1% tegenmove4u={bad}/{len(rows)}"
        elif regime in {"BEARISH", "BEARISH_WEAK"}:
            h1 = sum(x < 0 for x in r1) / len(r1) * 100.0
            h4 = sum(x < 0 for x in r4) / len(r4) * 100.0
            bad = sum(x["adverse"] >= 1.0 for x in rows)
            detail = f"richting-hit 1u={h1:5.1f}% 4u={h4:5.1f}% | >=1% tegenmove4u={bad}/{len(rows)}"
        else:
            a1 = [abs(x) for x in r1]
            a4 = [abs(x) for x in r4]
            detail = f"abs move med 1u={median(a1):.3f}% 4u={median(a4):.3f}% | >1%/4u={sum(x>1 for x in a4)}/{len(rows)}"
        print(
            f"{regime:14} n={len(rows):4d} | "
            f"1u avg={mean(r1):+7.3f}% med={median(r1):+7.3f}% | "
            f"4u avg={mean(r4):+7.3f}% med={median(r4):+7.3f}% | {detail}"
        )

    directional = [x for x in evaluated if x["regime"] != "NEUTRAL"]
    correct4 = 0
    for x in directional:
        if x["regime"] in {"BULLISH", "BULLISH_WEAK"} and x["r4"] > 0:
            correct4 += 1
        elif x["regime"] in {"BEARISH", "BEARISH_WEAK"} and x["r4"] < 0:
            correct4 += 1
    acc = correct4 / len(directional) * 100.0 if directional else 0.0

    print("\n=== SCHAKELBEOORDELING ===")
    print(f"Directionele labels 4u correct : {correct4}/{len(directional)} = {acc:.1f}%")
    if len(directional) < 20:
        verdict = "TE WEINIG DATA"
    elif acc >= 60.0:
        verdict = "BRUIKBAAR ALS ROUTING-SIGNAAL, NOG NIET ALLEENSTAAND"
    elif acc >= 52.0:
        verdict = "ZWAK / EXTRA BEVESTIGING NODIG"
    else:
        verdict = "ONVOLDOENDE VOOR STRATEGY-ROUTING"
    print(f"Conclusie                  : {verdict}")
    print("LIVE/config                : ONGEWIJZIGD")
    print("Orders/private API         : NEE")

    if errors:
        print("\nAPI-fouten (max 5):")
        for symbol, error in list(errors.items())[:5]:
            print(f"  {symbol}: {error}")
    return 0


def self_test() -> int:
    assert REGIME_ORDER[0] == "BULLISH"
    assert REGIME_ORDER[-1] == "BEARISH"
    assert abs(median([1.0, 3.0, 2.0]) - 2.0) < 1e-12
    assert abs(mean([1.0, 3.0]) - 2.0) < 1e-12
    print("DIAMOND_REGIME_SWITCH_QUALITY_AUDIT_SELF_TEST_OK")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()
    return self_test() if args.self_test else audit(max(1, args.days))


if __name__ == "__main__":
    raise SystemExit(main())
