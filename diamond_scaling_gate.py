#!/usr/bin/env python3
# Diamond Trader Scaling Gate v1.0
#
# Blokkeert automatische opschaling.
# Beoordeelt uitsluitend of een VOLGENDE canary-fase handmatig overwogen mag worden.
# Wijzigt GEEN stake, max-open, reserve, config of live-status.
#
# Faseplan:
# 1) €30-€35, max 1 positie, exact 5 gesloten trades
# 2) ~€65, max 1 positie, opnieuw 5 gesloten trades
# 3) €130 doelinzet, reserve €250, max open later geleidelijk tot 5
#
# Geen enkele overgang gebeurt automatisch.

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


VERSION = "1.0"
DATA = Path("/var/data")

POST_REVIEW_PATH = DATA / "diamond_post_canary_review.json"
SAFETY_PATH = DATA / "diamond_live_safety_status.json"
APPROVAL_PATH = DATA / "diamond_scaling_approval.json"
OUTPUT_PATH = DATA / "diamond_scaling_gate_status.json"

PHASE1_CLOSED_REQUIRED = 5
PHASE2_TARGET_STAKE_EUR = 65.0
PHASE2_MAX_OPEN = 1

PHASE3_TARGET_STAKE_EUR = 130.0
PHASE3_RESERVE_EUR = 250.0
PHASE3_MAX_OPEN_CEILING = 5

ALLOWED_REVIEW_PASS = {"PASS_CANDIDATE"}
SAFE_LIVE_STATUSES = {"READY", "NORMAL", "OK", "PASS"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def boolish(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "ja", "on", "aan"}:
        return True
    if text in {"0", "false", "no", "nee", "off", "uit"}:
        return False
    return default


def validate_phase2_approval(
    approval: Dict[str, Any],
    *,
    closed_trades: int,
) -> List[str]:
    problems = []

    if not approval:
        return ["approval_bestand_ontbreekt"]

    if not boolish(approval.get("approved"), False):
        problems.append("approved_is_niet_true")

    if to_int(approval.get("target_phase"), 0) != 2:
        problems.append("target_phase_moet_2_zijn")

    if to_int(approval.get("approved_closed_trades"), -1) != closed_trades:
        problems.append("approved_closed_trades_mismatch")

    if abs(
        to_float(approval.get("approved_stake_eur"), -1.0)
        - PHASE2_TARGET_STAKE_EUR
    ) > 0.001:
        problems.append("approved_stake_eur_moet_65_zijn")

    if to_int(approval.get("approved_max_open"), -1) != PHASE2_MAX_OPEN:
        problems.append("approved_max_open_moet_1_zijn")

    if boolish(approval.get("automatic_scaling"), True):
        problems.append("automatic_scaling_moet_false_zijn")

    return problems


def build_gate(
    post_review: Dict[str, Any],
    safety: Dict[str, Any],
    approval: Dict[str, Any],
) -> Dict[str, Any]:
    closed = to_int(post_review.get("closed_trades"), 0)
    review_status = str(post_review.get("status") or "UNKNOWN").upper()
    safety_status = str(safety.get("status") or "UNKNOWN").upper()
    pending_orders = to_int(safety.get("pending_orders"), 0)
    recovery_required = boolish(safety.get("recovery_required"), False)

    blockers = []
    warnings = []

    if pending_orders > 0:
        blockers.append(f"pending_orders={pending_orders}")

    if recovery_required:
        blockers.append("recovery_required=JA")

    if safety_status not in SAFE_LIVE_STATUSES:
        if safety_status in {"WARNING", "HIGH"}:
            warnings.append(f"live_safety={safety_status}")
        else:
            blockers.append(f"live_safety={safety_status}")

    if closed < PHASE1_CLOSED_REQUIRED:
        status = "HOLD_PHASE1"
        action = (
            f"Fase 1 ongewijzigd laten lopen tot exact "
            f"{PHASE1_CLOSED_REQUIRED} gesloten canary-trades."
        )
        approval_problems = ["nog_geen_phase2_approval_nodig"]

    elif blockers:
        status = "BLOCKED"
        action = "Niet opschalen. Eerst blockers handmatig oplossen."
        approval_problems = validate_phase2_approval(
            approval,
            closed_trades=closed,
        )

    elif review_status not in ALLOWED_REVIEW_PASS:
        status = "HOLD_OR_EXTEND"
        action = (
            "Niet opschalen. Post-canary review is nog geen PASS_CANDIDATE."
        )
        approval_problems = validate_phase2_approval(
            approval,
            closed_trades=closed,
        )

    else:
        approval_problems = validate_phase2_approval(
            approval,
            closed_trades=closed,
        )

        if approval_problems:
            status = "WAIT_MANUAL_APPROVAL"
            action = (
                "Resultaat mag handmatig worden beoordeeld voor fase 2, "
                "maar opschaling blijft geblokkeerd totdat een geldige "
                "expliciete scaling approval aanwezig is."
            )
        else:
            status = "MANUAL_PHASE2_ELIGIBLE"
            action = (
                "Handmatige fase-2 uitvoering mag worden voorbereid: ~€65, "
                "max 1 positie. Dit script wijzigt zelf niets."
            )

    return {
        "version": VERSION,
        "generated_at": now_iso(),
        "status": status,
        "closed_canary_trades": closed,
        "phase1_required": PHASE1_CLOSED_REQUIRED,
        "post_canary_review_status": review_status,
        "live_safety_status": safety_status,
        "blockers": blockers,
        "warnings": warnings,
        "approval_file": str(APPROVAL_PATH),
        "approval_present": bool(approval),
        "approval_problems": approval_problems,
        "manual_action": action,
        "phase_plan": {
            "phase1": {
                "stake_eur": "30-35",
                "max_open": 1,
                "closed_trades_required": 5,
            },
            "phase2": {
                "stake_eur": PHASE2_TARGET_STAKE_EUR,
                "max_open": PHASE2_MAX_OPEN,
                "closed_trades_required": 5,
                "automatic_start": False,
            },
            "phase3": {
                "target_stake_eur": PHASE3_TARGET_STAKE_EUR,
                "reserve_eur": PHASE3_RESERVE_EUR,
                "max_open_ceiling": PHASE3_MAX_OPEN_CEILING,
                "automatic_start": False,
                "gradual_only": True,
            },
        },
        "automatic_scaling": False,
        "stake_changed": False,
        "max_open_changed": False,
        "reserve_changed": False,
        "config_changed": False,
        "automatic_live_change": False,
        "orders_used": False,
        "private_api_used": False,
    }


def print_report(result: Dict[str, Any]) -> None:
    print("=" * 78)
    print(f" DIAMOND SCALING GATE v{VERSION}")
    print("=" * 78)
    print(
        f"Closed canary        : "
        f"{result['closed_canary_trades']}/{result['phase1_required']}"
    )
    print(f"Post-canary review   : {result['post_canary_review_status']}")
    print(f"Live safety          : {result['live_safety_status']}")
    print(f"SCALING STATUS       : {result['status']}")
    print(
        f"Manual approval      : "
        f"{'AANWEZIG' if result['approval_present'] else 'NIET AANWEZIG'}"
    )

    if result["blockers"]:
        print("\n=== BLOCKERS ===")
        for item in result["blockers"]:
            print(f"- {item}")

    if result["warnings"]:
        print("\n=== WAARSCHUWINGEN ===")
        for item in result["warnings"]:
            print(f"- {item}")

    if (
        result["approval_problems"]
        and result["approval_problems"] != ["nog_geen_phase2_approval_nodig"]
    ):
        print("\n=== APPROVAL STATUS ===")
        for item in result["approval_problems"]:
            print(f"- {item}")

    print("\n=== FASEPLAN ===")
    print("Fase 1 : €30-€35 | max 1 | exact 5 closes")
    print("Fase 2 : ~€65     | max 1 | opnieuw 5 closes")
    print("Fase 3 : €130 doel | reserve €250 | max open geleidelijk tot 5")

    print("\n=== HANDMATIGE ACTIE ===")
    print(result["manual_action"])

    print("\nAutomatisch opschalen : NEE")
    print("Stake gewijzigd       : NEE")
    print("Max-open gewijzigd    : NEE")
    print("Reserve gewijzigd     : NEE")
    print("Automatische livegang : NEE")
    print("Orders/private API    : NEE")


def main() -> int:
    post_review = load_json(POST_REVIEW_PATH)
    safety = load_json(SAFETY_PATH)
    approval = load_json(APPROVAL_PATH)

    result = build_gate(post_review, safety, approval)
    atomic_json(OUTPUT_PATH, result)
    print_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
