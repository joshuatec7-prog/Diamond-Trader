#!/usr/bin/env python3
"""
Diamond Trader EVENT_RATE_REGIME_GATE research v1.0.

Read-only onderzoek op bestaande scanner-signalen en de Early Entry collector.
Geen orders, private API, netwerkcalls, config- of LIVE-wijzigingen.

Vaste hypothese vooraf
----------------------
- Alleen huidige SELECTIVE-routes die al shadow_eligible=True zijn.
- Significante prijs-event: absolute 1m mid-price move >= 0.10%.
- FAST window: laatste 15 minuten voor detected_at.
- BASELINE: de 60 minuten direct vóór die FAST-window.
- Normaliseer baseline naar 15 minuten door baseline-count / 4.
- EVENT_RATE_ALIGNED wanneer:
    * minimaal 2 significante events in signaalrichting in FAST;
    * FAST directionele event-rate >= 1.5x de baseline-rate;
    * meer directionele dan tegengestelde events in FAST.
- EVENT_RATE_OPPOSED is exact dezelfde regel in de tegengestelde richting.
- Anders EVENT_RATE_NORMAL.

De drempels staan vooraf vast en worden niet geoptimaliseerd op de uitkomst.
We vergelijken forward uitvoerbare richtingmoves na 15m en 60m en, waar
beschikbaar, de bestaande gesloten CURRENT shadow-PnL.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VERSION = "1.0"
DATA = Path("/var/data")
EARLY = DATA / "diamond_early_entry" / "early_entry_samples_v1_3_1.csv"
SIGNALS = DATA / "diamond_market_signals.csv"
TRADES = DATA / "diamond_scanner_selective_shadow_trades.csv"

CORE = {"BTC/EUR", "ETH/EUR", "SOL/EUR", "XRP/EUR", "ADA/EUR"}
EVENT_THRESHOLD_PCT = 0.10
FAST_MINUTES = 15
BASELINE_MINUTES = 60
ACCEL_FACTOR = 1.50
MIN_FAST_DIRECTION_EVENTS = 2
MAX_MATCH_SECONDS = 75.0


def f(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def b(v: Any) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "ja", "on"}


def ts(v: Any) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).timestamp()
    except Exception:
        try:
            n = float(v)
            return n / 1000.0 if n > 1e12 else n
        except Exception:
            return None


def selective(row: Dict[str, str]) -> bool:
    if not b(row.get("shadow_eligible")):
        return False
    side = str(row.get("side") or "").upper()
    strategy = str(row.get("strategy") or "").lower()
    regime = str(row.get("market_regime") or "").upper()
    if side == "LONG":
        return strategy == "trend_breakout"
    if side == "SHORT":
        return regime == "BEARISH_WEAK" or strategy in {"momentum", "pullback_retest"}
    return False


def key(row: Dict[str, str]) -> str:
    return "|".join([
        str(row.get("symbol") or "").upper(),
        str(row.get("strategy") or ""),
        str(row.get("side") or "").upper(),
        str(row.get("candle_timestamp") or ""),
    ])


def load_early() -> Tuple[Dict[str, List[Dict[str, float]]], int, float, float]:
    if not EARLY.is_file():
        raise FileNotFoundError(EARLY)

    # Eén representatieve sample per UTC-minuut: laatste sample in die minuut.
    minute_map: Dict[str, Dict[int, Dict[str, float]]] = defaultdict(dict)
    rows = 0
    first = math.inf
    last = 0.0

    with EARLY.open("r", encoding="utf-8-sig", newline="") as h:
        r = csv.DictReader(h)
        required = {"timestamp_utc", "symbol", "bid", "ask"}
        missing = required - set(r.fieldnames or [])
        if missing:
            raise RuntimeError("Early Entry CSV mist: " + ", ".join(sorted(missing)))
        for row in r:
            rows += 1
            sym = str(row.get("symbol") or "").upper()
            if sym not in CORE:
                continue
            when = ts(row.get("timestamp_utc"))
            bid = f(row.get("bid"))
            ask = f(row.get("ask"))
            if when is None or bid <= 0 or ask <= 0:
                continue
            bucket = int(when // 60)
            minute_map[sym][bucket] = {
                "ts": when,
                "bid": bid,
                "ask": ask,
                "mid": (bid + ask) / 2.0,
            }
            first = min(first, when)
            last = max(last, when)

    out: Dict[str, List[Dict[str, float]]] = {}
    for sym, buckets in minute_map.items():
        seq = [buckets[k] for k in sorted(buckets)]
        prev: Optional[Dict[str, float]] = None
        for item in seq:
            item["ret1m"] = 0.0
            if prev is not None and prev["mid"] > 0 and 30 <= item["ts"] - prev["ts"] <= 90:
                item["ret1m"] = (item["mid"] / prev["mid"] - 1.0) * 100.0
            prev = item
        out[sym] = seq

    if not out:
        raise RuntimeError("Geen bruikbare Early Entry data")
    return out, rows, first, last


def nearest(seq: List[Dict[str, float]], target: float) -> Optional[Dict[str, float]]:
    if not seq:
        return None
    times = [x["ts"] for x in seq]
    i = bisect.bisect_left(times, target)
    choices: List[Dict[str, float]] = []
    if i < len(seq):
        choices.append(seq[i])
    if i > 0:
        choices.append(seq[i - 1])
    if not choices:
        return None
    best = min(choices, key=lambda x: abs(x["ts"] - target))
    return best if abs(best["ts"] - target) <= MAX_MATCH_SECONDS else None


def window_events(seq: List[Dict[str, float]], start: float, end: float) -> Tuple[int, int]:
    times = [x["ts"] for x in seq]
    lo = bisect.bisect_left(times, start)
    hi = bisect.bisect_left(times, end)
    up = down = 0
    for item in seq[lo:hi]:
        r = item.get("ret1m", 0.0)
        if r >= EVENT_THRESHOLD_PCT:
            up += 1
        elif r <= -EVENT_THRESHOLD_PCT:
            down += 1
    return up, down


def classify(side: str, fast_up: int, fast_down: int, base_up: int, base_down: int) -> str:
    if side == "LONG":
        d_fast, o_fast = fast_up, fast_down
        d_base, o_base = base_up / 4.0, base_down / 4.0
    else:
        d_fast, o_fast = fast_down, fast_up
        d_base, o_base = base_down / 4.0, base_up / 4.0

    aligned = (
        d_fast >= MIN_FAST_DIRECTION_EVENTS
        and d_fast >= ACCEL_FACTOR * d_base
        and d_fast > o_fast
    )
    opposed = (
        o_fast >= MIN_FAST_DIRECTION_EVENTS
        and o_fast >= ACCEL_FACTOR * o_base
        and o_fast > d_fast
    )
    if aligned:
        return "ALIGNED"
    if opposed:
        return "OPPOSED"
    return "NORMAL"


def load_trades() -> Dict[str, Dict[str, str]]:
    result: Dict[str, Dict[str, str]] = {}
    if not TRADES.is_file():
        return result
    with TRADES.open("r", encoding="utf-8-sig", newline="") as h:
        for row in csv.DictReader(h):
            if str(row.get("variant") or "").upper() != "CURRENT":
                continue
            if not str(row.get("closed_at") or "").strip():
                continue
            k = str(row.get("candidate_key") or "")
            if k:
                result[k] = row
    return result


def load_signals(first: float, last: float, trades: Dict[str, Dict[str, str]]) -> List[Dict[str, Any]]:
    if not SIGNALS.is_file():
        raise FileNotFoundError(SIGNALS)
    result: Dict[str, Dict[str, Any]] = {}
    with SIGNALS.open("r", encoding="utf-8-sig", newline="") as h:
        r = csv.DictReader(h)
        required = {"detected_at", "symbol", "strategy", "side", "shadow_eligible"}
        missing = required - set(r.fieldnames or [])
        if missing:
            raise RuntimeError("Signals CSV mist: " + ", ".join(sorted(missing)))
        for row in r:
            sym = str(row.get("symbol") or "").upper()
            if sym not in CORE or not selective(row):
                continue
            when = ts(row.get("detected_at"))
            if when is None or not (first + (FAST_MINUTES + BASELINE_MINUTES) * 60 <= when <= last - 60 * 60):
                continue
            k = key(row)
            item: Dict[str, Any] = dict(row)
            item.update({"key": k, "ts": when, "trade": trades.get(k)})
            result[k] = item
    return sorted(result.values(), key=lambda x: x["ts"])


def direction_move(side: str, entry: Dict[str, float], future: Dict[str, float]) -> Optional[float]:
    if side == "LONG":
        if entry["ask"] <= 0 or future["bid"] <= 0:
            return None
        return (future["bid"] / entry["ask"] - 1.0) * 100.0
    if entry["bid"] <= 0 or future["ask"] <= 0:
        return None
    return (entry["bid"] / future["ask"] - 1.0) * 100.0


def pft(rows: List[Dict[str, Any]]) -> str:
    vals = [f(x["trade"].get("net_pnl_eur")) for x in rows if x.get("trade")]
    if not vals:
        return "closed=0 PF=n/a PnL=€+0.000"
    gp = sum(x for x in vals if x > 0)
    gl = abs(sum(x for x in vals if x < 0))
    pf = gp / gl if gl > 0 else (math.inf if gp > 0 else None)
    pfs = "INF" if pf is not None and math.isinf(pf) else ("n/a" if pf is None else f"{pf:.3f}")
    return f"closed={len(vals)} W/L={sum(x>0 for x in vals)}/{sum(x<0 for x in vals)} PnL=€{sum(vals):+.3f} PF={pfs}"


def move_stats(rows: List[Dict[str, Any]], field: str) -> str:
    vals = [x[field] for x in rows if x.get(field) is not None]
    if not vals:
        return "n=0"
    return (
        f"n={len(vals)} avg={statistics.mean(vals):+.4f}% "
        f"med={statistics.median(vals):+.4f}% positief={100*sum(x>0 for x in vals)/len(vals):.1f}%"
    )


def self_test() -> int:
    assert classify("LONG", 4, 1, 4, 4) == "ALIGNED"
    assert classify("LONG", 1, 4, 4, 4) == "OPPOSED"
    assert classify("SHORT", 1, 4, 4, 4) == "ALIGNED"
    assert classify("SHORT", 4, 1, 4, 4) == "OPPOSED"
    assert classify("LONG", 1, 1, 4, 4) == "NORMAL"
    print("DIAMOND_EVENT_RATE_REGIME_GATE_RESEARCH_SELF_TEST_OK")
    return 0


def run() -> int:
    early, raw_rows, first, last = load_early()
    trade_map = load_trades()
    signals = load_signals(first, last, trade_map)

    evaluated: List[Dict[str, Any]] = []
    for s in signals:
        seq = early.get(str(s.get("symbol") or "").upper(), [])
        entry = nearest(seq, s["ts"])
        f15 = nearest(seq, s["ts"] + 15 * 60)
        f60 = nearest(seq, s["ts"] + 60 * 60)
        if entry is None or f15 is None or f60 is None:
            continue
        fast_start = s["ts"] - FAST_MINUTES * 60
        base_start = fast_start - BASELINE_MINUTES * 60
        fu, fd = window_events(seq, fast_start, s["ts"])
        bu, bd = window_events(seq, base_start, fast_start)
        side = str(s.get("side") or "").upper()
        s["label"] = classify(side, fu, fd, bu, bd)
        s["fast_up"], s["fast_down"] = fu, fd
        s["base_up"], s["base_down"] = bu, bd
        s["move15"] = direction_move(side, entry, f15)
        s["move60"] = direction_move(side, entry, f60)
        evaluated.append(s)

    print("=" * 112)
    print(f" DIAMOND EVENT RATE REGIME GATE RESEARCH v{VERSION}")
    print("=" * 112)
    print(f"Early Entry samples gelezen : {raw_rows}")
    print(f"SELECTIVE eligible signalen : {len(signals)}")
    print(f"Volledig beoordeeld         : {len(evaluated)}")
    print(f"Event threshold             : |1m move| >= {EVENT_THRESHOLD_PCT:.2f}%")
    print(f"FAST / BASELINE             : {FAST_MINUTES}m / voorafgaande {BASELINE_MINUTES}m")
    print(f"Acceleratie                 : >= {ACCEL_FACTOR:.2f}x baseline-rate en minimaal {MIN_FAST_DIRECTION_EVENTS} richting-events")
    print()

    for label in ("ALL", "ALIGNED", "NORMAL", "OPPOSED"):
        rows = evaluated if label == "ALL" else [x for x in evaluated if x["label"] == label]
        print(f"=== {label} ===")
        print(f"signalen={len(rows)} | {pft(rows)}")
        print(f"+15m richtingmove: {move_stats(rows, 'move15')}")
        print(f"+60m richtingmove: {move_stats(rows, 'move60')}")

    print("\n=== PER RICHTING ===")
    for side in ("LONG", "SHORT"):
        for label in ("ALL", "ALIGNED", "NORMAL", "OPPOSED"):
            rows = [x for x in evaluated if str(x.get("side") or "").upper() == side and (label == "ALL" or x["label"] == label)]
            print(f"{side:5} {label:8} n={len(rows):3d} | 15m {move_stats(rows, 'move15')} | 60m {move_stats(rows, 'move60')}")

    aligned = [x for x in evaluated if x["label"] == "ALIGNED"]
    print("\n=== LAATSTE ALIGNED SIGNALEN ===")
    for x in aligned[-10:]:
        print(
            f"{str(x.get('symbol') or ''):10} {str(x.get('side') or ''):5} {str(x.get('strategy') or ''):16} "
            f"fast U/D={x['fast_up']}/{x['fast_down']} base U/D={x['base_up']}/{x['base_down']} "
            f"15m={x['move15']:+.3f}% 60m={x['move60']:+.3f}%"
        )

    print("\n=== OORDEELREGEL ===")
    if len(aligned) < 20:
        print(f"ONVOLDOENDE BEWIJS: ALIGNED n={len(aligned)} < 20. Niet als hard gate invoeren.")
    else:
        all15 = [x["move15"] for x in evaluated if x.get("move15") is not None]
        al15 = [x["move15"] for x in aligned if x.get("move15") is not None]
        all60 = [x["move60"] for x in evaluated if x.get("move60") is not None]
        al60 = [x["move60"] for x in aligned if x.get("move60") is not None]
        improved = al15 and all15 and al60 and all60 and statistics.mean(al15) > statistics.mean(all15) and statistics.mean(al60) > statistics.mean(all60)
        print("POSITIEF RESEARCHSIGNAAL" if improved else "GEEN CONSISTENTE VERBETERING: niet als hard gate invoeren.")

    print("\n=== VEILIGHEID ===")
    print("Orders/private API : NEE")
    print("Netwerkcalls        : NEE")
    print("Config/strategie    : ONGEWIJZIGD")
    print("LIVE                : ONGEWIJZIGD")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    return self_test() if a.self_test else run()


if __name__ == "__main__":
    raise SystemExit(main())
