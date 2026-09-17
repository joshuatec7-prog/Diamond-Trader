#!/usr/bin/env python3
"""
v4.0 Step 4: LONG Momentgate research.

OFFLINE ONLY.
Purpose:
- decide whether an existing long-candidate moment is market-wide suitable for LONG
- use only point-in-time market context (+ simultaneous candidate count)
- do not choose the coin itself
- keep final 15 days sealed from evaluation

Design:
- 45d model fit
- 24h purge
- 13d calibration
- 24h purge
- 14d validation
- 24h purge before final 15d untouched
- target = mean candidate net close return at each market moment, averaged over 4h/8h/12h/24h
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

DAY_MS = 24 * 3600 * 1000
HORIZONS = ("240", "480", "720", "1440")

PASS = {
    "n_min": 100,
    "mean_gt_pct": 0.0,
    "pf_min": 1.20,
    "positive_horizons_min": 3,
    "positive_days_pct_min": 50.0,
}

def profit_factor(a: pd.Series) -> float | None:
    vals = a.to_numpy(dtype=float)
    pos = vals[vals > 0].sum()
    neg = -vals[vals < 0].sum()
    return float(pos / neg) if neg > 0 else None

def metrics(s: pd.DataFrame) -> dict:
    out = {
        "n": int(len(s)),
        "mean_pct": float(s["y"].mean()) if len(s) else None,
        "median_pct": float(s["y"].median()) if len(s) else None,
        "pf": profit_factor(s["y"]) if len(s) else None,
        "win_pct": float((s["y"] > 0).mean() * 100) if len(s) else None,
    }
    positive_horizons = 0
    for h in HORIZONS:
        mean = float(s["y" + h].mean()) if len(s) else None
        pf = profit_factor(s["y" + h]) if len(s) else None
        out[h + "_mean_pct"] = mean
        out[h + "_pf"] = pf
        if mean is not None and mean > 0:
            positive_horizons += 1
    out["positive_horizons"] = int(positive_horizons)
    if len(s):
        tmp = s.copy()
        tmp["day"] = pd.to_datetime(tmp["signal_ms"], unit="ms", utc=True).dt.date
        daily = tmp.groupby("day")["y"].mean()
        out["days"] = int(len(daily))
        out["positive_days_pct"] = float((daily > 0).mean() * 100)
    else:
        out["days"] = 0
        out["positive_days_pct"] = None
    return out

def passes(m: dict) -> bool:
    return (
        m["n"] >= PASS["n_min"]
        and m["mean_pct"] > PASS["mean_gt_pct"]
        and m["pf"] is not None and m["pf"] >= PASS["pf_min"]
        and m["positive_horizons"] >= PASS["positive_horizons_min"]
        and m["positive_days_pct"] is not None
        and m["positive_days_pct"] >= PASS["positive_days_pct_min"]
    )

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("--output", default="v40_step4_long_momentgate_result.json")
    args = ap.parse_args()

    ds = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    start = int(ds["period"]["signal_start_ms"])
    untouched_start = start + 75 * DAY_MS

    records = []
    context_keys = None
    for r in ds["rows"]:
        signal_ms = int(r["signal_ms"])
        if signal_ms >= untouched_start:
            continue
        mc = r["market_context"]
        if context_keys is None:
            context_keys = sorted(mc.keys())
        rec = {"signal_ms": signal_ms}
        for k in context_keys:
            rec["mc_" + k] = mc.get(k)
        rec["candidate_count"] = r["cross_section"]["simultaneous_candidate_count"]
        for h in HORIZONS:
            rec["y" + h] = r["forward_labels"][h]["net_close_pct"]
        rec["y"] = float(np.mean([rec["y" + h] for h in HORIZONS]))
        records.append(rec)

    df = pd.DataFrame(records)
    mc_cols = [c for c in df.columns if c.startswith("mc_")]

    context_consistent = all(
        int(df.groupby("signal_ms")[c].nunique(dropna=False).max()) <= 1
        for c in mc_cols
    )
    if not context_consistent:
        raise RuntimeError("market_context differs inside a simultaneous market moment")

    agg = {c: "first" for c in mc_cols}
    agg["candidate_count"] = "first"
    for h in HORIZONS:
        agg["y" + h] = "mean"
    agg["y"] = "mean"
    mom = df.groupby("signal_ms", as_index=False).agg(agg)

    fit = mom[(mom.signal_ms >= start) & (mom.signal_ms < start + 45 * DAY_MS)].copy()
    calibration = mom[
        (mom.signal_ms >= start + 46 * DAY_MS)
        & (mom.signal_ms < start + 59 * DAY_MS)
    ].copy()
    validation = mom[
        (mom.signal_ms >= start + 60 * DAY_MS)
        & (mom.signal_ms < start + 74 * DAY_MS)
    ].copy()

    features = mc_cols + ["candidate_count"]
    xfit = fit[features].replace([np.inf, -np.inf], np.nan)
    med = xfit.median(numeric_only=True)
    model = HistGradientBoostingRegressor(
        max_iter=100,
        learning_rate=0.05,
        max_leaf_nodes=7,
        min_samples_leaf=40,
        l2_regularization=1.0,
        random_state=42,
    )
    model.fit(xfit.fillna(med), fit["y"])

    def predict(sub: pd.DataFrame) -> pd.DataFrame:
        z = sub.copy()
        z["pred"] = model.predict(
            z[features].replace([np.inf, -np.inf], np.nan).fillna(med)
        )
        return z

    calibration = predict(calibration)
    validation = predict(validation)
    threshold = float(np.quantile(calibration["pred"], 0.80))
    cal_sel = calibration[calibration["pred"] >= threshold].copy()
    val_sel = validation[validation["pred"] >= threshold].copy()

    baseline = metrics(validation)
    model_cal = metrics(cal_sel)
    model_val = metrics(val_sel)

    def bull_rule(d: pd.DataFrame) -> pd.Series:
        return (
            (d["mc_breadth_positive_1h_pct"] >= 60.0)
            & (d["mc_breadth_positive_4h_pct"] >= 55.0)
            & (d["mc_btc_return_1h_pct"] > 0.0)
        )

    development = pd.concat([fit, calibration], ignore_index=True)
    bull_dev = metrics(development[bull_rule(development)].copy())
    bull_val = metrics(validation[bull_rule(validation)].copy())

    corr_pred_y = float(np.corrcoef(validation["pred"], validation["y"])[0, 1])

    model_pass = passes(model_cal) and passes(model_val)
    bull_pass = passes(bull_dev) and passes(bull_val)

    decision = (
        "FREEZE_LONG_MOMENTGATE_CANDIDATE_KEEP_UNTOUCHED_SEALED"
        if (model_pass or bull_pass)
        else "REJECT_STATIC_LONG_MOMENTGATE_KEEP_UNTOUCHED_SEALED"
    )

    result = {
        "version": "v40-step4-long-momentgate-1",
        "mode": "OFFLINE_MEASUREMENT_ONLY",
        "execution_enabled": False,
        "live_orders_possible": False,
        "active_paper_changed": False,
        "untouched_opened": False,
        "target": "market-moment mean net return across simultaneous long candidates and 4h/8h/12h/24h",
        "dataset": {
            "candidates_total_in_master": int(ds["candidates"]),
            "markets_completed": int(ds["markets_completed"]),
            "evaluated_moments_before_untouched": int(len(mom)),
        },
        "split": {
            "fit_moments": int(len(fit)),
            "calibration_moments": int(len(calibration)),
            "validation_moments": int(len(validation)),
            "purge_minutes": 1440,
            "untouched_start_ms": int(untouched_start),
            "untouched_opened": False,
        },
        "pass_criteria": PASS,
        "baseline_validation_all_long_candidate_moments": baseline,
        "market_context_model": {
            "type": "HistGradientBoostingRegressor",
            "features": features,
            "gate": "top 20% of calibration predicted moment quality",
            "threshold": threshold,
            "calibration": model_cal,
            "validation": model_val,
            "validation_prediction_correlation": corr_pred_y,
            "passed": bool(model_pass),
        },
        "interpretable_bull_rule": {
            "rule": "breadth_positive_1h>=60 AND breadth_positive_4h>=55 AND btc_return_1h>0",
            "development": bull_dev,
            "validation": bull_val,
            "passed": bool(bull_pass),
        },
        "decision": decision,
        "next_if_rejected": "Treat NO_LONG as default; research regime transition/change detection and separate BEAR/SHORT and RANGE states rather than tune a static long threshold endlessly.",
    }
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
