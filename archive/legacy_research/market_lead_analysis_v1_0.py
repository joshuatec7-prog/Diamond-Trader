#!/usr/bin/env python3
# Diamond Trader - Market Lead Analysis v1.0
# Read-only analyse van market_lead_samples_v1_0.csv.
# Positieve lag = Coinbase-beweging verschijnt eerder dan Bitvavo.

import csv
import json
import math
import statistics
from datetime import datetime
from pathlib import Path

DATA_DIR = Path("/var/data/diamond_market_lead")
CSV_FILE = DATA_DIR / "market_lead_samples_v1_0.csv"
REPORT_FILE = DATA_DIR / "market_lead_analysis_v1_0.json"

SYMBOLS = ("BTC-EUR", "ETH-EUR")
LAGS = (-300, -60, -30, -15, -10, -5, 0, 5, 10, 15, 30, 60, 300)
HORIZONS = (30, 60, 300)
THRESHOLD_PCT = 0.05


def num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def pct(a, b):
    return 0.0 if not a else (b - a) / a * 100.0


def corr(a, b):
    n = min(len(a), len(b))
    if n < 10:
        return None
    a, b = a[:n], b[:n]
    ma = sum(a) / n
    mb = sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 0 or vb <= 0:
        return None
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return cov / math.sqrt(va * vb)


def percentile(values, p):
    if not values:
        return None
    xs = sorted(values)
    k = (len(xs) - 1) * p
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return xs[lo]
    return xs[lo] * (hi - k) + xs[hi] * (k - lo)


def load():
    grouped = {s: [] for s in SYMBOLS}
    with CSV_FILE.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            s = r.get("symbol")
            if s not in grouped:
                continue
            try:
                ts = datetime.fromisoformat(r["timestamp_utc"].replace("Z", "+00:00"))
            except Exception:
                continue
            cb = num(r.get("coinbase_price"))
            bv = num(r.get("bitvavo_price"))
            if cb is None or bv is None:
                continue
            grouped[s].append({
                "ts": ts,
                "cb": cb,
                "bv": bv,
                "age": num(r.get("coinbase_age_ms")),
                "fetch": num(r.get("bitvavo_fetch_ms")),
                "diff": num(r.get("price_diff_pct")),
            })
    for s in grouped:
        grouped[s].sort(key=lambda x: x["ts"])
    return grouped


def shifted_corr(a, b, steps):
    if steps > 0:
        return corr(a[:-steps], b[steps:]) if len(a) > steps else None
    if steps < 0:
        k = abs(steps)
        return corr(a[k:], b[:-k]) if len(a) > k else None
    return corr(a, b)


def analyze_symbol(rows):
    gaps = [(b["ts"] - a["ts"]).total_seconds() for a, b in zip(rows, rows[1:])]
    normal_gaps = [g for g in gaps if 0 < g < 20]
    step = statistics.median(normal_gaps) if normal_gaps else 5.0

    cb_ret = [pct(a["cb"], b["cb"]) for a, b in zip(rows, rows[1:])]
    bv_ret = [pct(a["bv"], b["bv"]) for a, b in zip(rows, rows[1:])]

    lag_rows = []
    for lag in LAGS:
        steps = int(round(lag / step))
        c = shifted_corr(cb_ret, bv_ret, steps)
        lag_rows.append({"lag_seconds": lag, "corr": c})

    valid = [x for x in lag_rows if x["corr"] is not None]
    best = max(valid, key=lambda x: x["corr"]) if valid else None

    events = {}
    for horizon in HORIZONS:
        k = max(1, int(round(horizon / step)))
        cb_pairs = []
        bv_pairs = []

        for i in range(k, len(rows) - k):
            cb_past = pct(rows[i-k]["cb"], rows[i]["cb"])
            if abs(cb_past) >= THRESHOLD_PCT:
                cb_pairs.append((cb_past, pct(rows[i]["bv"], rows[i+k]["bv"])))

            bv_past = pct(rows[i-k]["bv"], rows[i]["bv"])
            if abs(bv_past) >= THRESHOLD_PCT:
                bv_pairs.append((bv_past, pct(rows[i]["cb"], rows[i+k]["cb"])))

        def summarize(pairs):
            if not pairs:
                return {"events": 0, "same_direction_pct": None}
            same = sum(
                1 for lead, follow in pairs
                if (lead > 0 and follow > 0) or (lead < 0 and follow < 0)
            )
            return {
                "events": len(pairs),
                "same_direction_pct": same / len(pairs) * 100.0,
            }

        events[str(horizon)] = {
            "coinbase_to_bitvavo": summarize(cb_pairs),
            "bitvavo_to_coinbase": summarize(bv_pairs),
        }

    ages = [x["age"] for x in rows if x["age"] is not None]
    fetches = [x["fetch"] for x in rows if x["fetch"] is not None]
    diffs = [abs(x["diff"]) for x in rows if x["diff"] is not None]

    quality = {
        "rows": len(rows),
        "median_interval_s": statistics.median(gaps) if gaps else None,
        "max_gap_s": max(gaps) if gaps else None,
        "coinbase_age_ms_median": statistics.median(ages) if ages else None,
        "coinbase_age_ms_p95": percentile(ages, 0.95),
        "bitvavo_fetch_ms_median": statistics.median(fetches) if fetches else None,
        "bitvavo_fetch_ms_p95": percentile(fetches, 0.95),
        "price_diff_abs_pct_median": statistics.median(diffs) if diffs else None,
        "price_diff_abs_pct_p95": percentile(diffs, 0.95),
    }

    return {
        "quality": quality,
        "lag": {"best": best, "all": lag_rows},
        "events": events,
    }


def main():
    grouped = load()
    report = {
        "version": "1.0",
        "mode": "READ_ONLY_MARKET_LEAD_ANALYSIS",
        "symbols": {},
        "safety": {
            "orders_possible": False,
            "private_api": False,
            "api_keys_used": False,
            "bot_config_modified": False,
        },
    }

    print("=== MARKET LEAD ANALYSIS v1.0 ===")
    for symbol in SYMBOLS:
        rows = grouped[symbol]
        result = analyze_symbol(rows)
        report["symbols"][symbol] = result

        q = result["quality"]
        best = result["lag"]["best"]
        print()
        print(symbol)
        print(f"regels       : {q['rows']}")
        print(f"interval     : {q['median_interval_s']:.2f}s")
        print(f"max gap      : {q['max_gap_s']:.1f}s")
        print(
            f"CB age med/p95: {q['coinbase_age_ms_median']:.0f}/"
            f"{q['coinbase_age_ms_p95']:.0f} ms"
        )
        print(
            f"BV fetch med/p95: {q['bitvavo_fetch_ms_median']:.0f}/"
            f"{q['bitvavo_fetch_ms_p95']:.0f} ms"
        )
        if best:
            print(
                f"beste lag    : {best['lag_seconds']:+d}s "
                f"(corr {best['corr']:+.4f})"
            )

        for h in HORIZONS:
            e = result["events"][str(h)]
            cb = e["coinbase_to_bitvavo"]
            bv = e["bitvavo_to_coinbase"]
            cbp = cb["same_direction_pct"]
            bvp = bv["same_direction_pct"]
            print(
                f"{h:>3}s CB→BV: {cb['events']:4d} events | "
                f"richting {0 if cbp is None else cbp:5.1f}% | "
                f"BV→CB: {bv['events']:4d} | "
                f"richting {0 if bvp is None else bvp:5.1f}%"
            )

    REPORT_FILE.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print()
    print("Orders/private API: NEE")
    print("Rapport:", REPORT_FILE)


if __name__ == "__main__":
    main()
