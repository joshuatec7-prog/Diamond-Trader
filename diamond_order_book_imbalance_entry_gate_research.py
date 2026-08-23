#!/usr/bin/env python3
"""
Diamond Trader ORDER_BOOK_IMBALANCE_ENTRY_GATE research v1.0.

Read-only onderzoek op bestaande SELECTIVE-shadowtrades en de bestaande
Early Entry collector. Geen orders, private API, netwerkcalls of LIVE-wijziging.

Vaste hypothese vooraf:
- meet uitsluitend de laatste 30 seconden VOOR detected_at;
- ORDER_BOOK_ALIGNED wanneer zowel mediane orderboek-imbalance als mediane
  60s trade-imbalance minimaal +0.10 (LONG) of maximaal -0.10 (SHORT) zijn;
- ORDER_BOOK_OPPOSED wanneer beide minimaal 0.10 de andere kant op wijzen;
- anders MIXED;
- dezelfde drempel blijft voor alle routes staan: niet optimaliseren op uitkomst.

De analyse vergelijkt exact dezelfde gesloten CURRENT-trades en rapporteert
netto PnL/PF plus uitvoerbare richtingmove na 1 en 5 minuten.
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
from typing import Any, Dict, List, Optional

VERSION = "1.0"
DATA_DIR = Path("/var/data")
TRADES_FILE = DATA_DIR / "diamond_scanner_selective_shadow_trades.csv"
EARLY_FILE = DATA_DIR / "diamond_early_entry" / "early_entry_samples_v1_3_1.csv"
CORE_SYMBOLS = {"BTC/EUR", "ETH/EUR", "SOL/EUR", "XRP/EUR", "ADA/EUR"}
THRESHOLD = 0.10
MICRO_WINDOW_SECONDS = 30.0
FUTURE_TOLERANCE_SECONDS = 22.0


def f(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def dt(v: Any) -> Optional[datetime]:
    try:
        x = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if x.tzinfo is None:
            x = x.replace(tzinfo=timezone.utc)
        return x.astimezone(timezone.utc)
    except Exception:
        return None


def selective_accepts(row: Dict[str, str]) -> bool:
    side = str(row.get("side") or "").upper()
    strategy = str(row.get("strategy") or "").lower()
    regime = str(row.get("market_regime") or "").upper()
    if side == "LONG":
        return strategy == "trend_breakout"
    if side == "SHORT":
        return regime == "BEARISH_WEAK" or strategy in {"momentum", "pullback_retest"}
    return False


def load_targets() -> List[Dict[str, Any]]:
    if not TRADES_FILE.exists():
        raise SystemExit(f"FOUT: ontbreekt: {TRADES_FILE}")
    out: List[Dict[str, Any]] = []
    seen = set()
    with TRADES_FILE.open(newline="", encoding="utf-8-sig") as h:
        for row in csv.DictReader(h):
            if str(row.get("variant") or "").upper() != "CURRENT":
                continue
            if not str(row.get("closed_at") or "").strip():
                continue
            if not selective_accepts(row):
                continue
            symbol = str(row.get("symbol") or "")
            if symbol not in CORE_SYMBOLS:
                continue
            when = dt(row.get("detected_at"))
            if when is None:
                continue
            key = str(row.get("candidate_key") or "") or (
                symbol,
                str(row.get("strategy") or ""),
                str(row.get("side") or ""),
                when.isoformat(),
            )
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "symbol": symbol,
                "strategy": str(row.get("strategy") or ""),
                "side": str(row.get("side") or "").upper(),
                "regime": str(row.get("market_regime") or ""),
                "detected": when,
                "ts": when.timestamp(),
                "pnl": f(row.get("net_pnl_eur")),
                "exit_reason": str(row.get("exit_reason") or ""),
                "micro": [],
                "future": {60: None, 300: None},
            })
    out.sort(key=lambda x: x["ts"])
    return out


def scan_early(targets: List[Dict[str, Any]]) -> int:
    if not EARLY_FILE.exists():
        raise SystemExit(f"FOUT: ontbreekt: {EARLY_FILE}")
    by_symbol: Dict[str, List[int]] = defaultdict(list)
    for idx, t in enumerate(targets):
        by_symbol[t["symbol"]].append(idx)
    times = {s: [targets[i]["ts"] for i in ids] for s, ids in by_symbol.items()}
    rows = 0
    with EARLY_FILE.open(newline="", encoding="utf-8-sig") as h:
        for row in csv.DictReader(h):
            rows += 1
            symbol = str(row.get("symbol") or "")
            ids = by_symbol.get(symbol)
            if not ids:
                continue
            when = dt(row.get("timestamp_utc"))
            if when is None:
                continue
            st = when.timestamp()
            tslist = times[symbol]

            # Alleen samples in de laatste 30 seconden VOOR detected_at.
            left = bisect.bisect_left(tslist, st)
            right = bisect.bisect_right(tslist, st + MICRO_WINDOW_SECONDS)
            sample = {
                "ts": st,
                "bid": f(row.get("bid")),
                "ask": f(row.get("ask")),
                "book": f(row.get("book_imbalance")),
                "trade": f(row.get("trade_imbalance_60s")),
            }
            for pos in range(left, right):
                idx = ids[pos]
                delta = targets[idx]["ts"] - st
                if -1e-6 <= delta <= MICRO_WINDOW_SECONDS + 1e-6:
                    targets[idx]["micro"].append(sample)

            # Uitvoerbare mark rond +1m en +5m, dichtstbijzijnde sample.
            for offset in (60, 300):
                wanted_target_ts = st - offset
                pos = bisect.bisect_left(tslist, wanted_target_ts)
                for p in (pos - 1, pos):
                    if p < 0 or p >= len(ids):
                        continue
                    idx = ids[p]
                    diff = abs(st - (targets[idx]["ts"] + offset))
                    if diff > FUTURE_TOLERANCE_SECONDS:
                        continue
                    old = targets[idx]["future"][offset]
                    if old is None or diff < old["diff"]:
                        targets[idx]["future"][offset] = {
                            "diff": diff,
                            "bid": sample["bid"],
                            "ask": sample["ask"],
                        }
    return rows


def classify(t: Dict[str, Any]) -> Optional[str]:
    samples = t["micro"]
    if not samples:
        return None
    book = statistics.median(x["book"] for x in samples)
    trade = statistics.median(x["trade"] for x in samples)
    sign = 1.0 if t["side"] == "LONG" else -1.0
    bdir = sign * book
    tdir = sign * trade
    if bdir >= THRESHOLD and tdir >= THRESHOLD:
        return "ALIGNED"
    if bdir <= -THRESHOLD and tdir <= -THRESHOLD:
        return "OPPOSED"
    return "MIXED"


def pf(rows: List[Dict[str, Any]]) -> Optional[float]:
    gp = sum(max(0.0, x["pnl"]) for x in rows)
    gl = abs(sum(min(0.0, x["pnl"]) for x in rows))
    if gl > 0:
        return gp / gl
    if gp > 0:
        return math.inf
    return None


def pft(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    if math.isinf(v):
        return "INF"
    return f"{v:.3f}"


def stats(rows: List[Dict[str, Any]]) -> str:
    w = sum(x["pnl"] > 0 for x in rows)
    l = sum(x["pnl"] < 0 for x in rows)
    pnl = sum(x["pnl"] for x in rows)
    avg = pnl / len(rows) if rows else 0.0
    return f"n={len(rows):2d} W/L={w}/{l} PnL=€{pnl:+.3f} PF={pft(pf(rows))} AVG=€{avg:+.3f}"


def direction_move(t: Dict[str, Any], offset: int) -> Optional[float]:
    if not t["micro"] or t["future"].get(offset) is None:
        return None
    latest = max(t["micro"], key=lambda x: x["ts"])
    future = t["future"][offset]
    if t["side"] == "LONG":
        entry = latest["ask"]
        exitp = future["bid"]
        if entry <= 0 or exitp <= 0:
            return None
        return (exitp / entry - 1.0) * 100.0
    entry = latest["bid"]
    cover = future["ask"]
    if entry <= 0 or cover <= 0:
        return None
    return (entry / cover - 1.0) * 100.0


def move_text(rows: List[Dict[str, Any]], offset: int) -> str:
    vals = [direction_move(x, offset) for x in rows]
    vals = [x for x in vals if x is not None]
    if not vals:
        return "n=0"
    return (
        f"n={len(vals)} avg={statistics.mean(vals):+.4f}% "
        f"med={statistics.median(vals):+.4f}% positief={100*sum(x>0 for x in vals)/len(vals):.1f}%"
    )


def self_test() -> int:
    base = {"side": "LONG", "micro": [{"book": 0.2, "trade": 0.3}]}
    assert classify(base) == "ALIGNED"
    assert classify({"side": "LONG", "micro": [{"book": -0.2, "trade": -0.3}]}) == "OPPOSED"
    assert classify({"side": "SHORT", "micro": [{"book": -0.2, "trade": -0.3}]}) == "ALIGNED"
    assert classify({"side": "SHORT", "micro": [{"book": 0.2, "trade": 0.3}]}) == "OPPOSED"
    assert classify({"side": "LONG", "micro": [{"book": 0.2, "trade": -0.3}]}) == "MIXED"
    print("DIAMOND_ORDER_BOOK_IMBALANCE_ENTRY_GATE_RESEARCH_SELF_TEST_OK")
    return 0


def run() -> int:
    targets = load_targets()
    source_rows = scan_early(targets)
    matched = [t for t in targets if t["micro"]]
    for t in matched:
        t["label"] = classify(t)

    print("=" * 108)
    print(f" DIAMOND ORDER BOOK IMBALANCE ENTRY GATE RESEARCH v{VERSION}")
    print("=" * 108)
    print(f"Early Entry samples gelezen : {source_rows}")
    print(f"SELECTIVE CURRENT closed op 5 kernmarkten : {len(targets)}")
    print(f"Micro-window gematcht       : {len(matched)}")
    print(f"Window                      : laatste {MICRO_WINDOW_SECONDS:.0f}s vóór detected_at")
    print(f"Vaste alignment-drempel     : {THRESHOLD:.2f} voor book EN trade imbalance")
    print("Kernmarkten                 : BTC/ETH/SOL/XRP/ADA EUR")
    print()

    print("=== EXACT DEZELFDE GESLOTEN TRADES ===")
    print("ALL      ", stats(matched))
    for label in ("ALIGNED", "MIXED", "OPPOSED"):
        group = [t for t in matched if t.get("label") == label]
        print(f"{label:9}", stats(group))
        print(f"           +1m move: {move_text(group, 60)}")
        print(f"           +5m move: {move_text(group, 300)}")
    print()

    print("=== PER RICHTING ===")
    for side in ("LONG", "SHORT"):
        base = [t for t in matched if t["side"] == side]
        aligned = [t for t in base if t.get("label") == "ALIGNED"]
        print(f"{side:5} ALL     {stats(base)}")
        print(f"{side:5} ALIGNED {stats(aligned)}")
    print()

    print("=== ALIGNED TRADES ===")
    aligned = [t for t in matched if t.get("label") == "ALIGNED"]
    if not aligned:
        print("GEEN")
    else:
        for t in aligned[-20:]:
            samples = t["micro"]
            book = statistics.median(x["book"] for x in samples)
            trade = statistics.median(x["trade"] for x in samples)
            print(
                f"{t['symbol']:8} {t['side']:5} {t['strategy'][:16]:16} "
                f"book={book:+.3f} trade={trade:+.3f} samples={len(samples)} "
                f"{t['exit_reason'][:11]:11} PnL=€{t['pnl']:+.3f}"
            )
    print()

    print("=== OORDEELREGEL ===")
    if len(aligned) < 20:
        print(f"ONVOLDOENDE PROSPECTIEF BEWIJS: ALIGNED n={len(aligned)} < 20. Niet als hard gate invoeren.")
    else:
        a = pf(aligned)
        allpf = pf(matched)
        apnl = sum(x["pnl"] for x in aligned)
        if a is not None and allpf is not None and a > allpf and apnl > 0:
            print("KANDIDAAT: alignment verbetert PF en blijft netto positief. Eerst prospectief/shadow bevestigen.")
        else:
            print("AFWIJZEN: alignment toont geen overtuigende verbetering.")

    print()
    print("=== VEILIGHEID ===")
    print("Orders/private API : NEE")
    print("Netwerkcalls        : NEE")
    print("Config/strategie    : ONGEWIJZIGD")
    print("LIVE                : ONGEWIJZIGD")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()
    return self_test() if args.self_test else run()


if __name__ == "__main__":
    raise SystemExit(main())
