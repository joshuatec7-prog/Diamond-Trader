#!/usr/bin/env python3
"""Step 9: improve within-moment candidate ranking without reusing opened OOS windows.

OFFLINE RESEARCH ONLY. No runtime/PAPER/live changes.

Question:
Can one fixed relative-target ranker improve the coin choice inside moments that
already produced candidates, compared with the frozen absolute-outcome HGB
ranking used in Steps 5/6?

Scientific guardrails:
- No hyperparameter sweep.
- Same HGB family/capacity as the frozen selectors.
- Challenger target is training-only within-moment residual outcome:
      outcome - mean(outcome of candidates at the same signal_ms)
- Two forward development tests only:
      A train days 0-30, purge day 30, test days 31-45
      B train days 0-45, purge day 45, test days 46-59
- Days >= 60 are never loaded into a fit/test slice here. That avoids reusing the
  Step 5/6 validation and already-opened final15 as evidence for this challenger.
- Results are challenger evidence only; any promoted challenger still requires
  genuinely new/future OOS before PAPER/runtime use.
"""
from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

DAY_MS = 86_400_000
SHORT_HORIZONS = ("240", "480", "720", "1440")
RANGE_HORIZONS = ("60", "240", "480", "720")

HGB = {
    "max_iter": 100,
    "learning_rate": 0.05,
    "max_leaf_nodes": 7,
    "min_samples_leaf": 40,
    "l2_regularization": 1.0,
}

PASS = {
    "mean_uplift_pp_min_each_window": 0.25,
    "pf_must_exceed_baseline_each_window": True,
    "realized_percentile_must_exceed_baseline_each_window": True,
    "oracle_hit_must_not_decline_each_window": True,
}

WINDOWS = (
    {"name": "A", "train_start_day": 0, "train_end_day": 30, "test_start_day": 31, "test_end_day": 45},
    {"name": "B", "train_start_day": 0, "train_end_day": 45, "test_start_day": 46, "test_end_day": 59},
)


def _pf(values: pd.Series) -> float | None:
    arr = values.to_numpy(dtype=float)
    pos = arr[arr > 0].sum()
    neg = -arr[arr < 0].sum()
    return float(pos / neg) if neg > 0 else None


def _load_bear(path: str) -> tuple[pd.DataFrame, int, int]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    start = int(data["period"]["signal_start_ms"])
    end = int(data["period"]["signal_end_ms_exclusive"])
    rows: list[dict[str, Any]] = []
    for src in data["rows"]:
        rec: dict[str, Any] = {
            "market": src["market"],
            "signal_ms": int(src["signal_ms"]),
            "relative_weakness_vs_btc_1h_pct": src.get("relative_weakness_vs_btc_1h_pct"),
        }
        for k, v in (src.get("entry_features") or {}).items():
            rec["ef_" + k] = v
        for k, v in (src.get("market_context") or {}).items():
            rec["mc_" + k] = v
        for k, v in (src.get("cross_section") or {}).items():
            rec["cs_" + k] = v
        vals = []
        for h in SHORT_HORIZONS:
            val = float(src["forward_short_labels"][h]["net_short_pct"])
            rec["y" + h] = val
            vals.append(val)
        rec["outcome"] = float(np.mean(vals))
        rows.append(rec)
    del data
    gc.collect()
    return pd.DataFrame(rows), start, end


def _load_range(path: str) -> tuple[pd.DataFrame, int, int]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    start = int(data["period"]["signal_start_ms"])
    end = int(data["period"]["signal_end_ms_exclusive"])
    rows: list[dict[str, Any]] = []
    for src in data["rows"]:
        side = str(src["side"])
        rec: dict[str, Any] = {
            "market": src["market"],
            "signal_ms": int(src["signal_ms"]),
            "side": side,
            "side_num": 1.0 if side == "RANGE_LONG" else -1.0,
        }
        for k, v in (src.get("entry_features") or {}).items():
            if k != "side":
                rec["ef_" + k] = v
        for k, v in (src.get("market_context") or {}).items():
            rec["mc_" + k] = v
        for k, v in (src.get("cross_section") or {}).items():
            rec["cs_" + k] = v
        vals = []
        for h in RANGE_HORIZONS:
            val = float(src["forward_range_labels"][h]["net_range_pct"])
            rec["y" + h] = val
            vals.append(val)
        rec["outcome"] = float(np.mean(vals))
        rows.append(rec)
    del data
    gc.collect()
    return pd.DataFrame(rows), start, end


def _features(df: pd.DataFrame) -> list[str]:
    excluded = {"market", "signal_ms", "side", "outcome"}
    excluded.update(c for c in df.columns if c.startswith("y"))
    return [c for c in df.columns if c not in excluded and pd.api.types.is_numeric_dtype(df[c])]


def _make_model(seed: int) -> HistGradientBoostingRegressor:
    return HistGradientBoostingRegressor(random_state=seed, **HGB)


def _selected_metrics(test: pd.DataFrame, score_col: str) -> tuple[dict[str, Any], pd.DataFrame]:
    work = test.copy()
    idx = work.groupby("signal_ms")[score_col].idxmax()
    chosen = work.loc[idx].copy().sort_values("signal_ms").reset_index(drop=True)

    # Ranking diagnostics use only labels for evaluation, never as model inputs.
    group_size = work.groupby("signal_ms")["outcome"].transform("size")
    rank_asc = work.groupby("signal_ms")["outcome"].rank(method="average", ascending=True)
    work["realized_percentile"] = np.where(group_size > 1, (rank_asc - 1.0) / (group_size - 1.0) * 100.0, 100.0)
    oracle_idx = work.groupby("signal_ms")["outcome"].idxmax()
    oracle = work.loc[oracle_idx, ["signal_ms", "market", "outcome"]].rename(
        columns={"market": "oracle_market", "outcome": "oracle_outcome"}
    )
    chosen = chosen.merge(
        work[["signal_ms", "market", "realized_percentile"]],
        on=["signal_ms", "market"], how="left"
    ).merge(oracle, on="signal_ms", how="left")
    chosen["oracle_hit"] = chosen["market"] == chosen["oracle_market"]
    chosen["gap_to_oracle_pct"] = chosen["oracle_outcome"] - chosen["outcome"]

    vals = chosen.outcome.to_numpy(dtype=float)
    metrics: dict[str, Any] = {
        "n_moments": int(len(chosen)),
        "mean_pct": float(vals.mean()) if len(vals) else None,
        "median_pct": float(np.median(vals)) if len(vals) else None,
        "pf": _pf(chosen.outcome) if len(chosen) else None,
        "win_pct": float((vals > 0).mean() * 100.0) if len(vals) else None,
        "oracle_hit_pct": float(chosen.oracle_hit.mean() * 100.0) if len(chosen) else None,
        "mean_realized_percentile": float(chosen.realized_percentile.mean()) if len(chosen) else None,
        "mean_gap_to_oracle_pct": float(chosen.gap_to_oracle_pct.mean()) if len(chosen) else None,
        "max_market_share_pct": float(chosen.market.value_counts(normalize=True).max() * 100.0) if len(chosen) else None,
    }
    if "side" in chosen.columns:
        metrics["range_long_share_pct"] = float((chosen.side == "RANGE_LONG").mean() * 100.0)
    return metrics, chosen


def _one_window(df: pd.DataFrame, start: int, spec: dict[str, Any], seed: int) -> dict[str, Any]:
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
    assert int(test.signal_ms.max()) < start + 60 * DAY_MS

    features = _features(df)
    xtrain = train[features].replace([np.inf, -np.inf], np.nan)
    med = xtrain.median(numeric_only=True)

    # Frozen-style baseline: predict absolute forward outcome and take max per moment.
    baseline = _make_model(seed)
    baseline.fit(xtrain.fillna(med), train.outcome)
    test["baseline_score"] = baseline.predict(test[features].replace([np.inf, -np.inf], np.nan).fillna(med))

    # Fixed challenger: remove the common moment component from the TRAINING target.
    # No future moment information is needed at inference time.
    moment_mean = train.groupby("signal_ms")["outcome"].transform("mean")
    residual_target = train.outcome - moment_mean
    challenger = _make_model(seed + 1000)
    challenger.fit(xtrain.fillna(med), residual_target)
    test["challenger_score"] = challenger.predict(test[features].replace([np.inf, -np.inf], np.nan).fillna(med))

    bm, bsel = _selected_metrics(test, "baseline_score")
    cm, csel = _selected_metrics(test, "challenger_score")
    comparison = {
        "mean_uplift_pp": float(cm["mean_pct"] - bm["mean_pct"]),
        "pf_uplift": None if bm["pf"] is None or cm["pf"] is None else float(cm["pf"] - bm["pf"]),
        "oracle_hit_uplift_pp": float(cm["oracle_hit_pct"] - bm["oracle_hit_pct"]),
        "realized_percentile_uplift_pp": float(cm["mean_realized_percentile"] - bm["mean_realized_percentile"]),
        "gap_to_oracle_improvement_pp": float(bm["mean_gap_to_oracle_pct"] - cm["mean_gap_to_oracle_pct"]),
        "same_choice_pct": float(
            bsel[["signal_ms", "market"]].merge(csel[["signal_ms", "market"]], on=["signal_ms", "market"]).shape[0]
            / max(1, len(bsel)) * 100.0
        ),
    }
    return {
        "window": spec,
        "train_rows": int(len(train)),
        "test_rows": int(len(test)),
        "train_moments": int(train.signal_ms.nunique()),
        "test_moments": int(test.signal_ms.nunique()),
        "feature_count": int(len(features)),
        "baseline": bm,
        "challenger": cm,
        "comparison": comparison,
    }


def _passes_window(w: dict[str, Any]) -> bool:
    b = w["baseline"]
    c = w["challenger"]
    d = w["comparison"]
    return (
        d["mean_uplift_pp"] >= PASS["mean_uplift_pp_min_each_window"]
        and b["pf"] is not None and c["pf"] is not None and c["pf"] > b["pf"]
        and c["mean_realized_percentile"] > b["mean_realized_percentile"]
        and c["oracle_hit_pct"] >= b["oracle_hit_pct"]
    )


def _evaluate_state(name: str, df: pd.DataFrame, start: int, dataset_end: int, seed: int) -> dict[str, Any]:
    # Hard guard: Step 9 is not allowed to use day 60+ rows in any model comparison.
    dev = df[df.signal_ms < start + 60 * DAY_MS].copy()
    assert int(dev.signal_ms.max()) < start + 60 * DAY_MS
    windows = [_one_window(dev, start, spec, seed + i * 10) for i, spec in enumerate(WINDOWS)]
    passed_each = [_passes_window(w) for w in windows]
    return {
        "state": name,
        "dataset_rows_total": int(len(df)),
        "dataset_moments_total": int(df.signal_ms.nunique()),
        "development_rows_used": int(len(dev)),
        "development_moments_used": int(dev.signal_ms.nunique()),
        "dataset_end_ms": int(dataset_end),
        "days_60_plus_used": False,
        "method": "same fixed HGB capacity; challenger predicts training within-moment residual instead of absolute outcome",
        "windows": windows,
        "passed_each_window": passed_each,
        "passed": bool(all(passed_each)),
        "decision": "PROMOTE_RELATIVE_RANKER_TO_FUTURE_OOS_CHALLENGER" if all(passed_each) else "KEEP_FROZEN_BASELINE_RANKER",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bear_dataset")
    ap.add_argument("range_dataset")
    ap.add_argument("--output", default="v40_step9_candidate_ranking_result.json")
    args = ap.parse_args()

    bear, bstart, bend = _load_bear(args.bear_dataset)
    short_result = _evaluate_state("SHORT", bear, bstart, bend, seed=52)
    del bear
    gc.collect()

    rang, rstart, rend = _load_range(args.range_dataset)
    range_result = _evaluate_state("RANGE", rang, rstart, rend, seed=62)
    del rang
    gc.collect()

    assert bstart == rstart, "datasets hebben verschillende signal_start_ms"

    any_promoted = bool(short_result["passed"] or range_result["passed"])
    result = {
        "version": "v40-step9-candidate-ranking-1",
        "mode": "OFFLINE_MEASUREMENT_ONLY",
        "execution_enabled": False,
        "live_orders_possible": False,
        "active_paper_changed": False,
        "hyperparameter_sweep": False,
        "step5_step6_validation_reused": False,
        "opened_final15_reused": False,
        "development_cutoff_day": 60,
        "pass_criteria": PASS,
        "hgb": HGB,
        "SHORT": short_result,
        "RANGE": range_result,
        "decision": "KEEP_ONE_OR_MORE_RELATIVE_RANKERS_FOR_FUTURE_OOS" if any_promoted else "KEEP_EXISTING_FROZEN_RANKERS",
        "notes": [
            "Een PROMOTE-beslissing betekent alleen challenger-status voor nieuw/future OOS; geen PAPER/runtime-wijziging.",
            "Geen data vanaf dag 60 gebruikt; bekende Step 5/6 validation en final15 zijn niet opnieuw ingezet om de challenger te kiezen.",
            "Step 10 blijft verantwoordelijk voor kosten/spread/liquiditeit/L2-veto; die factoren worden hier niet opnieuw getuned.",
        ],
    }
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")

    compact = {
        "decision": result["decision"],
        "SHORT": {
            "decision": short_result["decision"],
            "window_A_uplift_pp": short_result["windows"][0]["comparison"]["mean_uplift_pp"],
            "window_B_uplift_pp": short_result["windows"][1]["comparison"]["mean_uplift_pp"],
        },
        "RANGE": {
            "decision": range_result["decision"],
            "window_A_uplift_pp": range_result["windows"][0]["comparison"]["mean_uplift_pp"],
            "window_B_uplift_pp": range_result["windows"][1]["comparison"]["mean_uplift_pp"],
        },
        "opened_final15_reused": False,
    }
    print(json.dumps(compact, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
