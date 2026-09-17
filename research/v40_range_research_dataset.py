#!/usr/bin/env python3
"""
v4.0 dedicated sideways/range research dataset.

Research only. Independent of the existing LONG and BEAR candidate generators.
Scans all loaded EUR markets on CLOSED 5m candles and records broad range-edge
attention candidates in both directions:
- lower edge -> potential RANGE_LONG
- upper edge -> potential RANGE_SHORT

Future data is used only for explicit labels. No PAPER/live/config/deploy changes.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bitvavo_public import BitvavoPublic
from v40_human_engine import ROUNDTRIP_FIXED_COST_PCT
from v40_master_research_dataset import (
    ASSUMED_SPREAD_PCT,
    FIVE_MINUTE_MS,
    LABEL_HORIZONS_MINUTES,
    LABEL_TAIL_MINUTES,
    accumulate_early_market_context,
    finalize_early_market_context,
)
from v40_replay import DAY_MS

MAX_CANDIDATES_PER_MOMENT = 12
ROUNDTRIP_COST_PCT = ROUNDTRIP_FIXED_COST_PCT + ASSUMED_SPREAD_PCT
MIN_RANGE_WIDTH_4H_PCT = 1.50
MIN_EDGE_STRETCH = 1.25


def pct(new: float, old: float) -> float:
    return (new / old - 1.0) * 100.0 if new > 0.0 and old > 0.0 else 0.0


def point_in_time_features(rows: Sequence[Any], index: int) -> dict[str, float | str] | None:
    if index < 48:
        return None
    latest = float(rows[index].close)
    if latest <= 0.0:
        return None
    prior = [float(rows[index - n].close) for n in (1, 2, 3, 6, 12, 48)]
    if min(prior) <= 0.0:
        return None
    r5, r10, r15, r30, r60, r4h = [pct(latest, value) for value in prior]

    window = rows[index - 47:index + 1]
    closes = [float(c.close) for c in window]
    highs = [float(c.high) for c in window]
    lows = [float(c.low) for c in window]
    mid = mean(closes)
    sigma = pstdev(closes)
    high4h = max(highs)
    low4h = min(lows)
    if mid <= 0.0 or sigma <= 0.0 or high4h <= low4h:
        return None
    z4h = (latest - mid) / sigma
    width_pct = (high4h / low4h - 1.0) * 100.0
    pos4h = (latest - low4h) / (high4h - low4h)

    win2 = closes[-24:]
    mid2 = mean(win2)
    sig2 = pstdev(win2)
    z2h = (latest - mid2) / sig2 if sig2 > 0.0 else 0.0

    recent_vol = mean(float(c.volume) for c in rows[index - 1:index + 1])
    prior_vol = mean(float(c.volume) for c in rows[index - 7:index - 1])
    volume_accel = recent_vol / prior_vol if prior_vol > 0.0 else 0.0

    lower_stretch = max(-z4h, (0.20 - pos4h) * 5.0)
    upper_stretch = max(z4h, (pos4h - 0.80) * 5.0)
    if lower_stretch >= upper_stretch:
        side = "RANGE_LONG"
        stretch = lower_stretch
    else:
        side = "RANGE_SHORT"
        stretch = upper_stretch

    return {
        "side": side,
        "return_5m_pct": round(r5, 8),
        "return_10m_pct": round(r10, 8),
        "return_15m_pct": round(r15, 8),
        "return_30m_pct": round(r30, 8),
        "return_1h_pct": round(r60, 8),
        "return_4h_pct": round(r4h, 8),
        "zscore_2h": round(z2h, 8),
        "zscore_4h": round(z4h, 8),
        "range_position_4h": round(pos4h, 8),
        "range_width_4h_pct": round(width_pct, 8),
        "distance_to_mid_4h_pct": round(pct(latest, mid), 8),
        "stretch_score": round(float(stretch), 8),
        "volume_accel_ratio": round(volume_accel, 8),
        "trend_to_range_ratio": round(abs(r4h) / width_pct, 8) if width_pct > 0.0 else 999.0,
    }


def directional_labels(rows: Sequence[Any], timestamps: Sequence[int], index: int, side: str) -> dict[str, dict[str, float | int | None]]:
    signal_ms = int(rows[index].timestamp_ms)
    entry = float(rows[index].close)
    result: dict[str, dict[str, float | int | None]] = {}
    for horizon in LABEL_HORIZONS_MINUTES:
        target_ms = signal_ms + int(horizon) * 60_000
        if not timestamps or timestamps[-1] < target_ms:
            result[str(horizon)] = {"mature": 0, "gross_range_pct": None, "net_range_pct": None}
            continue
        j = bisect.bisect_right(timestamps, target_ms) - 1
        if j < index:
            result[str(horizon)] = {"mature": 0, "gross_range_pct": None, "net_range_pct": None}
            continue
        exit_price = float(rows[j].close)
        gross_long = pct(exit_price, entry)
        gross = gross_long if side == "RANGE_LONG" else -gross_long
        result[str(horizon)] = {
            "mature": 1,
            "gross_range_pct": round(gross, 6),
            "net_range_pct": round(gross - ROUNDTRIP_COST_PCT, 6),
        }
    return result


def btc_context_map(rows: Sequence[Any], signal_start_ms: int, signal_end_ms: int) -> dict[int, dict[str, float]]:
    ordered = sorted((c for c in rows if c.is_valid), key=lambda c: c.timestamp_ms)
    closes = {int(c.timestamp_ms): float(c.close) for c in ordered}
    out: dict[int, dict[str, float]] = {}
    for candle in ordered:
        ts = int(candle.timestamp_ms)
        if ts < signal_start_ms or ts >= signal_end_ms:
            continue
        refs = [closes.get(ts - n * FIVE_MINUTE_MS) for n in (1, 2, 3, 12)]
        if any(value is None for value in refs):
            continue
        close = float(candle.close)
        out[ts] = {
            "btc_return_5m_pct": round(pct(close, float(refs[0])), 8),
            "btc_return_10m_pct": round(pct(close, float(refs[1])), 8),
            "btc_return_15m_pct": round(pct(close, float(refs[2])), 8),
            "btc_return_1h_pct": round(pct(close, float(refs[3])), 8),
        }
    return out


def keep_bounded(bucket: list[dict[str, Any]], candidate: dict[str, Any]) -> None:
    bucket.append(candidate)
    if len(bucket) > MAX_CANDIDATES_PER_MOMENT * 2:
        bucket.sort(key=lambda item: (float(item["entry_features"]["stretch_score"]), str(item["market"])), reverse=True)
        del bucket[MAX_CANDIDATES_PER_MOMENT:]


def attach_cross_section(rows: list[dict[str, Any]]) -> None:
    by_time: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_time[int(row["signal_ms"])].append(row)
    for group in by_time.values():
        for row in group:
            own = float(row["entry_features"]["stretch_score"])
            width = float(row["entry_features"]["range_width_4h_pct"])
            trend = float(row["entry_features"]["trend_to_range_ratio"])
            row["cross_section"] = {
                "simultaneous_range_candidate_count": len(group),
                "rank_stretch_score": 1 + sum(float(other["entry_features"]["stretch_score"]) > own for other in group),
                "rank_range_width": 1 + sum(float(other["entry_features"]["range_width_4h_pct"]) > width for other in group),
                "rank_low_trendiness": 1 + sum(float(other["entry_features"]["trend_to_range_ratio"]) < trend for other in group),
            }


def run_export(api: BitvavoPublic, *, days: int = 90, now_ms: int | None = None, markets: Sequence[str] | None = None, output_path: str | None = None) -> dict[str, Any]:
    if days != 90:
        raise ValueError("range research dataset vereist exact 90 signaaldagen")
    fetch_end = int(time.time() * 1000) if now_ms is None else int(now_ms)
    fetch_end = fetch_end // FIVE_MINUTE_MS * FIVE_MINUTE_MS
    signal_end = fetch_end - LABEL_TAIL_MINUTES * 60_000
    signal_start = signal_end - days * DAY_MS
    fetch_start = signal_start - DAY_MS

    active = sorted(set(markets or api.trading_markets("EUR")))
    if "BTC-EUR" not in active:
        active.append("BTC-EUR")
        active.sort()

    btc = api.closed_candles_between("BTC-EUR", "5m", fetch_start, fetch_end, now_ms=fetch_end)
    if len(btc) < 288:
        raise RuntimeError("onvoldoende BTC-historie voor range research dataset")
    btc_map = btc_context_map(btc, signal_start, signal_end)

    context_aggregates: dict[int, dict[str, float]] = {}
    candidates_by_time: dict[int, list[dict[str, Any]]] = defaultdict(list)
    errors: list[str] = []
    markets_completed = 0
    trigger_rows_seen = 0
    incomplete_labels = 0

    for market in active:
        try:
            candles = btc if market == "BTC-EUR" else api.closed_candles_between(market, "5m", fetch_start, fetch_end, now_ms=fetch_end)
            rows = sorted((c for c in candles if c.is_valid), key=lambda c: c.timestamp_ms)
            accumulate_early_market_context(rows, signal_start, signal_end, context_aggregates)
            timestamps = [int(c.timestamp_ms) for c in rows]
            for i in range(48, len(rows)):
                ts = int(rows[i].timestamp_ms)
                if ts < signal_start or ts >= signal_end:
                    continue
                features = point_in_time_features(rows, i)
                if not features:
                    continue
                if float(features["range_width_4h_pct"]) < MIN_RANGE_WIDTH_4H_PCT:
                    continue
                if float(features["stretch_score"]) < MIN_EDGE_STRETCH:
                    continue
                trigger_rows_seen += 1
                labels = directional_labels(rows, timestamps, i, str(features["side"]))
                if not all(int(item["mature"]) == 1 for item in labels.values()):
                    incomplete_labels += 1
                    continue
                candidate = {
                    "market": market,
                    "signal_ms": ts,
                    "entry_reference": float(rows[i].close),
                    "side": str(features["side"]),
                    "entry_features": features,
                    "forward_range_labels": labels,
                    "market_context": {},
                    "cross_section": {},
                }
                keep_bounded(candidates_by_time[ts], candidate)
            markets_completed += 1
        except Exception as exc:
            errors.append(f"{market}: {type(exc).__name__}: {exc}")

    context = finalize_early_market_context(context_aggregates)
    candidates: list[dict[str, Any]] = []
    for ts, bucket in candidates_by_time.items():
        bucket.sort(key=lambda item: (float(item["entry_features"]["stretch_score"]), str(item["market"])), reverse=True)
        for candidate in bucket[:MAX_CANDIDATES_PER_MOMENT]:
            mc = context.get(int(ts))
            btc_ctx = btc_map.get(int(ts))
            if mc is None or btc_ctx is None:
                continue
            candidate["market_context"] = {**mc, **btc_ctx}
            candidates.append(candidate)

    attach_cross_section(candidates)
    candidates.sort(key=lambda item: (int(item["signal_ms"]), str(item["market"])))

    report = {
        "version": "v40-range-research-dataset-1",
        "component": "V40_RANGE_RESEARCH_DATASET",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "OFFLINE_MEASUREMENT_ONLY",
        "execution_enabled": False,
        "live_orders_possible": False,
        "active_paper_changed": False,
        "future_data_used_for_features": False,
        "future_data_used_for_labels": True,
        "sampling_independent_of_long_candidate_generator": True,
        "sampling_independent_of_bear_candidate_generator": True,
        "period": {
            "signal_days": days,
            "signal_start_ms": signal_start,
            "signal_end_ms_exclusive": signal_end,
            "fetch_start_ms": fetch_start,
            "fetch_end_ms": fetch_end,
            "warmup_days": 1,
            "label_tail_minutes": LABEL_TAIL_MINUTES,
            "candle_interval": "5m",
            "split_intent": "60D_TRAINING_15D_VALIDATION_15D_UNTOUCHED_TEST",
            "purge_required_at_split_boundaries": True,
        },
        "cost_model": {"roundtrip_cost_pct": ROUNDTRIP_COST_PCT, "assumed_spread_pct": ASSUMED_SPREAD_PCT},
        "attention_funnel": {
            "trigger": f"4h range width >= {MIN_RANGE_WIDTH_4H_PCT}% and edge stretch >= {MIN_EDGE_STRETCH}",
            "directions": ["RANGE_LONG", "RANGE_SHORT"],
            "max_candidates_per_moment": MAX_CANDIDATES_PER_MOMENT,
            "purpose": "broad point-in-time range-edge attention set, not a trade rule",
        },
        "markets_requested": len(active),
        "markets_completed": markets_completed,
        "trigger_rows_seen": trigger_rows_seen,
        "incomplete_labels": incomplete_labels,
        "candidates": len(candidates),
        "moments": len({int(row["signal_ms"]) for row in candidates}),
        "errors": errors,
        "rows": candidates,
        "notes": [
            "Geen LONG- of BEAR-signaalgenerator gebruikt voor sampling.",
            "Alle features gebruiken alleen gesloten candles.",
            "Toekomstdata staat uitsluitend in forward_range_labels.",
            "Range meet beide kanten: onderste band long, bovenste band short.",
            "Laatste 15 dagen worden door de exporter niet beoordeeld.",
            "Measurement-only; geen PAPER/live/config/deploy wijziging.",
        ],
    }
    if output_path:
        Path(output_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="v4.0 dedicated sideways/range research dataset")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--now-ms", type=int, default=None)
    parser.add_argument("--markets", default="")
    parser.add_argument("--output", default="cryptobot_v40_range_research_dataset.json")
    args = parser.parse_args()
    api = BitvavoPublic("https://api.bitvavo.com/v2", timeout_seconds=20, retries=4)
    markets = [item.strip().upper() for item in args.markets.split(",") if item.strip()] or None
    report = run_export(api, days=args.days, now_ms=args.now_ms, markets=markets, output_path=args.output)
    print("=== v4.0 SIDEWAYS/RANGE RESEARCH DATASET ===")
    print("UITVOERING : UIT / OFFLINE METING")
    print(f"MARKTEN    : {report['markets_completed']}/{report['markets_requested']}")
    print(f"TRIGGERS   : {report['trigger_rows_seen']}")
    print(f"KANDIDATEN : {report['candidates']}")
    print(f"MOMENTEN   : {report['moments']}")
    print(f"FOUTEN     : {len(report['errors'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
