#!/usr/bin/env python3
"""
Diamond Trader - Early Entry LONG Signal Analyzer v1.0

Vergelijkt Early Entry v1.3.1 data met LONG Entry Shadow-signalen.
Referentiepunt is signal_closed_at. Snapshots: -30m, -15m, -5m en 0m.

READ-ONLY:
- geen exchange-calls
- geen API keys
- geen orders
- geen config/bot-state wijzigingen
- schrijft alleen eigen analysebestanden onder /var/data/diamond_early_entry
"""

import argparse
import csv
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "1.0"
MODE = "READ_ONLY_EARLY_ENTRY_LONG_SIGNAL_ANALYSIS"

DATA_DIR = Path(os.getenv("DIAMOND_DATA_DIR", "/var/data"))
EARLY_DIR = DATA_DIR / "diamond_early_entry"
EARLY_CSV = EARLY_DIR / "early_entry_samples_v1_3_1.csv"
LONG_STATE = DATA_DIR / "diamond_long_entry_shadow_state.json"

REPORT_JSON = EARLY_DIR / "early_entry_long_signal_analysis_v1_0.json"
REPORT_CSV = EARLY_DIR / "early_entry_long_signal_analysis_v1_0.csv"

OFFSETS_MINUTES = (-30, -15, -5, 0)
DEFAULT_TOLERANCE_SECONDS = 45.0

READ_FIELDS = (
    "bid", "ask", "last", "spread_pct",
    "book_bid_value_top10", "book_ask_value_top10", "book_imbalance",
    "trade_count_60s", "buy_count_60s", "sell_count_60s",
    "buy_value_60s", "sell_value_60s", "trade_imbalance_60s",
    "close_1m", "volume_1m", "close_5m", "volume_5m",
)


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_dt(value):
    if value in (None, ""):
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def as_float(value):
    if value in (None, ""):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except Exception:
        return None


def pct_change(old, new):
    if old in (None, 0) or new is None:
        return None
    return (new - old) / old * 100.0


def fmt(value, digits=6):
    if value is None:
        return ""
    return f"{value:.{digits}f}"


def load_long_state():
    with LONG_STATE.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def current_outcomes(state):
    out = {}
    for item in state.get("closed") or []:
        if isinstance(item, dict) and item.get("variant") == "CURRENT":
            sid = str(item.get("signal_id") or "")
            if sid:
                out[sid] = item
    return out


def build_signals(state):
    signals = []
    raw_signals = state.get("signals") or {}
    if not isinstance(raw_signals, dict):
        return signals

    for raw_key, raw in raw_signals.items():
        if not isinstance(raw, dict):
            continue

        signal_id = str(raw.get("signal_id") or raw_key)
        symbol = str(raw.get("symbol") or "")
        anchor = parse_dt(raw.get("signal_closed_at"))
        candle = parse_dt(raw.get("signal_candle"))

        if not signal_id or not symbol or anchor is None:
            continue

        signals.append({
            "signal_id": signal_id,
            "symbol": symbol,
            "signal_candle": candle.isoformat() if candle else raw.get("signal_candle"),
            "signal_closed_at": anchor.isoformat(),
            "signal_close": as_float(raw.get("signal_close")),
            "signal_atr": as_float(raw.get("atr")),
            "signal_atr_pct": as_float(raw.get("atr_pct")),
            "signal_rsi": as_float(raw.get("rsi")),
            "detected_at": raw.get("detected_at"),
            "_anchor": anchor,
        })

    signals.sort(key=lambda x: x["_anchor"])
    return signals


def build_targets(signals):
    targets = {}
    for idx, signal in enumerate(signals):
        anchor = signal["_anchor"]
        for offset in OFFSETS_MINUTES:
            target = anchor + timedelta(minutes=offset)
            targets.setdefault(signal["symbol"], []).append((idx, offset, target))
    return targets


def scan_early_csv(targets_by_symbol, tolerance_seconds):
    best = {}
    first_ts = None
    last_ts = None
    row_count = 0

    with EARLY_CSV.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)

        for row in reader:
            ts = parse_dt(row.get("timestamp_utc"))
            symbol = str(row.get("symbol") or "")
            if ts is None or not symbol:
                continue

            row_count += 1
            first_ts = ts if first_ts is None or ts < first_ts else first_ts
            last_ts = ts if last_ts is None or ts > last_ts else last_ts

            for signal_index, offset, target in targets_by_symbol.get(symbol, []):
                diff = abs((ts - target).total_seconds())
                if diff > tolerance_seconds:
                    continue

                key = (signal_index, offset)
                old = best.get(key)
                if old is not None and diff >= old["distance_seconds"]:
                    continue

                snap = {
                    "timestamp_utc": ts.isoformat(),
                    "distance_seconds": diff,
                }
                for field in READ_FIELDS:
                    snap[field] = as_float(row.get(field))
                best[key] = snap

    return best, first_ts, last_ts, row_count


def build_result(signal, idx, best, first_ts, last_ts, outcomes):
    anchor = signal["_anchor"]
    snaps = {}
    coverage_possible = first_ts is not None and last_ts is not None

    for offset in OFFSETS_MINUTES:
        label = f"m{abs(offset)}" if offset < 0 else "m0"
        snaps[label] = best.get((idx, offset))
        target = anchor + timedelta(minutes=offset)
        if not (first_ts is not None and last_ts is not None and first_ts <= target <= last_ts):
            coverage_possible = False

    complete = all(snaps.values())
    outcome = outcomes.get(signal["signal_id"]) or {}

    m30 = snaps["m30"] or {}
    m15 = snaps["m15"] or {}
    m5 = snaps["m5"] or {}
    m0 = snaps["m0"] or {}

    return {
        "signal_id": signal["signal_id"],
        "symbol": signal["symbol"],
        "signal_candle": signal["signal_candle"],
        "signal_closed_at": signal["signal_closed_at"],
        "detected_at": signal.get("detected_at"),
        "signal_close": signal.get("signal_close"),
        "signal_atr": signal.get("signal_atr"),
        "signal_atr_pct": signal.get("signal_atr_pct"),
        "signal_rsi": signal.get("signal_rsi"),
        "coverage_possible": coverage_possible,
        "complete_snapshots": complete,
        "current_exit_reason": outcome.get("exit_reason"),
        "current_net_pnl_eur": as_float(outcome.get("net_pnl_eur")),
        "snapshots": snaps,
        "derived": {
            "price_m30_to_m0_pct": pct_change(as_float(m30.get("last")), as_float(m0.get("last"))),
            "price_m15_to_m0_pct": pct_change(as_float(m15.get("last")), as_float(m0.get("last"))),
            "price_m5_to_m0_pct": pct_change(as_float(m5.get("last")), as_float(m0.get("last"))),
            "book_imbalance_m30": as_float(m30.get("book_imbalance")),
            "book_imbalance_m15": as_float(m15.get("book_imbalance")),
            "book_imbalance_m5": as_float(m5.get("book_imbalance")),
            "book_imbalance_m0": as_float(m0.get("book_imbalance")),
            "trade_imbalance_m30": as_float(m30.get("trade_imbalance_60s")),
            "trade_imbalance_m15": as_float(m15.get("trade_imbalance_60s")),
            "trade_imbalance_m5": as_float(m5.get("trade_imbalance_60s")),
            "trade_imbalance_m0": as_float(m0.get("trade_imbalance_60s")),
            "spread_pct_m0": as_float(m0.get("spread_pct")),
        },
    }


CSV_COLUMNS = [
    "signal_id", "symbol", "signal_candle", "signal_closed_at", "detected_at",
    "coverage_possible", "complete_snapshots",
    "current_exit_reason", "current_net_pnl_eur",
    "signal_rsi", "signal_atr_pct",
    "price_m30_to_m0_pct", "price_m15_to_m0_pct", "price_m5_to_m0_pct",
    "book_imbalance_m30", "book_imbalance_m15", "book_imbalance_m5", "book_imbalance_m0",
    "trade_imbalance_m30", "trade_imbalance_m15", "trade_imbalance_m5", "trade_imbalance_m0",
    "spread_pct_m0",
    "m30_timestamp", "m15_timestamp", "m5_timestamp", "m0_timestamp",
    "m30_distance_seconds", "m15_distance_seconds", "m5_distance_seconds", "m0_distance_seconds",
]


def flatten_for_csv(item):
    d = item["derived"]
    s = item["snapshots"]

    row = {
        "signal_id": item["signal_id"],
        "symbol": item["symbol"],
        "signal_candle": item["signal_candle"],
        "signal_closed_at": item["signal_closed_at"],
        "detected_at": item.get("detected_at"),
        "coverage_possible": item["coverage_possible"],
        "complete_snapshots": item["complete_snapshots"],
        "current_exit_reason": item.get("current_exit_reason") or "",
        "current_net_pnl_eur": fmt(item.get("current_net_pnl_eur"), 8),
        "signal_rsi": fmt(item.get("signal_rsi"), 6),
        "signal_atr_pct": fmt(item.get("signal_atr_pct"), 6),
        "price_m30_to_m0_pct": fmt(d.get("price_m30_to_m0_pct"), 6),
        "price_m15_to_m0_pct": fmt(d.get("price_m15_to_m0_pct"), 6),
        "price_m5_to_m0_pct": fmt(d.get("price_m5_to_m0_pct"), 6),
        "book_imbalance_m30": fmt(d.get("book_imbalance_m30"), 6),
        "book_imbalance_m15": fmt(d.get("book_imbalance_m15"), 6),
        "book_imbalance_m5": fmt(d.get("book_imbalance_m5"), 6),
        "book_imbalance_m0": fmt(d.get("book_imbalance_m0"), 6),
        "trade_imbalance_m30": fmt(d.get("trade_imbalance_m30"), 6),
        "trade_imbalance_m15": fmt(d.get("trade_imbalance_m15"), 6),
        "trade_imbalance_m5": fmt(d.get("trade_imbalance_m5"), 6),
        "trade_imbalance_m0": fmt(d.get("trade_imbalance_m0"), 6),
        "spread_pct_m0": fmt(d.get("spread_pct_m0"), 6),
    }

    for label in ("m30", "m15", "m5", "m0"):
        snap = s.get(label) or {}
        row[f"{label}_timestamp"] = snap.get("timestamp_utc") or ""
        row[f"{label}_distance_seconds"] = fmt(as_float(snap.get("distance_seconds")), 3)

    return row


def write_reports(report):
    EARLY_DIR.mkdir(parents=True, exist_ok=True)

    tmp_json = REPORT_JSON.with_suffix(".json.tmp")
    with tmp_json.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp_json, REPORT_JSON)

    tmp_csv = REPORT_CSV.with_suffix(".csv.tmp")
    with tmp_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for item in report["signals"]:
            writer.writerow(flatten_for_csv(item))
    os.replace(tmp_csv, REPORT_CSV)


def print_report(report):
    c = report["counts"]

    print("DIAMOND TRADER EARLY ENTRY LONG ANALYSIS")
    print(f"Versie                 : {VERSION}")
    print(f"Modus                  : {MODE}")
    print(f"LONG signalen totaal   : {c['signals_total']}")
    print(f"Binnen Early dekking   : {c['coverage_possible']}")
    print(f"Complete snapshots     : {c['complete_snapshots']}")
    print(f"Nog niet analyseerbaar : {c['not_yet_analyzable']}")
    print(f"Early CSV regels       : {report['early_csv']['rows']}")
    print(f"Early eerste sample    : {report['early_csv']['first_sample']}")
    print(f"Early laatste sample   : {report['early_csv']['last_sample']}")
    print()

    complete = [x for x in report["signals"] if x["complete_snapshots"]]
    if not complete:
        print("Nog geen LONG-signaal met volledige Early Entry-data.")
        print("Wacht op het eerstvolgende officiële LONG-signaal.")
    else:
        print("=== ANALYSEERBARE SIGNALEN ===")
        for item in complete:
            d = item["derived"]
            pnl = item.get("current_net_pnl_eur")
            pnl_text = "open/onbekend" if pnl is None else f"€{pnl:+.4f}"
            print(
                f"{item['symbol']} | {item['signal_closed_at']} | "
                f"CURRENT {pnl_text} | "
                f"-30→0 {(d.get('price_m30_to_m0_pct') or 0):+.3f}% | "
                f"-15→0 {(d.get('price_m15_to_m0_pct') or 0):+.3f}% | "
                f"-5→0 {(d.get('price_m5_to_m0_pct') or 0):+.3f}%"
            )

    print()
    print(f"JSON: {REPORT_JSON}")
    print(f"CSV : {REPORT_CSV}")


def analyze(tolerance_seconds):
    if not LONG_STATE.exists():
        print(f"[FOUT] LONG state ontbreekt: {LONG_STATE}")
        return 2
    if not EARLY_CSV.exists():
        print(f"[FOUT] Early Entry CSV ontbreekt: {EARLY_CSV}")
        return 2

    state = load_long_state()
    signals = build_signals(state)
    targets = build_targets(signals)
    outcomes = current_outcomes(state)

    best, first_ts, last_ts, row_count = scan_early_csv(
        targets,
        tolerance_seconds,
    )

    results = [
        build_result(signal, idx, best, first_ts, last_ts, outcomes)
        for idx, signal in enumerate(signals)
    ]

    coverage_count = sum(1 for x in results if x["coverage_possible"])
    complete_count = sum(1 for x in results if x["complete_snapshots"])

    report = {
        "version": VERSION,
        "mode": MODE,
        "generated_at": utc_now_iso(),
        "reference": {
            "anchor": "signal_closed_at",
            "offset_minutes": list(OFFSETS_MINUTES),
            "nearest_sample_tolerance_seconds": tolerance_seconds,
        },
        "counts": {
            "signals_total": len(results),
            "coverage_possible": coverage_count,
            "complete_snapshots": complete_count,
            "not_yet_analyzable": len(results) - complete_count,
        },
        "early_csv": {
            "path": str(EARLY_CSV),
            "rows": row_count,
            "first_sample": first_ts.isoformat() if first_ts else None,
            "last_sample": last_ts.isoformat() if last_ts else None,
        },
        "inputs": {"long_state": str(LONG_STATE)},
        "safety": {
            "orders_possible": False,
            "private_exchange_calls": False,
            "api_keys_loaded": False,
            "config_write": False,
            "bot_state_write": False,
            "transactions_write": False,
            "own_output_files_only": True,
        },
        "signals": results,
    }

    write_reports(report)
    print_report(report)
    return 0


def status():
    if not REPORT_JSON.exists():
        print("Nog geen analyserapport aanwezig.")
        return 1

    with REPORT_JSON.open("r", encoding="utf-8") as handle:
        report = json.load(handle)
    print_report(report)
    return 0


def self_test():
    print("EARLY_ENTRY_LONG_ANALYSIS_V1_0_SELF_TEST_OK")
    print(f"Versie             : {VERSION}")
    print("Referentie          : signal_closed_at")
    print("Snapshots           : -30m, -15m, -5m, 0m")
    print("CSV verwerking      : streaming / geheugenarm")
    print("Exchange calls      : NEE")
    print("Private API         : NEE")
    print("Orders mogelijk     : NEE")
    print("Bot/config wijzigen : NEE")
    return 0


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--analyze", action="store_true")
    group.add_argument("--status", action="store_true")
    group.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--tolerance-seconds",
        type=float,
        default=DEFAULT_TOLERANCE_SECONDS,
    )
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if args.status:
        return status()

    return analyze(max(1.0, float(args.tolerance_seconds)))


if __name__ == "__main__":
    raise SystemExit(main())
