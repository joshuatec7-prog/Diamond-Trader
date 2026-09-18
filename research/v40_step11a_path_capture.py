#!/usr/bin/env python3
"""Step 11A: capture forward 5m paths for exit/stop/runner/risk research.

OFFLINE RESEARCH ONLY. No runtime/PAPER/live changes.

Purpose:
- Existing Step 5/6 datasets only contain fixed-horizon endpoint returns.
- Stops, trailing exits, runners, MAE/MFE and sizing need the full price path.
- Capture a deterministic sample of baseline-ranked development events only.
- Entire signal + forward path must remain before development day 60.
- Step 5/6 validation and already-opened final15 are not used for exit tuning.

Selection:
- Same fixed HGB family as frozen rankers.
- Two forward development windows:
    A train days [0,30), test [31,45)
    B train days [0,45), test [46,59)
- Confidence threshold = training-only 80th percentile of best-per-moment scores.
- RANGE_LONG is excluded because Step 7 keeps it NO_TRADE.
- Deterministic evenly spaced sample, max 120 events per state/window.

Path:
- Closed 5m candles.
- SHORT max path 1440m; RANGE_SHORT max path 720m.
- Bitvavo-aligned raw REST boundaries, avoiding generic end_ms-1 pagination.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from bitvavo_public import BitvavoPublic
from research.v40_step9_candidate_ranking import (
    DAY_MS,
    _features,
    _load_bear,
    _load_range,
    _make_model,
)

MINUTE_MS = 60_000
BAR_MS = 5 * MINUTE_MS
TOP_QUANTILE = 0.80
SAMPLE_PER_STATE_WINDOW = 120
ROUNDTRIP_COST_PCT = 0.78
WINDOWS = (
    {"name": "A", "train_start_day": 0, "train_end_day": 30, "test_start_day": 31, "test_end_day": 45},
    {"name": "B", "train_start_day": 0, "train_end_day": 45, "test_start_day": 46, "test_end_day": 59},
)
PATH_MINUTES = {"SHORT": 1440, "RANGE": 720}


def _even_sample(df: pd.DataFrame, n: int) -> pd.DataFrame:
    ordered = df.sort_values(["signal_ms", "market"]).reset_index(drop=True)
    if len(ordered) <= n:
        return ordered.copy()
    idx = np.unique(np.linspace(0, len(ordered) - 1, num=n, dtype=int))
    return ordered.iloc[idx].copy().reset_index(drop=True)


def _select_window(
    df: pd.DataFrame,
    start: int,
    spec: dict[str, Any],
    seed: int,
    state: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    train = df[
        (df.signal_ms >= start + int(spec["train_start_day"]) * DAY_MS)
        & (df.signal_ms < start + int(spec["train_end_day"]) * DAY_MS)
    ].copy()
    test = df[
        (df.signal_ms >= start + int(spec["test_start_day"]) * DAY_MS)
        & (df.signal_ms < start + int(spec["test_end_day"]) * DAY_MS)
    ].copy()
    assert not train.empty and not test.empty
    assert int(train.signal_ms.max()) < int(test.signal_ms.min())
    assert int(test.signal_ms.max()) < start + 59 * DAY_MS

    features = _features(df)
    xtrain = train[features].replace([np.inf, -np.inf], np.nan)
    med = xtrain.median(numeric_only=True)
    model = _make_model(seed)
    model.fit(xtrain.fillna(med), train.outcome)

    train["score"] = model.predict(
        train[features].replace([np.inf, -np.inf], np.nan).fillna(med)
    )
    test["score"] = model.predict(
        test[features].replace([np.inf, -np.inf], np.nan).fillna(med)
    )
    train_best = train.loc[train.groupby("signal_ms")["score"].idxmax()].copy()
    test_best = test.loc[test.groupby("signal_ms")["score"].idxmax()].copy()
    threshold = float(np.quantile(train_best.score, TOP_QUANTILE))
    selected = test_best[test_best.score >= threshold].copy()
    if state == "RANGE":
        selected = selected[selected.side == "RANGE_SHORT"].copy()

    sampled = _even_sample(selected, SAMPLE_PER_STATE_WINDOW)
    sampled["state"] = state
    sampled["window"] = str(spec["name"])

    meta = {
        "window": spec,
        "train_rows": int(len(train)),
        "test_rows": int(len(test)),
        "train_moments": int(train.signal_ms.nunique()),
        "test_moments": int(test.signal_ms.nunique()),
        "training_threshold_quantile": TOP_QUANTILE,
        "threshold": threshold,
        "selected_before_sample": int(len(selected)),
        "sampled": int(len(sampled)),
        "feature_count": int(len(features)),
    }
    return sampled, meta


def _aligned_closed_5m(
    api: BitvavoPublic,
    market: str,
    signal_ms: int,
    path_minutes: int,
):
    if signal_ms % BAR_MS != 0:
        raise ValueError("signal_ms is niet 5m-uitgelijnd")
    target_start = int(signal_ms) + int(path_minutes) * MINUTE_MS
    cutoff = target_start + BAR_MS
    if cutoff % BAR_MS != 0:
        raise ValueError("cutoff is niet 5m-uitgelijnd")
    limit = int(path_minutes // 5) + 3
    payload = api._get(
        f"/{market}/candles",
        {
            "interval": "5m",
            "limit": min(1440, limit),
            "start": int(signal_ms),
            "end": int(cutoff),
        },
    )
    rows = api._parse_candles(payload, market)
    return [
        c for c in rows
        if int(signal_ms) <= int(c.timestamp_ms) <= target_start
        and int(c.timestamp_ms) + BAR_MS <= cutoff
    ]


def _path_record(
    api: BitvavoPublic,
    row: pd.Series,
    development_cutoff_ms: int,
) -> dict[str, Any]:
    state = str(row.state)
    signal_ms = int(row.signal_ms)
    market = str(row.market)
    path_minutes = PATH_MINUTES[state]
    assert signal_ms + path_minutes * MINUTE_MS + BAR_MS <= development_cutoff_ms

    candles = _aligned_closed_5m(api, market, signal_ms, path_minutes)
    by_ts = {int(c.timestamp_ms): c for c in candles if c.is_valid}
    entry_candle = by_ts.get(signal_ms)
    if entry_candle is None:
        raise RuntimeError("entry candle ontbreekt")
    entry = float(entry_candle.close)
    if not math.isfinite(entry) or entry <= 0:
        raise RuntimeError("ongeldige entry")

    future = [
        c for c in candles
        if signal_ms < int(c.timestamp_ms) <= signal_ms + path_minutes * MINUTE_MS
    ]
    expected = path_minutes // 5
    if len(future) < math.floor(expected * 0.95):
        raise RuntimeError(f"onvoldoende 5m path coverage {len(future)}/{expected}")

    lows = [(int(c.timestamp_ms), float(c.low)) for c in future]
    highs = [(int(c.timestamp_ms), float(c.high)) for c in future]
    min_ts, min_low = min(lows, key=lambda x: x[1])
    max_ts, max_high = max(highs, key=lambda x: x[1])
    last = max(future, key=lambda c: int(c.timestamp_ms))
    favorable = (entry - min_low) / entry * 100.0
    adverse = (max_high - entry) / entry * 100.0
    gross_end = (entry - float(last.close)) / entry * 100.0

    compact_path = [
        [
            int((int(c.timestamp_ms) - signal_ms) // MINUTE_MS),
            round(float(c.open), 10),
            round(float(c.high), 10),
            round(float(c.low), 10),
            round(float(c.close), 10),
            round(float(c.volume), 10),
        ]
        for c in future
    ]

    return {
        "state": state,
        "window": str(row.window),
        "market": market,
        "signal_ms": signal_ms,
        "score": float(row.score),
        "side": "SHORT" if state == "SHORT" else "RANGE_SHORT",
        "path_minutes": path_minutes,
        "entry_close": entry,
        "bars_expected": int(expected),
        "bars_captured": int(len(future)),
        "coverage_pct": float(len(future) / expected * 100.0),
        "mfe_gross_pct": float(favorable),
        "mae_gross_pct": float(adverse),
        "time_to_mfe_min": int((min_ts - signal_ms) // MINUTE_MS),
        "time_to_mae_min": int((max_ts - signal_ms) // MINUTE_MS),
        "end_gross_short_pct": float(gross_end),
        "end_net_short_pct": float(gross_end - ROUNDTRIP_COST_PCT),
        "path": compact_path,
    }


def _dist(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"n": 0, "median": None, "p25": None, "p75": None, "p90": None}
    arr = np.asarray(values, dtype=float)
    return {
        "n": int(len(arr)),
        "median": float(np.median(arr)),
        "p25": float(np.quantile(arr, 0.25)),
        "p75": float(np.quantile(arr, 0.75)),
        "p90": float(np.quantile(arr, 0.90)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bear_dataset")
    ap.add_argument("range_dataset")
    ap.add_argument("--output", default="v40_step11a_path_capture_result.json")
    args = ap.parse_args()

    bear, bstart, _ = _load_bear(args.bear_dataset)
    rang, rstart, _ = _load_range(args.range_dataset)
    if bstart != rstart:
        raise RuntimeError("bear/range start verschillen")
    start = int(bstart)
    development_cutoff = start + 60 * DAY_MS

    parts: list[pd.DataFrame] = []
    selection_meta: dict[str, Any] = {"SHORT": [], "RANGE": []}
    for state, df, seed in (("SHORT", bear, 52), ("RANGE", rang, 62)):
        for i, spec in enumerate(WINDOWS):
            sampled, meta = _select_window(df, start, spec, seed + i * 10, state)
            parts.append(sampled)
            selection_meta[state].append(meta)

    events = pd.concat(parts, ignore_index=True).sort_values(
        ["signal_ms", "state", "market"]
    ).reset_index(drop=True)
    # Hard guard: even 24h SHORT path may not touch day 60+.
    assert all(
        int(row.signal_ms) + PATH_MINUTES[str(row.state)] * MINUTE_MS + BAR_MS
        <= development_cutoff
        for _, row in events.iterrows()
    )

    api = BitvavoPublic("https://api.bitvavo.com/v2", timeout_seconds=15, retries=4)
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for i, row in events.iterrows():
        try:
            records.append(_path_record(api, row, development_cutoff))
        except Exception as exc:
            errors.append(
                f"{row.state} {row.window} {row.market} {int(row.signal_ms)}: "
                f"{type(exc).__name__}: {exc}"
            )
        if (i + 1) % 40 == 0:
            print(
                f"path progress {i + 1}/{len(events)} usable={len(records)} errors={len(errors)}",
                flush=True,
            )
        time.sleep(0.02)

    requested = int(len(events))
    usable = int(len(records))
    usable_pct = float(usable / requested * 100.0) if requested else 0.0
    if usable_pct < 90.0:
        raise RuntimeError(f"path capture te onvolledig: {usable}/{requested}")

    by_state: dict[str, Any] = {}
    for state in ("SHORT", "RANGE"):
        sub = [r for r in records if r["state"] == state]
        by_state[state] = {
            "n": len(sub),
            "mfe_gross_pct": _dist([float(r["mfe_gross_pct"]) for r in sub]),
            "mae_gross_pct": _dist([float(r["mae_gross_pct"]) for r in sub]),
            "time_to_mfe_min": _dist([float(r["time_to_mfe_min"]) for r in sub]),
            "end_net_short_pct": _dist([float(r["end_net_short_pct"]) for r in sub]),
        }

    result = {
        "version": "v40-step11a-path-capture-1",
        "mode": "OFFLINE_MEASUREMENT_ONLY",
        "execution_enabled": False,
        "live_orders_possible": False,
        "active_paper_changed": False,
        "new_untouched_test": False,
        "purpose": "Capture development-only 5m paths before testing exits/stops/runners/sizing/risk.",
        "development_cutoff_day": 60,
        "days_60_plus_used": False,
        "step5_6_validation_reused": False,
        "final15_reused": False,
        "path_interval": "5m",
        "roundtrip_cost_pct_for_later_analysis": ROUNDTRIP_COST_PCT,
        "selection": selection_meta,
        "sample": {
            "target_per_state_window": SAMPLE_PER_STATE_WINDOW,
            "requested": requested,
            "usable": usable,
            "usable_pct": usable_pct,
            "errors": len(errors),
            "error_examples": errors[:20],
            "method": "training-only 80th percentile confidence then deterministic even sample",
        },
        "descriptive": by_state,
        "events": records,
        "decision": "PATH_DATASET_READY_FOR_STEP11B",
        "notes": [
            "No exit thresholds are selected in Step 11A.",
            "RANGE_LONG excluded because frozen Step 7 keeps it NO_TRADE.",
            "All full paths, including the last 5m candle close, remain before day 60.",
            "No Render/PAPER/live/runtime changes.",
        ],
    }
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({
        "decision": result["decision"],
        "requested": requested,
        "usable": usable,
        "usable_pct": usable_pct,
        "short_n": by_state["SHORT"]["n"],
        "range_n": by_state["RANGE"]["n"],
        "short_mfe_median": by_state["SHORT"]["mfe_gross_pct"]["median"],
        "short_mae_median": by_state["SHORT"]["mae_gross_pct"]["median"],
        "range_mfe_median": by_state["RANGE"]["mfe_gross_pct"]["median"],
        "range_mae_median": by_state["RANGE"]["mae_gross_pct"]["median"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
