#!/usr/bin/env python3
"""
Step 5A: bear/short feasibility using the existing v4.0 master dataset.

OFFLINE ONLY.
Important limitation: the master dataset contains only existing LONG-candidate
rows. This script therefore tests whether bearish context can turn those
already-sampled moments into short opportunities. It does NOT claim to be a
marketwide short backtest.

The final 15 days remain sealed.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

DAY_MS = 86_400_000
HORIZONS = ("240", "480", "720", "1440")
ROUNDTRIP_COST_PCT = 0.78
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
            "route": src["route"],
            "base_score": src["base_score"],
            "relative_strength_vs_btc_1h_pct": src.get("relative_strength_vs_btc_1h_pct"),
            "net_reward_risk": src.get("net_reward_risk"),
        }
        for key, value in (src.get("entry_features") or {}).items():
            rec["ef_" + key] = value
        for key, value in (src.get("market_context") or {}).items():
            rec["mc_" + key] = value
        for key, value in (src.get("cross_section") or {}).items():
            rec["cs_" + key] = value
        for horizon in HORIZONS:
            gross_long = float(src["forward_labels"][horizon]["gross_close_pct"])
            rec["s" + horizon] = -gross_long - ROUNDTRIP_COST_PCT
        rec["s"] = float(np.mean([rec["s" + h] for h in HORIZONS]))
        rows.append(rec)
    return pd.DataFrame(rows)


def aggregate_moments(df: pd.DataFrame) -> pd.DataFrame:
    mc = [c for c in df.columns if c.startswith("mc_")]
    agg = {"s": "mean", **{"s" + h: "mean" for h in HORIZONS}}
    agg.update({c: "first" for c in mc})
    agg["market"] = "count"
    return df.groupby("signal_ms").agg(agg).rename(columns={"market": "candidate_count"}).reset_index()


def metrics(sub: pd.DataFrame, target: str = "s") -> dict:
    vals = sub[target].to_numpy(dtype=float)
    pos = vals[vals > 0].sum()
    neg = -vals[vals < 0].sum()
    result = {
        "n": int(len(vals)),
        "mean_pct": float(vals.mean()) if len(vals) else None,
        "median_pct": float(np.median(vals)) if len(vals) else None,
        "pf": float(pos / neg) if neg > 0 else None,
        "win_pct": float((vals > 0).mean() * 100) if len(vals) else None,
    }
    positive_horizons = 0
    for horizon in HORIZONS:
        arr = sub["s" + horizon].to_numpy(dtype=float)
        p = arr[arr > 0].sum()
        n = -arr[arr < 0].sum()
        result[horizon + "_mean_pct"] = float(arr.mean()) if len(arr) else None
        result[horizon + "_pf"] = float(p / n) if n > 0 else None
        positive_horizons += int(len(arr) > 0 and arr.mean() > 0)
    result["positive_horizons"] = int(positive_horizons)
    temp = sub.copy()
    temp["day"] = pd.to_datetime(temp.signal_ms, unit="ms", utc=True).dt.date
    daily = temp.groupby("day")[target].mean()
    result["days"] = int(len(daily))
    result["positive_days_pct"] = float((daily > 0).mean() * 100) if len(daily) else None
    return result


def bear_rule(sub: pd.DataFrame) -> pd.DataFrame:
    return sub[
        (sub["mc_breadth_positive_1h_pct"] <= 40)
        & (sub["mc_breadth_positive_4h_pct"] <= 45)
        & (sub["mc_btc_return_1h_pct"] < 0)
    ].copy()


def bear_transition_rule(sub: pd.DataFrame) -> pd.DataFrame:
    return sub[
        (sub["mc_breadth_change_5m_pp"] <= -5)
        & (sub["mc_mean_return_10m_pct"] < 0)
        & (sub["mc_btc_return_15m_pct"] < 0)
    ].copy()


def choose_one(sub: pd.DataFrame, col: str, mode: str) -> pd.DataFrame:
    if sub.empty:
        return sub
    grouped = sub.groupby("signal_ms")[col]
    idx = grouped.idxmin() if mode == "min" else grouped.idxmax()
    return sub.loc[idx].copy()


def choose_weakness3(sub: pd.DataFrame) -> pd.DataFrame:
    if sub.empty:
        return sub
    temp = sub.copy()
    temp["weakness3"] = (
        temp["cs_rank_relative_strength_1h"]
        + temp["cs_rank_return_5m"]
        + temp["cs_rank_accel_5m"]
    )
    return temp.loc[temp.groupby("signal_ms")["weakness3"].idxmax()].copy()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("--output", default="v40_step5_bear_short_feasibility_result.json")
    args = ap.parse_args()

    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    df = load_rows(dataset)
    moments = aggregate_moments(df)
    start = int(dataset["period"]["signal_start_ms"])
    untouched_start = start + 75 * DAY_MS

    fit = moments[(moments.signal_ms >= start) & (moments.signal_ms < start + 45 * DAY_MS)].copy()
    cal = moments[(moments.signal_ms >= start + 46 * DAY_MS) & (moments.signal_ms < start + 59 * DAY_MS)].copy()
    val = moments[(moments.signal_ms >= start + 60 * DAY_MS) & (moments.signal_ms < start + 74 * DAY_MS)].copy()
    assert int(val.signal_ms.max()) < untouched_start

    mc_features = [c for c in moments.columns if c.startswith("mc_")] + ["candidate_count"]
    xfit = fit[mc_features].replace([np.inf, -np.inf], np.nan)
    med = xfit.median(numeric_only=True)
    moment_model = HistGradientBoostingRegressor(
        max_iter=100,
        learning_rate=0.05,
        max_leaf_nodes=7,
        min_samples_leaf=40,
        l2_regularization=1.0,
        random_state=42,
    )
    moment_model.fit(xfit.fillna(med), fit["s"])
    for sub in (cal, val):
        sub["pred_short"] = moment_model.predict(
            sub[mc_features].replace([np.inf, -np.inf], np.nan).fillna(med)
        )
    threshold = float(np.quantile(cal["pred_short"], 0.80))
    cal_sel = cal[cal.pred_short >= threshold].copy()
    val_sel = val[val.pred_short >= threshold].copy()

    df_fit = df[(df.signal_ms >= start) & (df.signal_ms < start + 45 * DAY_MS)].copy()
    df_cal = df[(df.signal_ms >= start + 46 * DAY_MS) & (df.signal_ms < start + 59 * DAY_MS)].copy()
    df_val = df[(df.signal_ms >= start + 60 * DAY_MS) & (df.signal_ms < start + 74 * DAY_MS)].copy()
    exclude = {"market", "signal_ms", "route", "s", *{"s" + h for h in HORIZONS}}
    features = [c for c in df.columns if c not in exclude and pd.api.types.is_numeric_dtype(df[c])]
    routes = sorted(df.route.dropna().unique())

    def xmake(sub: pd.DataFrame) -> pd.DataFrame:
        x = sub[features].copy()
        for route in routes:
            x["route_" + route] = (sub.route == route).astype(int).values
        return x.replace([np.inf, -np.inf], np.nan)

    xc = xmake(df_fit)
    medc = xc.median(numeric_only=True)
    candidate_model = HistGradientBoostingRegressor(
        max_iter=100,
        learning_rate=0.05,
        max_leaf_nodes=7,
        min_samples_leaf=40,
        l2_regularization=1.0,
        random_state=43,
    )
    candidate_model.fit(xc.fillna(medc), df_fit["s"])
    for sub in (df_cal, df_val):
        sub["pred_short"] = candidate_model.predict(xmake(sub).fillna(medc))
    cal_best = df_cal.loc[df_cal.groupby("signal_ms")["pred_short"].idxmax()].copy()
    val_best = df_val.loc[df_val.groupby("signal_ms")["pred_short"].idxmax()].copy()
    candidate_threshold = float(np.quantile(cal_best["pred_short"], 0.80))
    cal_combined = cal_best[cal_best.pred_short >= candidate_threshold].copy()
    val_combined = val_best[val_best.pred_short >= candidate_threshold].copy()

    oracle_best = df_val.loc[df_val.groupby("signal_ms")["s"].idxmax()].copy()
    bear_moments = set(bear_rule(val).signal_ms)
    bear_rows = df_val[df_val.signal_ms.isin(bear_moments)].copy()
    oracle_bear = bear_rows.loc[bear_rows.groupby("signal_ms")["s"].idxmax()].copy()

    dev60 = moments[(moments.signal_ms >= start) & (moments.signal_ms < start + 60 * DAY_MS)].copy()
    heuristics = {
        "weakest_relative_strength": ("relative_strength_vs_btc_1h_pct", "min"),
        "lowest_return_5m": ("ef_return_5m_pct", "min"),
        "lowest_return_10m": ("ef_return_10m_pct", "min"),
        "lowest_return_60m": ("ef_return_60m_pct", "min"),
        "lowest_accel_5m": ("ef_momentum_accel_5m_vs_15m", "min"),
        "lowest_base_score": ("base_score", "min"),
    }
    ranker_results = {"fit": {}, "calibration": {}, "validation": {}}
    for split_name, sub in (("fit", df_fit), ("calibration", df_cal), ("validation", df_val)):
        br = bear_rule(sub)
        for name, (col, mode) in heuristics.items():
            ranker_results[split_name][name] = metrics(choose_one(br, col, mode))
        ranker_results[split_name]["weakness3"] = metrics(choose_weakness3(br))

    result = {
        "version": "v40-step5-bear-short-feasibility-1",
        "mode": "OFFLINE_MEASUREMENT_ONLY",
        "execution_enabled": False,
        "live_orders_possible": False,
        "active_paper_changed": False,
        "untouched_opened": False,
        "sampling_limitation": "Master dataset contains only timestamps/markets where the existing LONG candidate generator produced a candidate; this is a short-on-long-candidate feasibility test, not a complete marketwide short test.",
        "cost_model": {"roundtrip_cost_pct": ROUNDTRIP_COST_PCT, "short_net_formula": "-gross_long_close_pct - 0.78"},
        "dataset": {"candidates": int(len(df)), "moments": int(len(moments)), "markets_completed": int(dataset["markets_completed"])},
        "split": {"fit_moments": int(len(fit)), "calibration_moments": int(len(cal)), "validation_moments": int(len(val)), "purge_minutes": 1440, "untouched_start_ms": int(untouched_start), "untouched_opened": False},
        "pass_criteria": PASS,
        "baseline_short_all_long_candidate_moments": metrics(val),
        "market_context_short_model": {
            "type": "HistGradientBoostingRegressor",
            "gate": "top 20% of calibration predicted short moment quality",
            "threshold": threshold,
            "calibration": metrics(cal_sel),
            "validation": metrics(val_sel),
            "validation_prediction_correlation": float(np.corrcoef(val.pred_short, val.s)[0, 1]),
        },
        "interpretable_bear_rule": {
            "rule": "breadth_positive_1h<=40 AND breadth_positive_4h<=45 AND btc_return_1h<0",
            "development": metrics(bear_rule(dev60)),
            "validation": metrics(bear_rule(val)),
        },
        "bear_transition_rule": {
            "rule": "breadth_change_5m_pp<=-5 AND mean_return_10m<0 AND btc_return_15m<0",
            "development": metrics(bear_transition_rule(dev60)),
            "validation": metrics(bear_transition_rule(val)),
        },
        "combined_candidate_model": {
            "type": "HistGradientBoostingRegressor",
            "gate": "choose highest predicted short candidate each moment, then top 20% calibration predicted score",
            "threshold": candidate_threshold,
            "calibration": metrics(cal_combined),
            "validation": metrics(val_combined),
            "validation_candidate_correlation": float(np.corrcoef(df_val.pred_short, df_val.s)[0, 1]),
        },
        "diagnostic_oracle_future_information_only": {
            "note": "Uses forward outcomes only to measure headroom; not tradable.",
            "best_short_candidate_every_validation_moment": metrics(oracle_best),
            "best_short_candidate_within_interpretable_bear_moments": metrics(oracle_bear),
        },
        "simple_point_in_time_rankers_within_bear_rule": {
            "note": "Heuristics select one candidate per bear-rule moment. None is stable across fit/calibration/validation.",
            **ranker_results,
        },
        "decision": "DO_NOT_APPROVE_SHORT_FROM_LONG_SAMPLED_DATA",
        "next": "Build a dedicated marketwide bear/short dataset independent of the long candidate generator, then repeat timing + ranking with the final 15 days sealed.",
    }
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"decision": result["decision"], "untouched_opened": False}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
