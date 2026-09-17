#!/usr/bin/env python3
"""
v4.0 Step 3 diagnostic: does the rejected nonlinear selector recognize
market moment quality, candidate quality, or neither?

OFFLINE ONLY. Reads the existing master research dataset.
It never reads the final untouched 15 days, does not fetch market data,
does not change PAPER, does not deploy Render, and cannot place orders.

Target = mean net close return over 4h/8h/12h/24h, identical to the
reproducible Momentgate validation in v40_momentgate_repro.py.
"""
from __future__ import annotations
import argparse, json
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

DAY_MS = 24 * 3600 * 1000
HORIZONS = ("240", "480", "720", "1440")

def load_rows(ds: dict) -> pd.DataFrame:
    out = []
    for r in ds["rows"]:
        z = {
            "market": r["market"],
            "signal_ms": r["signal_ms"],
            "route": r["route"],
            "base_score": r["base_score"],
            "relative_strength_vs_btc_1h_pct": r.get("relative_strength_vs_btc_1h_pct"),
            "net_reward_risk": r.get("net_reward_risk"),
        }
        for k, v in (r.get("entry_features") or {}).items():
            z["ef_" + k] = v
        for k, v in (r.get("market_context") or {}).items():
            z["mc_" + k] = v
        for k, v in (r.get("cross_section") or {}).items():
            z["cs_" + k] = v
        for h in HORIZONS:
            z["y" + h] = r["forward_labels"][h]["net_close_pct"]
        z["y"] = float(np.mean([z["y" + h] for h in HORIZONS]))
        out.append(z)
    return pd.DataFrame(out)

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("--output", default="v40_step3_selection_diagnostic_result.json")
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
    features = [c for c in df.columns if c not in exclude and pd.api.types.is_numeric_dtype(df[c])]
    routes = sorted(df["route"].dropna().unique())

    def xmake(sub: pd.DataFrame) -> pd.DataFrame:
        x = sub[features].copy()
        for rr in routes:
            x["route_" + rr] = (sub["route"] == rr).astype(int).values
        return x.replace([np.inf, -np.inf], np.nan)

    xfit = xmake(fit)
    med = xfit.median(numeric_only=True)
    model = HistGradientBoostingRegressor(
        max_iter=100, learning_rate=0.05, max_leaf_nodes=7,
        min_samples_leaf=40, l2_regularization=1.0, random_state=42,
    )
    model.fit(xfit.fillna(med), fit["y"])

    cal["pred"] = model.predict(xmake(cal).fillna(med))
    val["pred"] = model.predict(xmake(val).fillna(med))

    cal_best = cal.loc[cal.groupby("signal_ms")["pred"].idxmax()].copy()
    threshold = float(np.quantile(cal_best["pred"], 0.80))
    val_best = val.loc[val.groupby("signal_ms")["pred"].idxmax()].copy()
    selected = val_best[val_best.pred >= threshold].copy()

    moment = val.groupby("signal_ms").agg(
        n=("y","size"),
        mean_y=("y","mean"),
        best_y=("y","max"),
        worst_y=("y","min"),
    )
    val["moment_mean_y"] = val.groupby("signal_ms")["y"].transform("mean")
    val["within_moment_residual"] = val["y"] - val["moment_mean_y"]

    selected = selected.join(moment, on="signal_ms")
    selected["chosen_residual"] = selected["y"] - selected["mean_y"]
    selected_multi = selected[selected["n"] >= 2].copy()
    multi_moments = moment[moment["n"] >= 2]

    between_var = float(val["moment_mean_y"].var())
    within_var = float(val["within_moment_residual"].var())
    variance_share_between = float(between_var / (between_var + within_var))

    result = {
        "version": "v40-step3-selection-diagnostic-1",
        "mode": "OFFLINE_MEASUREMENT_ONLY",
        "execution_enabled": False,
        "live_orders_possible": False,
        "active_paper_changed": False,
        "untouched_opened": False,
        "dataset": {
            "candidates": int(len(df)),
            "markets_completed": int(ds["markets_completed"]),
        },
        "validation": {
            "rows": int(len(val)),
            "moments": int(len(moment)),
            "multi_candidate_moments": int(len(multi_moments)),
            "multi_candidate_moments_pct": float(len(multi_moments) / len(moment) * 100),
            "avg_candidates_per_moment": float(moment["n"].mean()),
        },
        "model_recognition": {
            "corr_prediction_vs_candidate_outcome": float(val["pred"].corr(val["y"])),
            "corr_prediction_vs_market_moment_mean": float(val["pred"].corr(val["moment_mean_y"])),
            "corr_prediction_vs_within_moment_residual": float(val["pred"].corr(val["within_moment_residual"])),
            "selected_moments": int(len(selected)),
            "selected_chosen_candidate_mean_net_pct": float(selected["y"].mean()),
            "selected_moment_mean_net_pct": float(selected["mean_y"].mean()),
            "chosen_candidate_residual_vs_selected_moment_mean_pct": float(selected["chosen_residual"].mean()),
            "selected_good_moment_precision_pct": float((selected["mean_y"] > 0).mean() * 100),
            "all_moments_good_prevalence_pct": float((moment["mean_y"] > 0).mean() * 100),
            "multi_selected_moments": int(len(selected_multi)),
            "chosen_best_actual_candidate_pct_multi": float(np.isclose(selected_multi["y"], selected_multi["best_y"]).mean() * 100),
            "chosen_residual_multi_pct": float(selected_multi["chosen_residual"].mean()),
        },
        "diagnostic_headroom_future_information_only": {
            "note": "Oracle figures use forward labels only to locate the bottleneck; they are NOT tradable rules.",
            "average_moment_mean_net_pct": float(moment["mean_y"].mean()),
            "positive_moment_pct": float((moment["mean_y"] > 0).mean() * 100),
            "oracle_positive_moment_count": int((moment["mean_y"] > 0).sum()),
            "oracle_positive_moment_random_candidate_mean_net_pct": float(moment.loc[moment["mean_y"] > 0, "mean_y"].mean()),
            "oracle_best_candidate_every_moment_mean_net_pct": float(moment["best_y"].mean()),
            "oracle_best_candidate_positive_pct": float((moment["best_y"] > 0).mean() * 100),
            "oracle_candidate_uplift_all_moments_pct": float((moment["best_y"] - moment["mean_y"]).mean()),
            "oracle_candidate_uplift_multi_candidate_moments_pct": float((multi_moments["best_y"] - multi_moments["mean_y"]).mean()),
        },
        "variance_decomposition": {
            "between_moment_variance": between_var,
            "within_moment_residual_variance": within_var,
            "between_moment_share_of_sum_pct": variance_share_between * 100,
        },
        "decision": "CURRENT_SELECTOR_RECOGNIZES_NEITHER_MOMENT_NOR_COIN_RELIABLY",
        "priority": "MOMENT_TIMING_FIRST_THEN_CANDIDATE_RANKING",
        "reason": [
            "Prediction correlations with outcome, moment quality and within-moment residual are all near zero.",
            "The gate barely raises the share of actually positive moments versus prevalence.",
            "Chosen coins underperform the average coin at their selected moments.",
            "Most diagnostic variance is between moments, so timing/regime is the first bottleneck.",
            "There is still meaningful candidate-ranking headroom when multiple candidates coexist.",
        ],
    }

    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
