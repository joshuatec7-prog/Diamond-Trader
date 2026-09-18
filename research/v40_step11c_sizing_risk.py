#!/usr/bin/env python3
"""Step 11C: fixed sizing/risk envelope after Step 11B.

OFFLINE RESEARCH ONLY. No runtime/PAPER/live changes.

This is NOT a return-maximizing stake sweep. It applies fixed safety guardrails
to a small predeclared set of stake/max-open envelopes.

Frozen v4.0 research capital model:
- equity: EUR 3600
- reserve: EUR 200
- Step 11B hard stop: 15.0%
- normal roundtrip costs: 0.78%
- extra execution stress: +0.50 percentage point (from Step 10 stress framing)

Guardrails, fixed before the run:
- stressed single-stop loss <= 2.0% of equity
- stressed aggregate stop loss at max_open <= 6.0% of equity
- five consecutive stressed stops <= 10.0% of starting equity
- deployed stake at max_open must leave the frozen reserve intact

Candidate envelopes are intentionally sparse, not optimized.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

CAPITAL_EUR = 3600.0
RESERVE_EUR = 200.0
AVAILABLE_EUR = CAPITAL_EUR - RESERVE_EUR

STOP_PCT = 15.0
ROUNDTRIP_COST_PCT = 0.78
EXTRA_STRESS_PCT = 0.50
BASE_STOP_NET_LOSS_PCT = STOP_PCT + ROUNDTRIP_COST_PCT
STRESSED_STOP_NET_LOSS_PCT = STOP_PCT + ROUNDTRIP_COST_PCT + EXTRA_STRESS_PCT

GUARDRAILS = {
    "single_stop_equity_pct_max": 2.0,
    "aggregate_stop_equity_pct_max": 6.0,
    "five_consecutive_stop_drawdown_pct_max": 10.0,
    "reserve_eur_min": RESERVE_EUR,
}

ENVELOPES = (
    {"name": "STAKE200_MAX4", "stake_eur": 200.0, "max_open": 4},
    {"name": "STAKE300_MAX4", "stake_eur": 300.0, "max_open": 4},
    {"name": "STAKE400_MAX3", "stake_eur": 400.0, "max_open": 3},
    {"name": "STAKE400_MAX4", "stake_eur": 400.0, "max_open": 4},
    {"name": "STAKE500_MAX2", "stake_eur": 500.0, "max_open": 2},
)


def _evaluate(env: dict[str, Any], short_mean: float, range_mean: float) -> dict[str, Any]:
    stake = float(env["stake_eur"])
    max_open = int(env["max_open"])
    deployed = stake * max_open
    cash_after_full_deploy = CAPITAL_EUR - deployed

    base_stop_eur = stake * BASE_STOP_NET_LOSS_PCT / 100.0
    stress_stop_eur = stake * STRESSED_STOP_NET_LOSS_PCT / 100.0
    single_stress_equity_pct = stress_stop_eur / CAPITAL_EUR * 100.0
    aggregate_stress_eur = stress_stop_eur * max_open
    aggregate_stress_equity_pct = aggregate_stress_eur / CAPITAL_EUR * 100.0
    five_stop_drawdown_eur = stress_stop_eur * 5.0
    five_stop_drawdown_pct = five_stop_drawdown_eur / CAPITAL_EUR * 100.0

    checks = {
        "single_stop_pass": single_stress_equity_pct <= GUARDRAILS["single_stop_equity_pct_max"],
        "aggregate_stop_pass": aggregate_stress_equity_pct <= GUARDRAILS["aggregate_stop_equity_pct_max"],
        "five_stops_pass": five_stop_drawdown_pct <= GUARDRAILS["five_consecutive_stop_drawdown_pct_max"],
        "reserve_pass": cash_after_full_deploy >= RESERVE_EUR,
    }
    passed = all(checks.values())

    return {
        **env,
        "deployed_eur": deployed,
        "cash_after_full_deploy_eur": cash_after_full_deploy,
        "base_stop_loss_eur_per_trade": base_stop_eur,
        "stress_stop_loss_eur_per_trade": stress_stop_eur,
        "stress_single_stop_equity_pct": single_stress_equity_pct,
        "stress_aggregate_stop_loss_eur": aggregate_stress_eur,
        "stress_aggregate_stop_equity_pct": aggregate_stress_equity_pct,
        "five_consecutive_stress_stops_loss_eur": five_stop_drawdown_eur,
        "five_consecutive_stress_stops_drawdown_pct": five_stop_drawdown_pct,
        "development_mean_eur_per_short_trade": stake * short_mean / 100.0,
        "development_mean_eur_per_range_trade": stake * range_mean / 100.0,
        "checks": checks,
        "passed": passed,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("step11b_summary")
    ap.add_argument("--output", default="v40_step11c_sizing_risk_result.json")
    args = ap.parse_args()

    source = json.loads(Path(args.step11b_summary).read_text(encoding="utf-8"))
    if source["decision"] != "STEP11B_EXIT_CANDIDATES_READY_FOR_STEP11C":
        raise RuntimeError("Step 11B is niet vrijgegeven voor Step 11C")
    if source["chosen_policy"]["SHORT"] != "TP10_STOP15" or source["chosen_policy"]["RANGE"] != "TP10_STOP15":
        raise RuntimeError("Step 11C verwacht frozen TP10_STOP15 voor beide states")
    if source["guards"]["days_60_plus_used"] or source["guards"]["step5_6_validation_reused"] or source["guards"]["final15_reused"]:
        raise RuntimeError("Step 11B split guard geschonden")

    short_mean = float(source["SHORT"]["POOLED"]["mean_net_pct"])
    range_mean = float(source["RANGE"]["POOLED"]["mean_net_pct"])

    evaluated = [_evaluate(e, short_mean, range_mean) for e in ENVELOPES]
    passing = [e for e in evaluated if e["passed"]]

    # Predeclared decision rule:
    # 1) stake ceiling = highest stake among passing envelopes
    # 2) at that stake, max_open ceiling = highest passing max_open
    # 3) baseline stake candidate = highest lower stake that passes with max_open >=4
    if not passing:
        decision = "STEP11C_NO_RISK_ENVELOPE_PASSES"
        stake_ceiling = None
        max_open_at_ceiling = None
        baseline = None
    else:
        stake_ceiling = max(float(e["stake_eur"]) for e in passing)
        max_open_at_ceiling = max(
            int(e["max_open"]) for e in passing if float(e["stake_eur"]) == stake_ceiling
        )
        lower_four = [
            e for e in passing
            if float(e["stake_eur"]) < stake_ceiling and int(e["max_open"]) >= 4
        ]
        baseline = max(lower_four, key=lambda e: float(e["stake_eur"])) if lower_four else None
        decision = "STEP11C_RISK_ENVELOPE_READY_FOR_FUTURE_SHADOW"

    mathematical_single_stake_ceiling = (
        CAPITAL_EUR * GUARDRAILS["single_stop_equity_pct_max"] / 100.0
    ) / (STRESSED_STOP_NET_LOSS_PCT / 100.0)

    result = {
        "version": "v40-step11c-sizing-risk-1",
        "mode": "OFFLINE_RISK_CALCULATION_ONLY",
        "execution_enabled": False,
        "active_paper_changed": False,
        "live_orders_possible": False,
        "capital_model": {
            "research_equity_eur": CAPITAL_EUR,
            "reserve_eur": RESERVE_EUR,
            "available_above_reserve_eur": AVAILABLE_EUR,
            "not_claimed_as_current_exchange_balance": True,
        },
        "frozen_exit_policy": "TP10_STOP15",
        "risk_model": {
            "stop_pct": STOP_PCT,
            "roundtrip_cost_pct": ROUNDTRIP_COST_PCT,
            "extra_execution_stress_pct": EXTRA_STRESS_PCT,
            "base_stop_net_loss_pct": BASE_STOP_NET_LOSS_PCT,
            "stressed_stop_net_loss_pct": STRESSED_STOP_NET_LOSS_PCT,
            "mathematical_single_stake_ceiling_eur": mathematical_single_stake_ceiling,
        },
        "guardrails": GUARDRAILS,
        "candidate_envelopes": evaluated,
        "passing_envelopes": [e["name"] for e in passing],
        "provisional": {
            "stake_ceiling_eur": stake_ceiling,
            "max_open_at_stake_ceiling": max_open_at_ceiling,
            "baseline_stake_candidate_eur": None if baseline is None else baseline["stake_eur"],
            "baseline_max_open_candidate": None if baseline is None else baseline["max_open"],
            "variable_strength_sizing_validated": False,
            "note": (
                "EUR 400 is a risk ceiling, not an automatic stake. "
                "A score-to-size mapping has not been validated; EUR 300 remains the lower future-shadow baseline candidate."
                if stake_ceiling is not None else
                "No provisional stake envelope available."
            ),
        },
        "development_reference_only": {
            "short_mean_net_pct": short_mean,
            "range_mean_net_pct": range_mean,
            "warning": "Development means are descriptive only and are not future-return forecasts.",
        },
        "development_only": True,
        "days_60_plus_used": False,
        "step5_6_validation_reused": False,
        "final15_reused": False,
        "decision": decision,
        "next_blockers_before_real_execution": [
            "Prospective shadow validation is still required.",
            "Step 10 execution veto was not promoted; repeated prospective L2 evidence is still required.",
            "SHORT execution venue/borrow/derivatives route is not yet validated.",
        ],
    }
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(json.dumps({
        "decision": decision,
        "passing_envelopes": result["passing_envelopes"],
        "mathematical_single_stake_ceiling_eur": mathematical_single_stake_ceiling,
        "provisional": result["provisional"],
        "candidate_envelopes": [
            {
                "name": e["name"],
                "passed": e["passed"],
                "stress_single_stop_equity_pct": e["stress_single_stop_equity_pct"],
                "stress_aggregate_stop_equity_pct": e["stress_aggregate_stop_equity_pct"],
                "five_stops_drawdown_pct": e["five_consecutive_stress_stops_drawdown_pct"],
                "cash_after_full_deploy_eur": e["cash_after_full_deploy_eur"],
            }
            for e in evaluated
        ],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
