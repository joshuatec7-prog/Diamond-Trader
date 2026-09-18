#!/usr/bin/env python3
"""Step 11B: fixed exit/stop/runner comparison on Step 11A development paths.

OFFLINE RESEARCH ONLY. No runtime/PAPER/live changes.

Input is the frozen Step 11A development-only path artifact:
- 480 paths
- 120 per SHORT_A / SHORT_B / RANGE_A / RANGE_B
- no day 60+, Step5/6 validation, or final15

This step compares a SMALL PREDECLARED policy set. It does not sweep/tune
thresholds. Any selected policy is a future shadow candidate only.

Intrabar convention for SHORT positions:
1) hard stop is checked first;
2) fixed take-profit second;
3) trailing stop third;
4) if a bar both creates a new trough and can hit the resulting trail,
   assume the trail is hit in that same bar (conservative).
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

ROUNDTRIP_COST_PCT = 0.78

POLICIES: dict[str, dict[str, float]] = {
    "BASE_TIME": {},
    "STOP10": {"stop_pct": 10.0},
    "STOP15": {"stop_pct": 15.0},
    "STOP20": {"stop_pct": 20.0},
    "TP10_STOP15": {"take_profit_pct": 10.0, "stop_pct": 15.0},
    "TP15_STOP15": {"take_profit_pct": 15.0, "stop_pct": 15.0},
    "RUN10_TRAIL7P5_STOP15": {
        "trail_activation_pct": 10.0,
        "trail_pct": 7.5,
        "stop_pct": 15.0,
    },
    "RUN15_TRAIL10_STOP20": {
        "trail_activation_pct": 15.0,
        "trail_pct": 10.0,
        "stop_pct": 20.0,
    },
}

PASS = {
    "mean_positive_each_window": True,
    "pf_min_each_window": 1.20,
    "worst10_improvement_pct_each_window": 40.0,
    "pooled_mean_fraction_of_baseline_min": 0.25,
}


def _pf(vals: np.ndarray) -> float | None:
    pos = float(vals[vals > 0].sum())
    neg = float(-vals[vals < 0].sum())
    return pos / neg if neg > 0 else None


def _metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    vals = np.asarray([float(x["net_pct"]) for x in results], dtype=float)
    holds = np.asarray([float(x["hold_minutes"]) for x in results], dtype=float)
    worst_n = max(1, int(math.ceil(len(vals) * 0.10)))
    worst = np.sort(vals)[:worst_n]
    reasons: dict[str, int] = {}
    for x in results:
        reasons[str(x["exit_reason"])] = reasons.get(str(x["exit_reason"]), 0) + 1
    return {
        "n": int(len(vals)),
        "mean_net_pct": float(vals.mean()),
        "median_net_pct": float(np.median(vals)),
        "pf": _pf(vals),
        "win_pct": float((vals > 0).mean() * 100.0),
        "p10_net_pct": float(np.quantile(vals, 0.10)),
        "worst10_mean_net_pct": float(worst.mean()),
        "max_loss_net_pct": float(vals.min()),
        "median_hold_minutes": float(np.median(holds)),
        "exit_reasons": reasons,
    }


def _simulate(event: dict[str, Any], policy: dict[str, float]) -> dict[str, Any]:
    entry = float(event["entry_close"])
    path = event["path"]
    stop_pct = policy.get("stop_pct")
    tp_pct = policy.get("take_profit_pct")
    activation_pct = policy.get("trail_activation_pct")
    trail_pct = policy.get("trail_pct")

    trough = entry
    trail_active = False

    for row in path:
        minutes = int(row[0])
        high = float(row[2])
        low = float(row[3])

        if stop_pct is not None:
            stop_px = entry * (1.0 + stop_pct / 100.0)
            if high >= stop_px:
                gross = -float(stop_pct)
                return {
                    "net_pct": gross - ROUNDTRIP_COST_PCT,
                    "gross_pct": gross,
                    "hold_minutes": minutes,
                    "exit_reason": "STOP",
                }

        if tp_pct is not None:
            tp_px = entry * (1.0 - tp_pct / 100.0)
            if low <= tp_px:
                gross = float(tp_pct)
                return {
                    "net_pct": gross - ROUNDTRIP_COST_PCT,
                    "gross_pct": gross,
                    "hold_minutes": minutes,
                    "exit_reason": "TAKE_PROFIT",
                }

        if activation_pct is not None and trail_pct is not None:
            trough = min(trough, low)
            favorable_pct = (entry - trough) / entry * 100.0
            if not trail_active and favorable_pct >= activation_pct:
                trail_active = True
            if trail_active:
                trail_stop_px = trough * (1.0 + trail_pct / 100.0)
                if high >= trail_stop_px:
                    gross = (entry - trail_stop_px) / entry * 100.0
                    return {
                        "net_pct": gross - ROUNDTRIP_COST_PCT,
                        "gross_pct": gross,
                        "hold_minutes": minutes,
                        "exit_reason": "TRAIL",
                    }

    last = path[-1]
    close = float(last[4])
    gross = (entry - close) / entry * 100.0
    return {
        "net_pct": gross - ROUNDTRIP_COST_PCT,
        "gross_pct": gross,
        "hold_minutes": int(last[0]),
        "exit_reason": "TIME",
    }


def _tail_improvement_pct(candidate: float, baseline: float) -> float | None:
    if baseline >= 0:
        return None
    return (candidate - baseline) / abs(baseline) * 100.0


def _evaluate_state(events: list[dict[str, Any]], state: str) -> dict[str, Any]:
    state_events = [e for e in events if e["state"] == state]
    if len(state_events) != 240:
        raise RuntimeError(f"{state} verwacht 240 events, kreeg {len(state_events)}")

    results: dict[str, Any] = {}
    raw_by_policy: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for name, policy in POLICIES.items():
        raw_by_policy[name] = {}
        policy_metrics: dict[str, Any] = {}
        for window in ("A", "B"):
            subset = [e for e in state_events if e["window"] == window]
            if len(subset) != 120:
                raise RuntimeError(f"{state}_{window} verwacht 120 events, kreeg {len(subset)}")
            sims = [_simulate(e, policy) for e in subset]
            raw_by_policy[name][window] = sims
            policy_metrics[window] = _metrics(sims)
        pooled = raw_by_policy[name]["A"] + raw_by_policy[name]["B"]
        policy_metrics["POOLED"] = _metrics(pooled)
        results[name] = policy_metrics

    baseline = results["BASE_TIME"]
    eligible: list[dict[str, Any]] = []
    for name in POLICIES:
        if name == "BASE_TIME":
            continue
        m = results[name]
        window_checks: dict[str, Any] = {}
        passed = True
        min_pf = float("inf")
        min_mean = float("inf")
        for window in ("A", "B"):
            cand = m[window]
            base = baseline[window]
            tail_imp = _tail_improvement_pct(
                float(cand["worst10_mean_net_pct"]),
                float(base["worst10_mean_net_pct"]),
            )
            checks = {
                "mean_positive": float(cand["mean_net_pct"]) > 0.0,
                "pf_at_least_min": cand["pf"] is not None and float(cand["pf"]) >= PASS["pf_min_each_window"],
                "worst10_improvement_pct": tail_imp,
                "worst10_improvement_pass": tail_imp is not None and tail_imp >= PASS["worst10_improvement_pct_each_window"],
            }
            window_checks[window] = checks
            passed = passed and checks["mean_positive"] and checks["pf_at_least_min"] and checks["worst10_improvement_pass"]
            min_pf = min(min_pf, float(cand["pf"] or 0.0))
            min_mean = min(min_mean, float(cand["mean_net_pct"]))

        pooled_base_mean = float(baseline["POOLED"]["mean_net_pct"])
        pooled_cand_mean = float(m["POOLED"]["mean_net_pct"])
        if pooled_base_mean > 0:
            fraction = pooled_cand_mean / pooled_base_mean
            pooled_pass = fraction >= PASS["pooled_mean_fraction_of_baseline_min"]
        else:
            fraction = None
            pooled_pass = pooled_cand_mean > 0
        passed = passed and pooled_pass

        item = {
            "policy": name,
            "passed": bool(passed),
            "window_checks": window_checks,
            "pooled_mean_fraction_of_baseline": fraction,
            "pooled_mean_pass": bool(pooled_pass),
            "min_window_pf": min_pf,
            "min_window_mean_net_pct": min_mean,
            "pooled_pf": m["POOLED"]["pf"],
        }
        if passed:
            eligible.append(item)

    chosen = None
    if eligible:
        eligible.sort(
            key=lambda x: (
                float(x["min_window_pf"]),
                float(x["min_window_mean_net_pct"]),
                float(x["pooled_pf"] or 0.0),
                x["policy"],
            ),
            reverse=True,
        )
        chosen = eligible[0]["policy"]

    return {
        "state": state,
        "baseline": baseline,
        "policies": results,
        "eligibility": eligible,
        "chosen_policy": chosen,
        "decision": (
            "PROMOTE_FIXED_EXIT_CANDIDATE_TO_STEP11C"
            if chosen is not None
            else "NO_EXIT_POLICY_PASSES_FIXED_STEP11B_RULES"
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("step11a_result")
    ap.add_argument("--output", default="v40_step11b_exit_policy_result.json")
    args = ap.parse_args()

    source = json.loads(Path(args.step11a_result).read_text(encoding="utf-8"))
    if source["decision"] != "PATH_DATASET_READY_FOR_STEP11B":
        raise RuntimeError("Step 11A dataset is niet vrijgegeven voor Step 11B")
    if float(source["sample"]["usable_pct"]) < 90.0:
        raise RuntimeError("Step 11A coverage onder 90%")
    if source["days_60_plus_used"] or source["step5_6_validation_reused"] or source["final15_reused"]:
        raise RuntimeError("Step 11A split guard geschonden")
    events = source["events"]
    if len(events) != 480:
        raise RuntimeError(f"verwacht 480 events, kreeg {len(events)}")

    short = _evaluate_state(events, "SHORT")
    rang = _evaluate_state(events, "RANGE")
    both = short["chosen_policy"] is not None and rang["chosen_policy"] is not None

    result = {
        "version": "v40-step11b-exit-policy-1",
        "mode": "OFFLINE_DEVELOPMENT_ONLY",
        "execution_enabled": False,
        "active_paper_changed": False,
        "live_orders_possible": False,
        "source_step11a_decision": source["decision"],
        "source_event_count": len(events),
        "source_usable_pct": source["sample"]["usable_pct"],
        "roundtrip_cost_pct": ROUNDTRIP_COST_PCT,
        "policy_set": POLICIES,
        "pass_rules": PASS,
        "intrabar_rule": "SHORT: hard stop first, fixed TP second, trailing third; newly-created trail may be hit in same bar (conservative).",
        "states": {
            "SHORT": short,
            "RANGE": rang,
        },
        "development_only": True,
        "days_60_plus_used": False,
        "step5_6_validation_reused": False,
        "final15_reused": False,
        "decision": (
            "STEP11B_EXIT_CANDIDATES_READY_FOR_STEP11C"
            if both
            else "STEP11B_INSUFFICIENT_EXIT_EVIDENCE"
        ),
        "notes": [
            "No policy thresholds were swept; the policy set was fixed before this workflow run.",
            "Selection uses development windows A/B only.",
            "A chosen policy remains a future shadow candidate, not PAPER/runtime/live logic.",
            "RANGE here means frozen RANGE_SHORT only; RANGE_LONG remains NO_TRADE.",
        ],
    }
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(json.dumps({
        "decision": result["decision"],
        "SHORT": {
            "chosen": short["chosen_policy"],
            "baseline_A": short["baseline"]["A"],
            "baseline_B": short["baseline"]["B"],
            "chosen_A": None if short["chosen_policy"] is None else short["policies"][short["chosen_policy"]]["A"],
            "chosen_B": None if short["chosen_policy"] is None else short["policies"][short["chosen_policy"]]["B"],
        },
        "RANGE": {
            "chosen": rang["chosen_policy"],
            "baseline_A": rang["baseline"]["A"],
            "baseline_B": rang["baseline"]["B"],
            "chosen_A": None if rang["chosen_policy"] is None else rang["policies"][rang["chosen_policy"]]["A"],
            "chosen_B": None if rang["chosen_policy"] is None else rang["policies"][rang["chosen_policy"]]["B"],
        },
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
