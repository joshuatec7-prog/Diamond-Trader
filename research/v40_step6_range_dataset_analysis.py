#!/usr/bin/env python3
"""
Step 6 analysis for dedicated sideways/range research dataset.

OFFLINE ONLY. Final 15 days remain sealed. One fixed model family, no sweep.
Evaluates both range-long and range-short edge reversions after fixed costs.
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
PASS = {
    "n_min": 100,
    "mean_gt_pct": 0.0,
    "pf_min": 1.20,
    "positive_horizons_min": 3,
    "positive_days_pct_min": 50.0,
}


def load_rows(dataset: dict) -> pd.DataFrame:
    rows = []
    for src in dataset["rows"]:
        rec = {
            "market": src["market"],
            "signal_ms": int(src["signal_ms"]),
            "side": src["side"],
            "side_num": 1.0 if src["side"] == "RANGE_LONG" else -1.0,
        }
        for key, value in (src.get("entry_features") or {}).items():
            if key != "side":
                rec["ef_" + key] = value
        for key, value in (src.get("market_context") or {}).items():
            rec["mc_" + key] = value
        for key, value in (src.get("cross_section") or {}).items():
            rec["cs_" + key] = value
        for horizon in HORIZONS:
            rec["r" + horizon] = float(src["forward_range_labels"][horizon]["net_range_pct"])
        rec["r"] = float(np.mean([rec["r" + h] for h in HORIZONS]))
        rows.append(rec)
    return pd.DataFrame(rows)


def metrics(sub: pd.DataFrame, target: str = "r") -> dict:
    vals = sub[target].to_numpy(dtype=float)
    pos = vals[vals > 0].sum()
    neg = -vals[vals < 0].sum()
    out = {
        "n": int(len(vals)),
        "mean_pct": float(vals.mean()) if len(vals) else None,
        "median_pct": float(np.median(vals)) if len(vals) else None,
        "pf": float(pos / neg) if neg > 0 else None,
        "win_pct": float((vals > 0).mean() * 100) if len(vals) else None,
    }
    positive_horizons = 0
    for h in HORIZONS:
        arr = sub["r" + h].to_numpy(dtype=float)
        p = arr[arr > 0].sum()
        n = -arr[arr < 0].sum()
        out[h + "_mean_pct"] = float(arr.mean()) if len(arr) else None
        out[h + "_pf"] = float(p / n) if n > 0 else None
        positive_horizons += int(len(arr) > 0 and arr.mean() > 0)
    out["positive_horizons"] = int(positive_horizons)
    if len(sub):
        temp = sub.copy()
        temp["day"] = pd.to_datetime(temp.signal_ms, unit="ms", utc=True).dt.date
        daily = temp.groupby("day")[target].mean()
        out["days"] = int(len(daily))
        out["positive_days_pct"] = float((daily > 0).mean() * 100)
        out["max_market_share_pct"] = (
            float(temp.market.value_counts(normalize=True).max() * 100)
            if "market" in temp.columns else None
        )
        out["range_long_pct"] = (
            float((temp.side == "RANGE_LONG").mean() * 100)
            if "side" in temp.columns else None
        )
    else:
        out.update({"days": 0, "positive_days_pct": None, "max_market_share_pct": None, "range_long_pct": None})
    return out


def passes(m: dict) -> bool:
    return (
        m["n"] >= PASS["n_min"]
        and m["mean_pct"] > 0.0
        and m["pf"] is not None and m["pf"] >= PASS["pf_min"]
        and m["positive_horizons"] >= PASS["positive_horizons_min"]
        and m["positive_days_pct"] is not None and m["positive_days_pct"] >= PASS["positive_days_pct_min"]
    )


def aggregate_moments(df: pd.DataFrame) -> pd.DataFrame:
    mc = [c for c in df.columns if c.startswith("mc_")]
    agg = {"r": "mean", **{"r" + h: "mean" for h in HORIZONS}}
    agg.update({c: "first" for c in mc})
    agg["market"] = "count"
    return df.groupby("signal_ms").agg(agg).rename(columns={"market": "candidate_count"}).reset_index()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("--output", default="v40_step6_range_dataset_analysis_result.json")
    args = ap.parse_args()

    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    df = load_rows(dataset)
    moments = aggregate_moments(df)
    start = int(dataset["period"]["signal_start_ms"])
    untouched_start = start + 75 * DAY_MS

    fit = df[(df.signal_ms >= start) & (df.signal_ms < start + 45 * DAY_MS)].copy()
    cal = df[(df.signal_ms >= start + 46 * DAY_MS) & (df.signal_ms < start + 59 * DAY_MS)].copy()
    val = df[(df.signal_ms >= start + 60 * DAY_MS) & (df.signal_ms < start + 74 * DAY_MS)].copy()
    fit_m = moments[(moments.signal_ms >= start) & (moments.signal_ms < start + 45 * DAY_MS)].copy()
    cal_m = moments[(moments.signal_ms >= start + 46 * DAY_MS) & (moments.signal_ms < start + 59 * DAY_MS)].copy()
    val_m = moments[(moments.signal_ms >= start + 60 * DAY_MS) & (moments.signal_ms < start + 74 * DAY_MS)].copy()

    assert int(val.signal_ms.max()) < untouched_start
    assert int(val_m.signal_ms.max()) < untouched_start

    mc_features = [c for c in moments.columns if c.startswith("mc_")] + ["candidate_count"]
    xfitm = fit_m[mc_features].replace([np.inf, -np.inf], np.nan)
    medm = xfitm.median(numeric_only=True)
    moment_model = HistGradientBoostingRegressor(
        max_iter=100, learning_rate=0.05, max_leaf_nodes=7,
        min_samples_leaf=40, l2_regularization=1.0, random_state=61,
    )
    moment_model.fit(xfitm.fillna(medm), fit_m["r"])
    for sub in (cal_m, val_m):
        sub["pred"] = moment_model.predict(sub[mc_features].replace([np.inf, -np.inf], np.nan).fillna(medm))
    moment_threshold = float(np.quantile(cal_m["pred"], 0.80))
    cal_m_sel = cal_m[cal_m.pred >= moment_threshold].copy()
    val_m_sel = val_m[val_m.pred >= moment_threshold].copy()

    excluded = {"market", "signal_ms", "side", "r", *{"r" + h for h in HORIZONS}}
    candidate_features = [c for c in df.columns if c not in excluded and pd.api.types.is_numeric_dtype(df[c])]
    xfit = fit[candidate_features].replace([np.inf, -np.inf], np.nan)
    med = xfit.median(numeric_only=True)
    candidate_model = HistGradientBoostingRegressor(
        max_iter=100, learning_rate=0.05, max_leaf_nodes=7,
        min_samples_leaf=40, l2_regularization=1.0, random_state=62,
    )
    candidate_model.fit(xfit.fillna(med), fit["r"])
    for sub in (cal, val):
        sub["pred"] = candidate_model.predict(sub[candidate_features].replace([np.inf, -np.inf], np.nan).fillna(med))
    cal_best = cal.loc[cal.groupby("signal_ms")["pred"].idxmax()].copy()
    val_best = val.loc[val.groupby("signal_ms")["pred"].idxmax()].copy()
    candidate_threshold = float(np.quantile(cal_best["pred"], 0.80))
    cal_sel = cal_best[cal_best.pred >= candidate_threshold].copy()
    val_sel = val_best[val_best.pred >= candidate_threshold].copy()

    def neutral_regime(sub: pd.DataFrame) -> pd.DataFrame:
        return sub[
            sub["mc_breadth_positive_1h_pct"].between(40, 60)
            & sub["mc_breadth_positive_4h_pct"].between(40, 60)
            & (sub["mc_btc_return_1h_pct"].abs() <= 0.50)
        ].copy()

    neutral = neutral_regime(val)
    if len(neutral):
        most_stretched = neutral.loc[neutral.groupby("signal_ms")["ef_stretch_score"].idxmax()].copy()
    else:
        most_stretched = neutral

    vm = metrics(val_sel)
    mm = metrics(val_m_sel)
    result = {
        "version": "v40-step6-range-dataset-analysis-1",
        "mode": "OFFLINE_MEASUREMENT_ONLY",
        "execution_enabled": False,
        "live_orders_possible": False,
        "active_paper_changed": False,
        "untouched_opened": False,
        "dataset": {
            "candidates": int(len(df)), "moments": int(len(moments)),
            "markets_completed": int(dataset["markets_completed"]),
            "sampling_independent_of_long_candidate_generator": bool(dataset["sampling_independent_of_long_candidate_generator"]),
            "sampling_independent_of_bear_candidate_generator": bool(dataset["sampling_independent_of_bear_candidate_generator"]),
        },
        "split": {
            "fit_rows": int(len(fit)), "calibration_rows": int(len(cal)), "validation_rows": int(len(val)),
            "fit_moments": int(len(fit_m)), "calibration_moments": int(len(cal_m)), "validation_moments": int(len(val_m)),
            "purge_minutes": 1440, "untouched_start_ms": int(untouched_start), "untouched_opened": False,
        },
        "pass_criteria": PASS,
        "baseline_all_validation_candidates": metrics(val),
        "baseline_all_validation_moments": metrics(val_m),
        "market_moment_gate": {"threshold": moment_threshold, "calibration": metrics(cal_m_sel), "validation": mm, "passed": passes(mm)},
        "candidate_selector": {"threshold": candidate_threshold, "calibration": metrics(cal_sel), "validation": vm, "passed": passes(vm)},
        "interpretable_neutral_regime_most_stretched_each_moment": metrics(most_stretched),
        "decision": "PASS_RANGE_VALIDATION_KEEP_UNTOUCHED_SEALED" if passes(vm) else "REJECT_FIXED_RANGE_MODEL_KEEP_UNTOUCHED_SEALED",
    }
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({
        "decision": result["decision"],
        "candidate_validation_n": vm["n"],
        "candidate_validation_mean_pct": vm["mean_pct"],
        "candidate_validation_pf": vm["pf"],
        "candidate_positive_horizons": vm["positive_horizons"],
        "untouched_opened": False,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
