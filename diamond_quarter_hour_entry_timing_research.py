#!/usr/bin/env python3
"""
Diamond Trader Quarter-Hour Entry Timing Research v1.0

Doel
----
Meet of de Market Scanner kansen te laat ziet doordat de 15m-scan op een
willekeurige minuut draait in plaats van kort na de sluiting van een 15m-candle.

We koppelen bestaande Market Scanner-signalen aan de 15-seconden Early Entry
Collector voor BTC/ETH/SOL/XRP/ADA. Voor elk signaal vergelijken we de echte
uitvoerbare bid/ask rond de candle-close met de bid/ask op detectietijd.

Positieve 'entry edge' betekent dat de eerdere timing een betere prijs gaf:
- LONG: eerdere ask was lager dan de ask op detectietijd;
- SHORT: eerdere bid was hoger dan de bid op detectietijd.

Daarnaast splitsen we bestaande CURRENT shadow-resultaten uit naar detectievertraging
en naar orderboek+trade-flow alignment bij candle-close.

Veiligheid
----------
- read-only;
- geen orders;
- geen private API;
- geen netwerkcalls;
- geen config- of LIVE-wijzigingen.
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
from typing import Any, Dict, Iterable, List, Optional, Tuple

VERSION = "1.0"
TIMEFRAME_SECONDS = 15 * 60
STAKE_EUR = 130.0
MAX_MATCH_SECONDS = 45.0

EARLY = Path("/var/data/diamond_early_entry/early_entry_samples_v1_3_1.csv")
SIGNALS = Path("/var/data/diamond_market_signals.csv")
TRADES = Path("/var/data/diamond_scanner_selective_shadow_trades.csv")

CORE = {"BTC/EUR", "ETH/EUR", "SOL/EUR", "XRP/EUR", "ADA/EUR"}
ROUTES = {
    ("LONG", "trend_breakout"): "LONG_TREND",
    ("LONG", "momentum"): "LONG_MOM",
    ("SHORT", "momentum"): "SHORT_MOM",
}
OFFSETS_MIN = (-5, -2, -1, 0, 1, 2, 5)


def f(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def b(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "ja", "on"}


def parse_time(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        n = float(text)
        if n > 1e12:
            return n / 1000.0
        if n > 1e9:
            return n
    except Exception:
        pass
    try:
        d = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).timestamp()
    except Exception:
        return None


def median(values: List[float]) -> float:
    return statistics.median(values) if values else 0.0


def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    data = sorted(values)
    if len(data) == 1:
        return data[0]
    pos = (len(data) - 1) * p
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return data[lo]
    w = pos - lo
    return data[lo] * (1 - w) + data[hi] * w


def pf(rows: Iterable[Dict[str, Any]]) -> Optional[float]:
    vals = [f(r.get("net_pnl_eur")) for r in rows]
    gp = sum(x for x in vals if x > 0)
    gl = abs(sum(x for x in vals if x < 0))
    if gl > 0:
        return gp / gl
    if gp > 0:
        return math.inf
    return None


def pf_text(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    if math.isinf(value):
        return "INF"
    return f"{value:.3f}"


def candidate_key(row: Dict[str, str]) -> str:
    return "|".join([
        str(row.get("symbol") or "").upper(),
        str(row.get("strategy") or ""),
        str(row.get("side") or "").upper(),
        str(row.get("candle_timestamp") or ""),
    ])


def load_early() -> Tuple[Dict[str, List[Tuple[float, float, float, float, float]]], float, float, int]:
    if not EARLY.is_file():
        raise FileNotFoundError(EARLY)
    data: Dict[str, List[Tuple[float, float, float, float, float]]] = defaultdict(list)
    first_ts = math.inf
    last_ts = 0.0
    count = 0
    with EARLY.open("r", encoding="utf-8-sig", newline="") as h:
        r = csv.DictReader(h)
        required = {"timestamp_utc", "symbol", "bid", "ask", "book_imbalance", "trade_imbalance_60s"}
        missing = required - set(r.fieldnames or [])
        if missing:
            raise RuntimeError("Early Entry CSV mist: " + ", ".join(sorted(missing)))
        for row in r:
            sym = str(row.get("symbol") or "").upper()
            if sym not in CORE:
                continue
            ts = parse_time(row.get("timestamp_utc"))
            bid = f(row.get("bid"))
            ask = f(row.get("ask"))
            if ts is None or bid <= 0 or ask <= 0:
                continue
            data[sym].append((ts, bid, ask, f(row.get("book_imbalance")), f(row.get("trade_imbalance_60s"))))
            first_ts = min(first_ts, ts)
            last_ts = max(last_ts, ts)
            count += 1
    for sym in data:
        data[sym].sort(key=lambda x: x[0])
    if not count:
        raise RuntimeError("Geen bruikbare Early Entry samples")
    return data, first_ts, last_ts, count


def nearest(samples: List[Tuple[float, float, float, float, float]], target: float) -> Optional[Tuple[float, float, float, float, float]]:
    if not samples:
        return None
    times = [x[0] for x in samples]
    i = bisect.bisect_left(times, target)
    choices = []
    if i < len(samples):
        choices.append(samples[i])
    if i > 0:
        choices.append(samples[i - 1])
    if not choices:
        return None
    best = min(choices, key=lambda x: abs(x[0] - target))
    return best if abs(best[0] - target) <= MAX_MATCH_SECONDS else None


def load_trades() -> Dict[str, Dict[str, str]]:
    result: Dict[str, Dict[str, str]] = {}
    if not TRADES.is_file():
        return result
    with TRADES.open("r", encoding="utf-8-sig", newline="") as h:
        r = csv.DictReader(h)
        for row in r:
            if str(row.get("variant") or "").upper() != "CURRENT":
                continue
            if not str(row.get("closed_at") or "").strip():
                continue
            key = str(row.get("candidate_key") or "")
            if key:
                result[key] = row
    return result


def load_signals(first_ts: float, last_ts: float, trade_map: Dict[str, Dict[str, str]]) -> List[Dict[str, Any]]:
    if not SIGNALS.is_file():
        raise FileNotFoundError(SIGNALS)
    result: Dict[str, Dict[str, Any]] = {}
    with SIGNALS.open("r", encoding="utf-8-sig", newline="") as h:
        r = csv.DictReader(h)
        required = {"detected_at", "candle_timestamp", "symbol", "strategy", "side", "shadow_eligible"}
        missing = required - set(r.fieldnames or [])
        if missing:
            raise RuntimeError("Signals CSV mist: " + ", ".join(sorted(missing)))
        for row in r:
            sym = str(row.get("symbol") or "").upper()
            side = str(row.get("side") or "").upper()
            strategy = str(row.get("strategy") or "")
            route = ROUTES.get((side, strategy))
            if sym not in CORE or route is None:
                continue
            detected = parse_time(row.get("detected_at"))
            candle = parse_time(row.get("candle_timestamp"))
            if detected is None or candle is None:
                continue
            if not (first_ts <= detected <= last_ts):
                continue
            key = candidate_key(row)
            item: Dict[str, Any] = dict(row)
            item["key"] = key
            item["route"] = route
            item["detected_ts"] = detected
            item["close_ts"] = candle + TIMEFRAME_SECONDS
            item["delay_min"] = (detected - item["close_ts"]) / 60.0
            item["trade"] = trade_map.get(key)
            result[key] = item
    return sorted(result.values(), key=lambda x: x["detected_ts"])


def edge_pct(side: str, earlier: Tuple[float, float, float, float, float], detected: Tuple[float, float, float, float, float]) -> float:
    if side == "LONG":
        return (detected[2] - earlier[2]) / detected[2] * 100.0
    return (earlier[1] - detected[1]) / detected[1] * 100.0


def aligned(side: str, sample: Tuple[float, float, float, float, float]) -> bool:
    book = sample[3]
    flow = sample[4]
    if side == "LONG":
        return book > 0 and flow > 0
    return book < 0 and flow < 0


def stats_trades(rows: List[Dict[str, Any]]) -> str:
    closed = [r for r in rows if r.get("trade")]
    vals = [f(r["trade"].get("net_pnl_eur")) for r in closed]
    wins = sum(x > 0 for x in vals)
    losses = sum(x < 0 for x in vals)
    p = pf([r["trade"] for r in closed])
    return f"n={len(closed):3d} W/L={wins}/{losses} PnL=€{sum(vals):+.3f} PF={pf_text(p)}"


def self_test() -> int:
    det = (100.0, 99.0, 101.0, 0.2, 0.3)
    early_long = (90.0, 98.0, 100.0, 0.1, 0.2)
    early_short = (90.0, 100.0, 101.0, -0.1, -0.2)
    assert edge_pct("LONG", early_long, det) > 0
    assert edge_pct("SHORT", early_short, det) > 0
    assert aligned("LONG", early_long)
    assert aligned("SHORT", early_short)
    print("DIAMOND_QUARTER_HOUR_ENTRY_TIMING_SELF_TEST_OK")
    return 0


def run() -> int:
    data, first_ts, last_ts, sample_count = load_early()
    trades = load_trades()
    signals = load_signals(first_ts, last_ts, trades)

    analyzed: List[Dict[str, Any]] = []
    for sig in signals:
        samples = data.get(str(sig.get("symbol") or "").upper(), [])
        det_sample = nearest(samples, sig["detected_ts"])
        close_sample = nearest(samples, sig["close_ts"])
        if det_sample is None:
            continue
        x = dict(sig)
        x["det_sample"] = det_sample
        x["close_sample"] = close_sample
        x["offset_samples"] = {
            off: nearest(samples, sig["close_ts"] + off * 60.0)
            for off in OFFSETS_MIN
        }
        analyzed.append(x)

    print("=" * 112)
    print(f" DIAMOND QUARTER-HOUR ENTRY TIMING RESEARCH v{VERSION}")
    print("=" * 112)
    print(f"Early Entry samples      : {sample_count}")
    print(f"Early Entry periode      : {datetime.fromtimestamp(first_ts, timezone.utc).isoformat()} -> {datetime.fromtimestamp(last_ts, timezone.utc).isoformat()}")
    print(f"Relevante signalen       : {len(signals)}")
    print(f"Met detectie-sample      : {len(analyzed)}")
    print(f"Met gesloten CURRENT     : {sum(1 for x in analyzed if x.get('trade'))}")
    print("Routes                   : LONG trend_breakout, LONG momentum, SHORT momentum")
    print("Positieve edge            : eerder uitvoerbare prijs beter dan prijs op scanner-detectie")
    print()

    delays = [x["delay_min"] for x in analyzed if -2 <= x["delay_min"] <= 30]
    print("=== SCANNER DETECTIEVERTRAGING T.O.V. 15m CANDLE-CLOSE ===")
    if delays:
        print(f"n={len(delays)} | avg={statistics.mean(delays):.2f} min | med={median(delays):.2f} min | p90={percentile(delays,0.90):.2f} min")
        buckets = [(-2,3,"<=3m"),(3,7,"3-7m"),(7,11,"7-11m"),(11,16,"11-16m"),(16,31,">16m")]
        for lo, hi, label in buckets:
            group = [x for x in analyzed if lo <= x["delay_min"] < hi]
            print(f"{label:7} signalen={len(group):3d} | gesloten: {stats_trades(group)}")
    else:
        print("GEEN bruikbare delays")
    print()

    print("=== ENTRY EDGE ROND CANDLE-CLOSE T.O.V. HUIDIGE DETECTIETIJD ===")
    for off in OFFSETS_MIN:
        edges: List[float] = []
        aligned_count = 0
        for x in analyzed:
            s = x["offset_samples"].get(off)
            if s is None:
                continue
            e = edge_pct(str(x.get("side") or "").upper(), s, x["det_sample"])
            edges.append(e)
            if aligned(str(x.get("side") or "").upper(), s):
                aligned_count += 1
        if not edges:
            print(f"close{off:+3d}m : GEEN")
            continue
        positive = sum(e > 0 for e in edges)
        eur = STAKE_EUR * statistics.mean(edges) / 100.0
        print(
            f"close{off:+3d}m n={len(edges):3d} | avg edge={statistics.mean(edges):+.4f}% "
            f"med={median(edges):+.4f}% | beter={100*positive/len(edges):5.1f}% | "
            f"€130 avg≈€{eur:+.3f} | micro-aligned={100*aligned_count/len(edges):5.1f}%"
        )
    print()

    print("=== PER ROUTE: CANDLE-CLOSE (0m) VS DETECTIE ===")
    for route in ("LONG_TREND", "LONG_MOM", "SHORT_MOM"):
        group = [x for x in analyzed if x.get("route") == route and x.get("close_sample") is not None]
        edges = [edge_pct(str(x.get("side") or "").upper(), x["close_sample"], x["det_sample"]) for x in group]
        if not edges:
            print(f"{route:12}: GEEN")
            continue
        positive = sum(e > 0 for e in edges)
        print(
            f"{route:12} n={len(group):3d} | avg edge={statistics.mean(edges):+.4f}% "
            f"med={median(edges):+.4f}% | beter={100*positive/len(edges):5.1f}% | gesloten: {stats_trades(group)}"
        )
    print()

    print("=== MICROSTRUCTUUR OP CANDLE-CLOSE ===")
    close_rows = [x for x in analyzed if x.get("close_sample") is not None]
    a = [x for x in close_rows if aligned(str(x.get("side") or "").upper(), x["close_sample"])]
    n = [x for x in close_rows if not aligned(str(x.get("side") or "").upper(), x["close_sample"])]
    print(f"ALIGNED     signalen={len(a):3d} | gesloten: {stats_trades(a)}")
    print(f"NOT_ALIGNED signalen={len(n):3d} | gesloten: {stats_trades(n)}")
    print()

    print("=== LAATSTE 10 GEMATCHTE GESLOTEN TRADES ===")
    closed = [x for x in analyzed if x.get("trade") and x.get("close_sample") is not None]
    for x in closed[-10:]:
        e = edge_pct(str(x.get("side") or "").upper(), x["close_sample"], x["det_sample"])
        t = x["trade"]
        print(
            f"{x.get('symbol'):8} {x.get('side'):5} {x.get('strategy'):16} "
            f"delay={x['delay_min']:5.1f}m edge0={e:+.3f}% "
            f"aligned={'JA' if aligned(str(x.get('side') or '').upper(), x['close_sample']) else 'NEE'} "
            f"{t.get('exit_reason')} PnL=€{f(t.get('net_pnl_eur')):+.3f}"
        )

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
