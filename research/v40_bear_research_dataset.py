#!/usr/bin/env python3
"""
v4.0 dedicated bear/short research dataset.

Research only. This exporter is deliberately independent of the existing LONG
candidate generator. It scans every successfully loaded EUR market on closed
5m candles, creates a broad bearish attention funnel from point-in-time data,
and stores forward SHORT outcomes after the same fixed roundtrip cost model.

It does not place orders, change PAPER, deploy Render, or alter runtime config.
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
from statistics import mean
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

MAX_CANDIDATES_PER_MOMENT = 20
ROUNDTRIP_COST_PCT = ROUNDTRIP_FIXED_COST_PCT + ASSUMED_SPREAD_PCT


def pct(new: float, old: float) -> float:
    return (new / old - 1.0) * 100.0 if new > 0.0 and old > 0.0 else 0.0


def point_in_time_features(rows: Sequence[Any], index: int) -> dict[str, float] | None:
    if index < 48:
        return None
    latest = float(rows[index].close)
    closes = [float(rows[index - n].close) for n in (1, 2, 3, 6, 12, 48)]
    if min([latest, *closes]) <= 0.0:
        return None
    r5 = pct(latest, closes[0])
    r10 = pct(latest, closes[1])
    r15 = pct(latest, closes[2])
    r30 = pct(latest, closes[3])
    r60 = pct(latest, closes[4])
    r4h = pct(latest, closes[5])
    recent_vol = mean(float(c.volume) for c in rows[index - 1:index + 1])
    prior_vol = mean(float(c.volume) for c in rows[index - 7:index - 1])
    volume_accel = recent_vol / prior_vol if prior_vol > 0.0 else 0.0
    weakness_score = max(-r5 / 0.25, -r10 / 0.40, -r30 / 0.80, -r60 / 1.25)
    return {
        "return_5m_pct": round(r5, 8),
        "return_10m_pct": round(r10, 8),
        "return_15m_pct": round(r15, 8),
        "return_30m_pct": round(r30, 8),
        "return_1h_pct": round(r60, 8),
        "return_4h_pct": round(r4h, 8),
        "accel_5m_vs_15m": round(r5 * 3.0 - r15, 8),
        "accel_10m_vs_30m": round(r10 * 3.0 - r30, 8),
        "volume_accel_ratio": round(volume_accel, 8),
        "weakness_score": round(weakness_score, 8),
    }


def short_forward_labels(rows: Sequence[Any], timestamps: Sequence[int], index: int) -> dict[str, dict[str, float | int | None]]:
    signal_ms = int(rows[index].timestamp_ms)
    entry = float(rows[index].close)
    result: dict[str, dict[str, float | int | None]] = {}
    for horizon in LABEL_HORIZONS_MINUTES:
        target_ms = signal_ms + int(horizon) * 60_000
        if not timestamps or timestamps[-1] < target_ms:
            result[str(horizon)] = {"mature": 0, "gross_short_pct": None, "net_short_pct": None}
            continue
        j = bisect.bisect_right(timestamps, target_ms) - 1
        if j < index:
            result[str(horizon)] = {"mature": 0, "gross_short_pct": None, "net_short_pct": None}
            continue
        exit_price = float(rows[j].close)
        gross_short = -pct(exit_price, entry)
        result[str(horizon)] = {
            "mature": 1,
            "gross_short_pct": round(gross_short, 6),
            "net_short_pct": round(gross_short - ROUNDTRIP_COST_PCT, 6),
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
        p5 = closes.get(ts - FIVE_MINUTE_MS)
        p10 = closes.get(ts - 2 * FIVE_MINUTE_MS)
        p15 = closes.get(ts - 3 * FIVE_MINUTE_MS)
        p60 = closes.get(ts - 12 * FIVE_MINUTE_MS)
        if None in (p5, p10, p15, p60):
            continue
        close = float(candle.close)
        out[ts] = {
            "btc_return_5m_pct": round(pct(close, float(p5)), 8),
            "btc_return_10m_pct": round(pct(close, float(p10)), 8),
            "btc_return_15m_pct": round(pct(close, float(p15)), 8),
            "btc_return_1h_pct": round(pct(close, float(p60)), 8),
        }
    return out


def keep_bounded(bucket: list[dict[str, Any]], candidate: dict[str, Any]) -> None:
    bucket.append(candidate)
    if len(bucket) > MAX_CANDIDATES_PER_MOMENT * 2:
        bucket.sort(key=lambda item: (float(item["entry_features"]["weakness_score"]), str(item["market"])), reverse=True)
        del bucket[MAX_CANDIDATES_PER_MOMENT:]


def attach_cross_section(rows: list[dict[str, Any]]) -> None:
    by_time: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_time[int(row["signal_ms"])].append(row)
    for group in by_time.values():
        count = len(group)
        fields = {
            "rank_weakness_score": ("entry_features", "weakness_score", True),
            "rank_weakest_5m": ("entry_features", "return_5m_pct", False),
            "rank_weakest_10m": ("entry_features", "return_10m_pct", False),
            "rank_relative_weakness_1h": ("relative_weakness_vs_btc_1h_pct", None, True),
            "rank_volume_accel": ("entry_features", "volume_accel_ratio", True),
        }
        for row in group:
            cross = {"simultaneous_bear_candidate_count": count}
            for output, (outer, inner, descending) in fields.items():
                def value(item: dict[str, Any]) -> float:
                    raw = item[outer] if inner is None else item[outer][inner]
                    try:
                        x = float(raw)
                    except (TypeError, ValueError, OverflowError):
                        return float("-inf") if descending else float("inf")
                    return x if math.isfinite(x) else (float("-inf") if descending else float("inf"))
                own = value(row)
                rank = 1 + sum(value(other) > own for other in group) if descending else 1 + sum(value(other) < own for other in group)
                cross[output] = rank
            row["cross_section"] = cross


def run_export(api: BitvavoPublic, *, days: int = 90, now_ms: int | None = None, markets: Sequence[str] | None = None, output_path: str | None = None) -> dict[str, Any]:
    if days != 90:
        raise ValueError("bear research dataset vereist exact 90 signaaldagen")
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
        raise RuntimeError("onvoldoende BTC-historie voor bear research dataset")
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
                if not features or float(features["weakness_score"]) < 1.0:
                    continue
                trigger_rows_seen += 1
                labels = short_forward_labels(rows, timestamps, i)
                if not all(int(item["mature"]) == 1 for item in labels.values()):
                    incomplete_labels += 1
                    continue
                btc_ctx = btc_map.get(ts)
                if btc_ctx is None:
                    continue
                relative_weakness = float(btc_ctx["btc_return_1h_pct"]) - float(features["return_1h_pct"])
                candidate = {
                    "market": market,
                    "signal_ms": ts,
                    "entry_reference": float(rows[i].close),
                    "entry_features": features,
                    "relative_weakness_vs_btc_1h_pct": round(relative_weakness, 8),
                    "forward_short_labels": labels,
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
        bucket.sort(key=lambda item: (float(item["entry_features"]["weakness_score"]), str(item["market"])), reverse=True)
        for candidate in bucket[:MAX_CANDIDATES_PER_MOMENT]:
            market_context = context.get(int(ts))
            btc_ctx = btc_map.get(int(ts))
            if market_context is None or btc_ctx is None:
                continue
            candidate["market_context"] = {**market_context, **btc_ctx}
            candidates.append(candidate)

    attach_cross_section(candidates)
    candidates.sort(key=lambda item: (int(item["signal_ms"]), str(item["market"])))

    report = {
        "version": "v40-bear-research-dataset-1",
        "component": "V40_BEAR_RESEARCH_DATASET",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "OFFLINE_MEASUREMENT_ONLY",
        "execution_enabled": False,
        "live_orders_possible": False,
        "active_paper_changed": False,
        "future_data_used_for_features": False,
        "future_data_used_for_labels": True,
        "sampling_independent_of_long_candidate_generator": True,
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
            "purge_required_at_split_boundaries": True
        },
        "cost_model": {"roundtrip_cost_pct": ROUNDTRIP_COST_PCT, "assumed_spread_pct": ASSUMED_SPREAD_PCT},
        "attention_funnel": {
            "trigger": "max(-r5/0.25,-r10/0.40,-r30/0.80,-r1h/1.25)>=1",
            "max_candidates_per_moment": MAX_CANDIDATES_PER_MOMENT,
            "purpose": "broad point-in-time bearish attention set, not a trade rule"
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
            "Geen LONG-signaalgenerator gebruikt voor sampling.",
            "Alle features zijn point-in-time en gebruiken alleen gesloten candles.",
            "Toekomstdata staat uitsluitend in forward_short_labels.",
            "Laatste 15 dagen blijven voor latere untouched evaluatie en worden niet door de exporter beoordeeld.",
            "Dit is measurement-only; geen PAPER/live/config/deploy wijziging."
        ]
    }
    if output_path:
        Path(output_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="v4.0 dedicated bear/short research dataset")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--now-ms", type=int, default=None)
    parser.add_argument("--markets", default="")
    parser.add_argument("--output", default="cryptobot_v40_bear_research_dataset.json")
    args = parser.parse_args()
    api = BitvavoPublic("https://api.bitvavo.com/v2", timeout_seconds=20, retries=4)
    markets = [item.strip().upper() for item in args.markets.split(",") if item.strip()] or None
    report = run_export(api, days=args.days, now_ms=args.now_ms, markets=markets, output_path=args.output)
    print("=== v4.0 BEAR/SHORT RESEARCH DATASET ===")
    print("UITVOERING : UIT / OFFLINE METING")
    print(f"MARKTEN    : {report['markets_completed']}/{report['markets_requested']}")
    print(f"TRIGGERS   : {report['trigger_rows_seen']}")
    print(f"KANDIDATEN : {report['candidates']}")
    print(f"MOMENTEN   : {report['moments']}")
    print(f"FOUTEN     : {len(report['errors'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
