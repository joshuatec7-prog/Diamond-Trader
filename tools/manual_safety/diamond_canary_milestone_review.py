#!/usr/bin/env python3
# Diamond Trader Canary Milestone Review v1.0
#
# Read-only review voor canary trade 1, 5 en 10.
# Geen orders, geen private API en geen automatische opschaling/livegang.

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional


VERSION = "1.0"
DEFAULT_ANALYSIS = Path("/var/data/diamond_canary_log_analysis.json")

STATUS_ORDER = {
    "OK": 0,
    "WARNING": 1,
    "HIGH": 2,
    "STOP_CANDIDATE": 3,
    "FAIL": 4,
}

MILESTONES = (1, 5, 10)


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        result = float(value)
        if math.isfinite(result):
            return result
    except (TypeError, ValueError):
        pass
    return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def fmt_eur(value: float) -> str:
    sign = "+" if value > 0 else ""
    return f"€{sign}{value:.4f}"


def fmt_pct(value: float) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.4f}%"


def pf_text(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if math.isinf(number):
        return "INF"
    return f"{number:.4f}"


def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def next_milestone(closed: int) -> Optional[int]:
    for milestone in MILESTONES:
        if closed < milestone:
            return milestone
    return None


def reached_milestones(closed: int) -> List[int]:
    return [m for m in MILESTONES if closed >= m]


def current_milestone(closed: int) -> int:
    reached = reached_milestones(closed)
    return reached[-1] if reached else 0


def build_decision(data: Dict[str, Any]) -> Dict[str, Any]:
    closed = to_int(data.get("closed_trades"), 0)
    analyzer_status = str(data.get("status") or "READY").upper()
    issues = list(data.get("issues") or [])
    counts = data.get("status_counts") or {}
    stop_count = to_int(counts.get("STOP_CANDIDATE"), 0)
    high_count = to_int(counts.get("HIGH"), 0)
    warning_count = to_int(counts.get("WARNING"), 0)

    milestone = current_milestone(closed)
    upcoming = next_milestone(closed)

    hard_integrity_problem = analyzer_status == "FAIL"
    pause_candidate = hard_integrity_problem or stop_count > 0

    if closed <= 0:
        decision = "WAIT_FOR_FIRST_CANARY"
        reason = "Nog geen gesloten echte canary-trades."
    elif pause_candidate:
        decision = "PAUSE_CANDIDATE"
        reasons = []
        if hard_integrity_problem:
            reasons.append("canary-log/integriteitsstatus FAIL")
        if stop_count > 0:
            reasons.append(
                f"{stop_count} STOP_CANDIDATE slippage-event(s)"
            )
        reason = "; ".join(reasons)
    elif closed < 5:
        decision = "CONTINUE_PHASE_1_OBSERVATION"
        reason = (
            "Geen harde execution-stop gevonden; "
            "doorlopen naar 5 gesloten canary-trades."
        )
    elif closed < 10:
        decision = "MANUAL_REVIEW_5_READY"
        reason = (
            "Fase-1 mijlpaal bereikt. Handmatige beoordeling vereist; "
            "geen automatische opschaling."
        )
    else:
        decision = "MANUAL_REVIEW_10_READY"
        reason = (
            "10-trade mijlpaal bereikt. Handmatige beoordeling vereist; "
            "geen automatische opschaling."
        )

    return {
        "closed": closed,
        "milestone": milestone,
        "next_milestone": upcoming,
        "decision": decision,
        "reason": reason,
        "hard_integrity_problem": hard_integrity_problem,
        "stop_candidate_count": stop_count,
        "high_count": high_count,
        "warning_count": warning_count,
        "issues": issues,
    }


def print_report(data: Dict[str, Any]) -> int:
    decision = build_decision(data)
    closed = decision["closed"]

    print("=" * 70)
    print(f" DIAMOND CANARY MILESTONE REVIEW v{VERSION}")
    print("=" * 70)

    if not data:
        print("[WAIT] Analyzer-rapport ontbreekt.")
        print("Run eerst: python3 diamond_canary_log_analyzer.py")
        print("Orders/private API : NEE")
        return 0

    print(f"Closed canary      : {closed}")
    print(
        "Mijlpaal           : "
        + (
            f"{decision['milestone']}/10 bereikt"
            if decision["milestone"]
            else "nog geen"
        )
    )
    if decision["next_milestone"] is not None:
        print(
            f"Volgende mijlpaal  : {decision['next_milestone']}/10"
        )
    else:
        print("Volgende mijlpaal  : 10/10 bereikt")

    print(
        f"W/L/BE             : "
        f"{to_int(data.get('wins'))}/"
        f"{to_int(data.get('losses'))}/"
        f"{to_int(data.get('breakeven'))}"
    )
    print(
        f"Actual netto PnL   : "
        f"{fmt_eur(to_float(data.get('actual_net_pnl_quote')))}"
    )
    print(
        f"Expected netto PnL : "
        f"{fmt_eur(to_float(data.get('expected_net_pnl_quote')))}"
    )
    print(
        f"Execution verschil : "
        f"{fmt_eur(to_float(data.get('pnl_difference_quote')))}"
    )
    print(
        f"Profit Factor      : {pf_text(data.get('profit_factor'))}"
    )
    print(
        f"Totale fees        : "
        f"{fmt_eur(to_float(data.get('total_fees_quote')))}"
    )
    print(
        f"Gem. BUY slippage  : "
        f"{fmt_pct(to_float(data.get('avg_buy_slippage_pct')))}"
    )
    print(
        f"Gem. SELL slippage : "
        f"{fmt_pct(to_float(data.get('avg_sell_slippage_pct')))}"
    )
    print(
        f"Max slippage       : "
        f"{fmt_pct(to_float(data.get('max_adverse_slippage_pct')))}"
    )
    print(
        f"Recovery gebruikt  : "
        f"{to_int(data.get('recovery_events'))}"
    )

    counts = data.get("status_counts") or {}
    print(
        "Slippage status    : "
        f"OK={to_int(counts.get('OK'))} "
        f"WARNING={to_int(counts.get('WARNING'))} "
        f"HIGH={to_int(counts.get('HIGH'))} "
        f"STOP={to_int(counts.get('STOP_CANDIDATE'))}"
    )

    print("\n=== MIJLPAALBEOORDELING ===")

    if closed >= 1:
        print(
            "[CHECK 1] Eerste echte trade geregistreerd "
            "en execution-metadata beschikbaar."
        )
    else:
        print("[WAIT 1] Nog geen gesloten echte trade.")

    if closed >= 5:
        print(
            "[READY 5] Fase-1 review beschikbaar. "
            "Handmatig beslissen: doorgaan, langer testen of pauzeren."
        )
    else:
        print(f"[WAIT 5] Nog {5 - closed} gesloten trade(s) nodig.")

    if closed >= 10:
        print(
            "[READY 10] 10-trade review beschikbaar. "
            "Handmatig beslissen; geen automatische opschaling."
        )
    else:
        print(f"[WAIT 10] Nog {10 - closed} gesloten trade(s) nodig.")

    print("\n=== ADVIESSTATUS ===")
    print(f"{decision['decision']} | {decision['reason']}")

    if decision["issues"]:
        print("\n=== DATA / SAFETY MELDINGEN ===")
        for issue in decision["issues"]:
            print(f"- {issue}")

    print("\nAutomatisch schalen : NEE")
    print("Orders/private API  : NEE")
    print("Live wijzigen       : NEE")

    return 2 if decision["decision"] == "PAUSE_CANDIDATE" else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only 1/5/10 canary milestone review."
    )
    parser.add_argument(
        "--analysis",
        default=str(DEFAULT_ANALYSIS),
        help=f"Analyzer JSON (standaard: {DEFAULT_ANALYSIS})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    data = load_json(Path(args.analysis))
    return print_report(data)


if __name__ == "__main__":
    raise SystemExit(main())
