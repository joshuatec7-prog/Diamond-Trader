#!/usr/bin/env python3
"""Step 12A-1: freeze the already-validated SHORT/RANGE selectors for prospective shadow use.

OFFLINE RESEARCH ONLY. No runtime/PAPER/live changes.

The models are not retuned here. They are rebuilt from the exact historical
training slice and frozen thresholds already used in Steps 5/6:
- fit days [0,45)
- same HistGradientBoostingRegressor capacity/seeds
- SHORT threshold from Step 5 frozen result
- RANGE threshold from Step 6 frozen result
- no day60+, validation, final15 or future shadow data used for fitting

Output is a lightweight pickle bundle plus a JSON manifest. The bundle is only
for prospective observe-only shadow scoring.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pickle
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from research.v40_step9_candidate_ranking import DAY_MS, _load_bear, _load_range, _features

HGB = {
    "max_iter": 100,
    "learning_rate": 0.05,
    "max_leaf_nodes": 7,
    "min_samples_leaf": 40,
    "l2_regularization": 1.0,
}


def _fit(
    df: pd.DataFrame,
    start: int,
    features: list[str],
    *,
    seed: int,
) -> tuple[HistGradientBoostingRegressor, dict[str, float]]:
    fit = df[
        (df.signal_ms >= start)
        & (df.signal_ms < start + 45 * DAY_MS)
    ].copy()
    if fit.empty:
        raise RuntimeError("fit slice is leeg")
    x = fit[features].replace([np.inf, -np.inf], np.nan)
    med = x.median(numeric_only=True)
    model = HistGradientBoostingRegressor(random_state=seed, **HGB)
    model.fit(x.fillna(med), fit.outcome)
    return model, {str(k): float(v) for k, v in med.to_dict().items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bear_dataset")
    ap.add_argument("range_dataset")
    ap.add_argument("--short-result", default="research/v40_step5_bear_untouched_once_result.json")
    ap.add_argument("--range-result", default="research/v40_step6_range_dataset_analysis_result.json")
    ap.add_argument("--bundle", default="v40_step12a_selector_bundle.pkl")
    ap.add_argument("--manifest", default="v40_step12a_selector_bundle_manifest.json")
    args = ap.parse_args()

    short_frozen = json.loads(Path(args.short_result).read_text(encoding="utf-8"))
    range_frozen = json.loads(Path(args.range_result).read_text(encoding="utf-8"))

    bear, bstart, _ = _load_bear(args.bear_dataset)
    rang, rstart, _ = _load_range(args.range_dataset)
    if bstart != rstart:
        raise RuntimeError("bear/range period start verschilt")
    start = int(bstart)

    short_features = [str(x) for x in short_frozen["model"]["features"]]
    missing_short = [x for x in short_features if x not in bear.columns]
    if missing_short:
        raise RuntimeError(f"SHORT features ontbreken: {missing_short}")

    range_features = _features(rang)
    if "side_num" not in range_features:
        raise RuntimeError("RANGE side_num ontbreekt uit frozen features")

    short_model, short_medians = _fit(bear, start, short_features, seed=52)
    range_model, range_medians = _fit(rang, start, range_features, seed=62)

    bundle: dict[str, Any] = {
        "version": "v40-step12a-selector-bundle-1",
        "mode": "OBSERVE_ONLY_MODEL_BUNDLE",
        "training_cutoff_day_exclusive": 45,
        "days_60_plus_used": False,
        "validation_reused_for_fit": False,
        "final15_reused_for_fit": False,
        "SHORT": {
            "model": short_model,
            "features": short_features,
            "medians": short_medians,
            "threshold": float(short_frozen["model"]["candidate_threshold"]),
            "seed": 52,
        },
        "RANGE": {
            "model": range_model,
            "features": range_features,
            "medians": range_medians,
            "threshold": float(range_frozen["candidate_selector"]["threshold"]),
            "seed": 62,
        },
        "arbiter": {
            "priority": ["SHORT", "RANGE_SHORT_AS_RANGE", "NO_TRADE"],
            "LONG": "DISABLED",
            "RANGE_LONG": "NO_TRADE",
        },
        "exit_policy": {"take_profit_pct": 10.0, "stop_pct": 15.0},
        "shadow_risk": {
            "baseline_stake_eur": 300.0,
            "max_open": 4,
            "stake_ceiling_eur": 400.0,
            "max_open_at_ceiling": 3,
            "variable_strength_sizing_validated": False,
        },
        "execution_enabled": False,
        "live_orders_possible": False,
        "active_paper_changed": False,
    }

    raw = pickle.dumps(bundle, protocol=pickle.HIGHEST_PROTOCOL)
    Path(args.bundle).write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()

    manifest = {
        "version": bundle["version"],
        "mode": bundle["mode"],
        "bundle_sha256": digest,
        "bundle_bytes": len(raw),
        "training_cutoff_day_exclusive": 45,
        "days_60_plus_used": False,
        "validation_reused_for_fit": False,
        "final15_reused_for_fit": False,
        "SHORT": {
            "feature_count": len(short_features),
            "threshold": bundle["SHORT"]["threshold"],
            "seed": 52,
        },
        "RANGE": {
            "feature_count": len(range_features),
            "threshold": bundle["RANGE"]["threshold"],
            "seed": 62,
        },
        "arbiter": bundle["arbiter"],
        "exit_policy": bundle["exit_policy"],
        "shadow_risk": bundle["shadow_risk"],
        "execution_enabled": False,
        "live_orders_possible": False,
        "active_paper_changed": False,
        "decision": "SELECTOR_BUNDLE_READY_FOR_PROSPECTIVE_SHADOW",
    }
    Path(args.manifest).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
