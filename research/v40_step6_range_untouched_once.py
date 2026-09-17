#!/usr/bin/env python3
"""
One-time untouched evaluator for Step 6 range research.

Must only be run after the frozen Step 6 validation result says PASS.
Refits the already-fixed candidate model on the same pre-untouched fit data,
uses the frozen calibration threshold from the validation result, and evaluates
only the final 15-day window. No tuning after opening.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

DAY_MS = 86_400_000
HORIZONS = ("60", "240", "480", "720")


def load_rows(dataset: dict) -> pd.DataFrame:
    rows = []
    for src in dataset["rows"]:
        rec = {
            "market": src["market"], "signal_ms": int(src["signal_ms"]),
            "side": src["side"], "side_num": 1.0 if src["side"] == "RANGE_LONG" else -1.0,
        }
        for key, value in (src.get("entry_features") or {}).items():
            if key != "side": rec["ef_" + key] = value
        for key, value in (src.get("market_context") or {}).items(): rec["mc_" + key] = value
        for key, value in (src.get("cross_section") or {}).items(): rec["cs_" + key] = value
        for h in HORIZONS: rec["r" + h] = float(src["forward_range_labels"][h]["net_range_pct"])
        rec["r"] = float(np.mean([rec["r" + h] for h in HORIZONS]))
        rows.append(rec)
    return pd.DataFrame(rows)


def metrics(sub: pd.DataFrame) -> dict:
    vals = sub.r.to_numpy(dtype=float)
    pos, neg = vals[vals > 0].sum(), -vals[vals < 0].sum()
    out = {"n": int(len(vals)), "mean_pct": float(vals.mean()), "median_pct": float(np.median(vals)),
           "pf": float(pos / neg) if neg > 0 else None, "win_pct": float((vals > 0).mean() * 100)}
    positive = 0
    for h in HORIZONS:
        arr = sub["r" + h].to_numpy(dtype=float); p = arr[arr > 0].sum(); n = -arr[arr < 0].sum()
        out[h + "_mean_pct"] = float(arr.mean()); out[h + "_pf"] = float(p / n) if n > 0 else None
        positive += int(arr.mean() > 0)
    out["positive_horizons"] = positive
    temp = sub.copy(); temp["day"] = pd.to_datetime(temp.signal_ms, unit="ms", utc=True).dt.date
    daily = temp.groupby("day").r.mean()
    out["days"] = int(len(daily)); out["positive_days_pct"] = float((daily > 0).mean() * 100)
    out["max_market_share_pct"] = float(temp.market.value_counts(normalize=True).max() * 100)
    out["range_long_pct"] = float((temp.side == "RANGE_LONG").mean() * 100)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("validation_result")
    ap.add_argument("--output", default="v40_step6_range_untouched_once_result.json")
    args = ap.parse_args()

    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    frozen = json.loads(Path(args.validation_result).read_text(encoding="utf-8"))
    if frozen.get("decision") != "PASS_RANGE_VALIDATION_KEEP_UNTOUCHED_SEALED":
        raise RuntimeError("untouched mag alleen open na frozen PASS-validatie")
    if frozen.get("untouched_opened") is not False:
        raise RuntimeError("verwacht sealed frozen validation result")

    df = load_rows(dataset)
    start = int(dataset["period"]["signal_start_ms"])
    untouched_start = start + 75 * DAY_MS
    signal_end = int(dataset["period"]["signal_end_ms_exclusive"])
    fit = df[(df.signal_ms >= start) & (df.signal_ms < start + 45 * DAY_MS)].copy()
    untouched = df[(df.signal_ms >= untouched_start) & (df.signal_ms < signal_end)].copy()

    excluded = {"market", "signal_ms", "side", "r", *{"r" + h for h in HORIZONS}}
    features = [c for c in df.columns if c not in excluded and pd.api.types.is_numeric_dtype(df[c])]
    xfit = fit[features].replace([np.inf, -np.inf], np.nan)
    med = xfit.median(numeric_only=True)
    model = HistGradientBoostingRegressor(max_iter=100, learning_rate=0.05, max_leaf_nodes=7,
        min_samples_leaf=40, l2_regularization=1.0, random_state=62)
    model.fit(xfit.fillna(med), fit.r)
    untouched["pred"] = model.predict(untouched[features].replace([np.inf, -np.inf], np.nan).fillna(med))
    best = untouched.loc[untouched.groupby("signal_ms")["pred"].idxmax()].copy()
    threshold = float(frozen["candidate_selector"]["threshold"])
    selected = best[best.pred >= threshold].copy()
    m = metrics(selected)
    criteria = frozen["pass_criteria"]
    passed = (m["n"] >= criteria["n_min"] and m["mean_pct"] > criteria["mean_gt_pct"] and
              m["pf"] is not None and m["pf"] >= criteria["pf_min"] and
              m["positive_horizons"] >= criteria["positive_horizons_min"] and
              m["positive_days_pct"] >= criteria["positive_days_pct_min"])
    result = {
        "version": "v40-step6-range-untouched-once-1",
        "mode": "OFFLINE_MEASUREMENT_ONLY", "execution_enabled": False,
        "live_orders_possible": False, "active_paper_changed": False,
        "untouched_opened": True, "no_tuning_after_open": True,
        "frozen_validation_decision": frozen["decision"],
        "candidate_threshold": threshold,
        "untouched": {"rows": int(len(untouched)), "selected": m},
        "pass_criteria": criteria, "passed": bool(passed),
        "decision": "PASS_RANGE_UNTOUCHED" if passed else "FAIL_RANGE_UNTOUCHED",
        "notes": ["Exact frozen candidate model family/random_state uit Step 6 validation.",
                  "Geen tuning na openen van laatste 15 dagen.",
                  "Research forward labels na vast kostenmodel; nog geen uitvoerbare tradingstrategie."],
    }
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"decision": result["decision"], "n": m["n"], "mean_pct": m["mean_pct"], "pf": m["pf"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
