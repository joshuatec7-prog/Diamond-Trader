#!/usr/bin/env python3
"""
v4.0 Momentgate reproducibility research.
OFFLINE ONLY. Reads an existing master dataset. Does not fetch market data,
change PAPER, deploy Render, or enable live orders.

Development design:
- 90d master dataset
- first 45d: model fit
- 24h purge
- next 13d: calibration
- 24h purge before validation
- next 14d: validation (the 15th day is purged before untouched boundary)
- final 15d: never read/evaluated by this script
Target = mean net close return over 4h/8h/12h/24h.
"""
from __future__ import annotations
import argparse, json
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

DAY_MS = 24 * 3600 * 1000
HORIZONS = ("240", "480", "720", "1440")

PASS = {
    "n_min": 100,
    "mean_target_pct_gt": 0.0,
    "pf_min": 1.20,
    "positive_horizons_min": 3,
    "positive_days_pct_min": 50.0,
    "max_market_share_pct_max": 10.0,
}

def load_rows(dataset: dict) -> pd.DataFrame:
    out = []
    for r in dataset["rows"]:
        rec = {
            "market": r["market"],
            "signal_ms": r["signal_ms"],
            "route": r["route"],
            "base_score": r["base_score"],
            "relative_strength_vs_btc_1h_pct": r.get("relative_strength_vs_btc_1h_pct"),
            "net_reward_risk": r.get("net_reward_risk"),
        }
        for k, v in (r.get("entry_features") or {}).items():
            rec["ef_" + k] = v
        for k, v in (r.get("market_context") or {}).items():
            rec["mc_" + k] = v
        for k, v in (r.get("cross_section") or {}).items():
            rec["cs_" + k] = v
        for h in HORIZONS:
            rec["y" + h] = r["forward_labels"][h]["net_close_pct"]
        rec["y"] = float(np.mean([rec["y" + h] for h in HORIZONS]))
        out.append(rec)
    return pd.DataFrame(out)

def metrics(s: pd.DataFrame) -> dict:
    vals = s["y"].to_numpy()
    pos = vals[vals > 0].sum()
    neg = -vals[vals < 0].sum()
    result = {
        "n": int(len(s)),
        "mean": float(vals.mean()) if len(vals) else None,
        "median": float(np.median(vals)) if len(vals) else None,
        "pf": float(pos / neg) if neg > 0 else None,
        "win_pct": float((vals > 0).mean() * 100) if len(vals) else None,
    }
    for h in HORIZONS:
        a = s["y" + h].to_numpy()
        p = a[a > 0].sum()
        n = -a[a < 0].sum()
        result[h + "_mean"] = float(a.mean()) if len(a) else None
        result[h + "_pf"] = float(p / n) if n > 0 else None
    tmp = s.copy()
    tmp["day"] = pd.to_datetime(tmp.signal_ms, unit="ms", utc=True).dt.date
    daily = tmp.groupby("day")["y"].mean()
    result["days"] = int(len(daily))
    result["positive_days_pct"] = float((daily > 0).mean() * 100) if len(daily) else None
    result["max_market_share_pct"] = (
        float(tmp.market.value_counts(normalize=True).max() * 100) if len(tmp) else None
    )
    return result

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("--output", default="v40_momentgate_repro_result.json")
    args = ap.parse_args()

    ds = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    df = load_rows(ds)
    start = int(ds["period"]["signal_start_ms"])

    fit_end = start + 45 * DAY_MS
    cal_start = fit_end + DAY_MS
    dev_end = start + 60 * DAY_MS
    cal_end = dev_end - DAY_MS
    val_end = dev_end + 15 * DAY_MS
    untouched_start = val_end

    fit = df[(df.signal_ms >= start) & (df.signal_ms < fit_end)].copy()
    cal = df[(df.signal_ms >= cal_start) & (df.signal_ms < cal_end)].copy()
    val = df[(df.signal_ms >= dev_end) & (df.signal_ms < val_end - DAY_MS)].copy()

    exclude = {"market","signal_ms","route","y","y240","y480","y720","y1440"}
    features = [
        c for c in df.columns
        if c not in exclude and pd.api.types.is_numeric_dtype(df[c])
    ]
    routes = sorted(df["route"].dropna().unique())

    def xmake(sub: pd.DataFrame) -> pd.DataFrame:
        x = sub[features].copy()
        for rr in routes:
            x["route_" + rr] = (sub["route"] == rr).astype(int).values
        return x.replace([np.inf, -np.inf], np.nan)

    xfit = xmake(fit)
    med = xfit.median(numeric_only=True)
    xfit = xfit.fillna(med)

    model = HistGradientBoostingRegressor(
        max_iter=100,
        learning_rate=0.05,
        max_leaf_nodes=7,
        min_samples_leaf=40,
        l2_regularization=1.0,
        random_state=42,
    )
    model.fit(xfit, fit["y"])

    def add_pred(sub: pd.DataFrame) -> pd.DataFrame:
        z = sub.copy()
        z["pred"] = model.predict(xmake(z).fillna(med))
        return z

    cal = add_pred(cal)
    val = add_pred(val)
    cal_best = cal.loc[cal.groupby("signal_ms")["pred"].idxmax()].copy()
    val_best = val.loc[val.groupby("signal_ms")["pred"].idxmax()].copy()

    threshold = float(np.quantile(cal_best["pred"], 0.80))
    cal_sel = cal_best[cal_best.pred >= threshold]
    val_sel = val_best[val_best.pred >= threshold]

    vm = metrics(val_sel)
    positive_horizons = sum(vm[h + "_mean"] > 0 for h in HORIZONS)
    passed = (
        vm["n"] >= PASS["n_min"]
        and vm["mean"] > PASS["mean_target_pct_gt"]
        and vm["pf"] >= PASS["pf_min"]
        and positive_horizons >= PASS["positive_horizons_min"]
        and vm["positive_days_pct"] >= PASS["positive_days_pct_min"]
        and vm["max_market_share_pct"] <= PASS["max_market_share_pct_max"]
    )

    result = {
        "version": "v40-momentgate-repro-1",
        "mode": "OFFLINE_MEASUREMENT_ONLY",
        "execution_enabled": False,
        "live_orders_possible": False,
        "active_paper_changed": False,
        "dataset": {
            "candidates": int(len(df)),
            "markets_completed": int(ds["markets_completed"]),
        },
        "split": {
            "fit_rows": int(len(fit)),
            "calibration_rows": int(len(cal)),
            "validation_rows": int(len(val)),
            "purge_minutes": 1440,
            "target_horizons_minutes": [240,480,720,1440],
            "untouched_start_ms": int(untouched_start),
            "untouched_opened": False,
        },
        "model": {
            "type": "HistGradientBoostingRegressor",
            "max_iter": 100,
            "learning_rate": 0.05,
            "max_leaf_nodes": 7,
            "min_samples_leaf": 40,
            "l2_regularization": 1.0,
            "random_state": 42,
            "gate": "top 20% calibration moment-level predictions",
            "threshold": threshold,
        },
        "pass_criteria": PASS,
        "calibration": metrics(cal_sel),
        "validation": vm,
        "baseline_validation_all_moments": metrics(val_best),
        "validation_passed": bool(passed),
        "decision": (
            "FREEZE_AND_ALLOW_ONE_UNTOUCHED_TEST"
            if passed else
            "REJECT_NONLINEAR_LONG_SELECTION_KEEP_UNTOUCHED_SEALED"
        ),
    }
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
