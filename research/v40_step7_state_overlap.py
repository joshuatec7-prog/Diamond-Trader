#!/usr/bin/env python3
"""Step 7: combine proven research selectors into LONG/SHORT/RANGE/NO_TRADE diagnostics.

OFFLINE RESEARCH ONLY. No runtime/PAPER/live changes.
Important: Step 5 and 6 untouched windows have already been opened separately,
so this combined-arbiter analysis is diagnostic, not a new untouched test.
"""
from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

DAY_MS = 86_400_000


def _pf(values: pd.Series) -> float | None:
    arr = values.to_numpy(dtype=float)
    pos = arr[arr > 0].sum()
    neg = -arr[arr < 0].sum()
    return float(pos / neg) if neg > 0 else None


def _summary(df: pd.DataFrame, target: str) -> dict:
    if df.empty:
        return {"n": 0, "mean_pct": None, "pf": None, "win_pct": None}
    return {
        "n": int(len(df)),
        "mean_pct": float(df[target].mean()),
        "pf": _pf(df[target]),
        "win_pct": float((df[target] > 0).mean() * 100.0),
    }


def _load_bear_selected(path: str, frozen_result: dict) -> pd.DataFrame:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = []
    horizons = ("240", "480", "720", "1440")
    for src in data["rows"]:
        rec = {
            "market": src["market"],
            "signal_ms": int(src["signal_ms"]),
            "relative_weakness_vs_btc_1h_pct": src.get("relative_weakness_vs_btc_1h_pct"),
        }
        for k, v in (src.get("entry_features") or {}).items(): rec["ef_" + k] = v
        for k, v in (src.get("market_context") or {}).items(): rec["mc_" + k] = v
        for k, v in (src.get("cross_section") or {}).items(): rec["cs_" + k] = v
        for h in horizons: rec["s" + h] = float(src["forward_short_labels"][h]["net_short_pct"])
        rec["outcome"] = float(np.mean([rec["s" + h] for h in horizons]))
        rows.append(rec)
    df = pd.DataFrame(rows)
    start = int(data["period"]["signal_start_ms"])
    fit = df[(df.signal_ms >= start) & (df.signal_ms < start + 45 * DAY_MS)].copy()
    eval_df = df[(df.signal_ms >= start + 60 * DAY_MS) & (df.signal_ms < int(data["period"]["signal_end_ms_exclusive"]))].copy()
    features = list(frozen_result["model"]["features"])
    xfit = fit[features].replace([np.inf, -np.inf], np.nan)
    med = xfit.median(numeric_only=True)
    model = HistGradientBoostingRegressor(max_iter=100, learning_rate=0.05, max_leaf_nodes=7,
        min_samples_leaf=40, l2_regularization=1.0, random_state=52)
    model.fit(xfit.fillna(med), fit.outcome)
    eval_df["pred"] = model.predict(eval_df[features].replace([np.inf, -np.inf], np.nan).fillna(med))
    best = eval_df.loc[eval_df.groupby("signal_ms")["pred"].idxmax()].copy()
    selected = best[best.pred >= float(frozen_result["model"]["candidate_threshold"])].copy()
    selected = selected[["signal_ms", "market", "pred", "outcome"]]
    selected["source"] = "SHORT"
    selected["state_side"] = "SHORT"
    del data, df, fit, eval_df, best
    gc.collect()
    return selected


def _load_range_selected(path: str, frozen_validation: dict) -> pd.DataFrame:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = []
    horizons = ("60", "240", "480", "720")
    for src in data["rows"]:
        rec = {"market": src["market"], "signal_ms": int(src["signal_ms"]), "side": src["side"],
               "side_num": 1.0 if src["side"] == "RANGE_LONG" else -1.0}
        for k, v in (src.get("entry_features") or {}).items():
            if k != "side": rec["ef_" + k] = v
        for k, v in (src.get("market_context") or {}).items(): rec["mc_" + k] = v
        for k, v in (src.get("cross_section") or {}).items(): rec["cs_" + k] = v
        for h in horizons: rec["r" + h] = float(src["forward_range_labels"][h]["net_range_pct"])
        rec["outcome"] = float(np.mean([rec["r" + h] for h in horizons]))
        rows.append(rec)
    df = pd.DataFrame(rows)
    start = int(data["period"]["signal_start_ms"])
    fit = df[(df.signal_ms >= start) & (df.signal_ms < start + 45 * DAY_MS)].copy()
    eval_df = df[(df.signal_ms >= start + 60 * DAY_MS) & (df.signal_ms < int(data["period"]["signal_end_ms_exclusive"]))].copy()
    excluded = {"market", "signal_ms", "side", "outcome", *{"r" + h for h in horizons}}
    features = [c for c in df.columns if c not in excluded and pd.api.types.is_numeric_dtype(df[c])]
    xfit = fit[features].replace([np.inf, -np.inf], np.nan)
    med = xfit.median(numeric_only=True)
    model = HistGradientBoostingRegressor(max_iter=100, learning_rate=0.05, max_leaf_nodes=7,
        min_samples_leaf=40, l2_regularization=1.0, random_state=62)
    model.fit(xfit.fillna(med), fit.outcome)
    eval_df["pred"] = model.predict(eval_df[features].replace([np.inf, -np.inf], np.nan).fillna(med))
    best = eval_df.loc[eval_df.groupby("signal_ms")["pred"].idxmax()].copy()
    threshold = float(frozen_validation["candidate_selector"]["threshold"])
    selected = best[best.pred >= threshold].copy()
    selected = selected[["signal_ms", "market", "side", "pred", "outcome"]]
    selected["source"] = "RANGE"
    selected["state_side"] = selected["side"]
    del data, df, fit, eval_df, best
    gc.collect()
    return selected


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bear_dataset")
    ap.add_argument("range_dataset")
    ap.add_argument("--short-result", default="research/v40_step5_bear_untouched_once_result.json")
    ap.add_argument("--range-result", default="research/v40_step6_range_dataset_analysis_result.json")
    ap.add_argument("--output", default="v40_step7_state_overlap_result.json")
    args = ap.parse_args()

    short_frozen = json.loads(Path(args.short_result).read_text(encoding="utf-8"))
    range_frozen = json.loads(Path(args.range_result).read_text(encoding="utf-8"))
    short = _load_bear_selected(args.bear_dataset, short_frozen)
    rang = _load_range_selected(args.range_dataset, range_frozen)

    short_times = set(short.signal_ms.tolist())
    range_times = set(rang.signal_ms.tolist())
    overlap_times = short_times & range_times
    exact = short.merge(rang, on=["signal_ms", "market"], suffixes=("_short", "_range"))
    conflict = exact[exact.state_side_range == "RANGE_LONG"].copy()

    short_only = short[~short.signal_ms.isin(range_times)].copy()
    range_only = rang[~rang.signal_ms.isin(short_times)].copy()
    both_short = short[short.signal_ms.isin(overlap_times)].copy()
    both_range = rang[rang.signal_ms.isin(overlap_times)].copy()

    # Frozen top-level interpretation for Step 7 diagnostics:
    # LONG stays disabled (Step 4 failed). RANGE_LONG is not promoted.
    # If proven SHORT fires -> SHORT. Otherwise proven RANGE_SHORT -> RANGE.
    # Everything else -> NO_TRADE.
    states = []
    for ts in sorted(short_times | range_times):
        s = short[short.signal_ms == ts]
        r = rang[rang.signal_ms == ts]
        if not s.empty:
            row = s.iloc[0]
            states.append({"signal_ms": int(ts), "state": "SHORT", "market": row.market, "outcome": float(row.outcome)})
        elif not r.empty and r.iloc[0].state_side == "RANGE_SHORT":
            row = r.iloc[0]
            states.append({"signal_ms": int(ts), "state": "RANGE", "market": row.market, "outcome": float(row.outcome)})
        else:
            states.append({"signal_ms": int(ts), "state": "NO_TRADE", "market": None, "outcome": None})
    state_df = pd.DataFrame(states)

    def state_summary(name: str) -> dict:
        sub = state_df[state_df.state == name]
        if name == "NO_TRADE" or sub.empty:
            return {"n": int(len(sub)), "mean_pct": None, "pf": None, "win_pct": None}
        return _summary(sub, "outcome")

    result = {
        "version": "v40-step7-state-overlap-1",
        "mode": "OFFLINE_MEASUREMENT_ONLY",
        "execution_enabled": False,
        "live_orders_possible": False,
        "active_paper_changed": False,
        "new_untouched_test": False,
        "warning": "Step 5/6 untouched windows were already opened separately; this combined analysis is diagnostic and needs later shadow/PAPER validation.",
        "long_state": {"enabled": False, "reason": "Step 4 long momentgate failed validation"},
        "selected_counts": {"short": int(len(short)), "range": int(len(rang)), "short_unique_moments": len(short_times), "range_unique_moments": len(range_times)},
        "overlap": {
            "same_moment_count": int(len(overlap_times)),
            "same_moment_pct_of_short": float(len(overlap_times) / len(short_times) * 100.0) if short_times else None,
            "same_moment_pct_of_range": float(len(overlap_times) / len(range_times) * 100.0) if range_times else None,
            "exact_same_market_count": int(len(exact)),
            "exact_range_long_conflicts": int(len(conflict)),
            "range_long_share_pct": float((rang.state_side == "RANGE_LONG").mean() * 100.0) if len(rang) else None,
        },
        "performance": {
            "short_all": _summary(short, "outcome"),
            "range_all": _summary(rang, "outcome"),
            "short_only_moments": _summary(short_only, "outcome"),
            "range_only_moments": _summary(range_only, "outcome"),
            "short_on_overlap_moments": _summary(both_short, "outcome"),
            "range_on_overlap_moments": _summary(both_range, "outcome"),
        },
        "frozen_state_arbiter": {
            "priority": ["SHORT", "RANGE_SHORT_AS_RANGE", "NO_TRADE"],
            "LONG": "disabled",
            "RANGE_LONG": "NO_TRADE until separate long/range-long evidence exists",
            "states": {"SHORT": state_summary("SHORT"), "RANGE": state_summary("RANGE"), "NO_TRADE": state_summary("NO_TRADE")},
        },
    }
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({
        "short": result["selected_counts"]["short"],
        "range": result["selected_counts"]["range"],
        "same_moment": result["overlap"]["same_moment_count"],
        "exact_same_market": result["overlap"]["exact_same_market_count"],
        "range_long_share_pct": result["overlap"]["range_long_share_pct"],
        "states": result["frozen_state_arbiter"]["states"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
