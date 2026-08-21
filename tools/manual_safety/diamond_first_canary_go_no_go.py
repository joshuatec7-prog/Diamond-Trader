#!/usr/bin/env python3
# Diamond Trader First-Canary GO / NO-GO v1.0
#
# Laatste alleen-lezen draaiboekcontrole vóór een APARTE handmatige canary-activering.
# Zet niets live, wijzigt dry-run niet, plaatst geen orders en gebruikt geen private API.

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

try:
    import yaml
except Exception:
    yaml = None


VERSION = "1.0"
ROOT = Path(__file__).resolve().parent
DATA = Path("/var/data")

PREFLIGHT = ROOT / "diamond_canary_preflight.py"
ANALYZER = ROOT / "diamond_canary_log_analyzer.py"
SAFETY = ROOT / "diamond_live_safety_monitor.py"
FEE_VALIDATOR = ROOT / "diamond_fee_cost_validator.py"

PHASE_STATUS = DATA / "diamond_release_phase_status.json"
CANARY_ANALYSIS = DATA / "diamond_canary_log_analysis.json"
LIVE_SAFETY = DATA / "diamond_live_safety_status.json"
FEE_STATUS = DATA / "diamond_fee_cost_validation.json"
STATE = DATA / "diamond_state.json"
CONFIG = ROOT / "config.yaml"
OUTPUT = DATA / "diamond_first_canary_go_no_go.json"

CANARY_STAKE_MIN_EUR = 30.0
CANARY_STAKE_MAX_EUR = 35.0
CANARY_MAX_OPEN = 1
CANARY_PHASE1_CLOSED = 5


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_yaml(path: Path) -> Dict[str, Any]:
    if yaml is None or not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def cfg_value(cfg: Dict[str, Any], dotted: str, default: Any = None) -> Any:
    cur: Any = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def as_bool(value: Any, default: bool = False) -> bool:
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


def as_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


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


def run_helper(path: Path) -> Tuple[int, str]:
    if not path.exists():
        return 127, f"ONTBREEKT:{path.name}"
    try:
        result = subprocess.run(
            ["python3", str(path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
        combined = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        if "Traceback (most recent call last)" in combined:
            return 126, f"PYTHON_FOUT:{path.name}"
        return result.returncode, combined
    except Exception as exc:
        return 125, f"START_FOUT:{path.name}:{type(exc).__name__}"


def add_check(
    checks: List[Dict[str, Any]],
    name: str,
    ok: bool,
    detail: str,
) -> None:
    checks.append({
        "name": name,
        "ok": bool(ok),
        "detail": detail,
    })


def evaluate(
    *,
    preflight_rc: int,
    phase: Dict[str, Any],
    analysis: Dict[str, Any],
    safety: Dict[str, Any],
    fee: Dict[str, Any],
    state: Dict[str, Any],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []

    canary_ready = as_bool(phase.get("canary_ready"), False)
    live_active = as_bool(phase.get("live_active"), False)
    execution_closed = as_int(phase.get("execution_closed"), 0)
    execution_status = str(phase.get("execution_status") or "ONBEKEND").upper()

    dry_run = as_bool(cfg_value(cfg, "risk.dry_run", True), True)

    pending = state.get("pending_orders") or {}
    if not isinstance(pending, dict):
        pending = {}

    recovery_required = as_bool(
        state.get("recovery_required"),
        False,
    )

    opened = as_int(analysis.get("opened_trades"), 0)
    closed = as_int(analysis.get("closed_trades"), 0)
    incomplete = as_int(analysis.get("incomplete_trades"), 0)
    analysis_status = str(
        analysis.get("status") or ("READY" if closed == 0 else "ONBEKEND")
    ).upper()

    safety_status = str(safety.get("status") or "ONBEKEND").upper()
    fee_status = str(fee.get("status") or "READY").upper()

    add_check(
        checks,
        "Centrale preflight",
        preflight_rc == 0,
        "GO" if preflight_rc == 0 else "NO-GO",
    )
    add_check(
        checks,
        "CANARY READY",
        canary_ready,
        "JA" if canary_ready else "NEE",
    )
    add_check(
        checks,
        "Execution eindtest",
        execution_status == "PASS" and execution_closed >= 20,
        f"{execution_closed}/20 status={execution_status}",
    )
    add_check(
        checks,
        "Dry-run nog actief",
        dry_run,
        f"dry_run={dry_run}",
    )
    add_check(
        checks,
        "Live nog uit",
        not live_active,
        "LIVE ACTIVE=NEE" if not live_active else "LIVE ACTIVE=JA",
    )
    add_check(
        checks,
        "Pending orders leeg",
        len(pending) == 0,
        str(len(pending)),
    )
    add_check(
        checks,
        "Recovery vrij",
        not recovery_required,
        "NEE" if not recovery_required else "JA",
    )
    add_check(
        checks,
        "Nog vóór eerste canary",
        opened == 0 and closed == 0 and incomplete == 0,
        f"opened={opened} closed={closed} incomplete={incomplete}",
    )
    add_check(
        checks,
        "Canary analyzer",
        analysis_status in {"READY", "OK"},
        analysis_status,
    )
    add_check(
        checks,
        "Live safety",
        safety_status in {"READY", "NORMAL", "OK"},
        safety_status,
    )
    add_check(
        checks,
        "Fee / cost validator",
        fee_status in {"READY", "PASS", "OK"},
        fee_status,
    )

    blockers = [item for item in checks if not item["ok"]]
    go = len(blockers) == 0

    return {
        "version": VERSION,
        "generated_at": now_iso(),
        "go_for_manual_canary_preparation": go,
        "status": "GO" if go else "NO-GO",
        "checks": checks,
        "blockers": blockers,
        "canary_plan": {
            "stake_min_eur": CANARY_STAKE_MIN_EUR,
            "stake_max_eur": CANARY_STAKE_MAX_EUR,
            "max_open_positions": CANARY_MAX_OPEN,
            "phase1_closed_trades": CANARY_PHASE1_CLOSED,
            "automatic_scaling": False,
            "manual_activation_required": True,
        },
        "automatic_live_change": False,
        "dry_run_changed": False,
        "orders_used": False,
        "private_api_used": False,
    }


def print_report(result: Dict[str, Any]) -> None:
    print("=" * 78)
    print(f" DIAMOND FIRST-CANARY GO / NO-GO v{VERSION}")
    print("=" * 78)

    for item in result["checks"]:
        mark = "PASS" if item["ok"] else "WAIT"
        print(
            f"[{mark:<4}] {item['name']:<26} | {item['detail']}"
        )

    print("\n=== EINDOORDEEL ===")
    print(result["status"])

    if result["blockers"]:
        print("Blokkers:")
        for blocker in result["blockers"]:
            print(
                f"- {blocker['name']}: {blocker['detail']}"
            )
    else:
        print("Alle harde controles zijn PASS.")
        print("Alleen GO voor een APARTE HANDMATIGE canary-voorbereiding.")

    print("\n=== VASTE EERSTE-CANARY GRENZEN ===")
    print("Inzet             : €30 - €35")
    print("Max open posities : 1")
    print("Fase 1            : exact 5 gesloten trades")
    print("Auto opschalen    : NEE")
    print("Handmatige GO     : VERPLICHT")

    print("\nAutomatische livegang : NEE")
    print("Dry-run gewijzigd     : NEE")
    print("Orders/private API    : NEE")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only eerste-canary GO/NO-GO draaiboek."
    )
    parser.add_argument(
        "--skip-helpers",
        action="store_true",
        help="Alleen voor offline tests.",
    )
    parser.add_argument(
        "--output",
        default=str(OUTPUT),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    helper_errors: List[str] = []
    preflight_rc = 1

    if not args.skip_helpers:
        # Analyzer eerst, zodat live safety de nieuwste canary-status leest.
        for helper in (ANALYZER, SAFETY, FEE_VALIDATOR):
            rc, text = run_helper(helper)
            if rc not in (0, 1, 2):
                helper_errors.append(text)

        preflight_rc, preflight_text = run_helper(PREFLIGHT)
        if preflight_rc not in (0, 1):
            helper_errors.append(preflight_text)
    else:
        preflight_rc = 0

    phase = load_json(PHASE_STATUS)
    analysis = load_json(CANARY_ANALYSIS)
    safety = load_json(LIVE_SAFETY)
    fee = load_json(FEE_STATUS)
    state = load_json(STATE)
    cfg = load_yaml(CONFIG)

    if helper_errors:
        preflight_rc = 2

    result = evaluate(
        preflight_rc=preflight_rc,
        phase=phase,
        analysis=analysis,
        safety=safety,
        fee=fee,
        state=state,
        cfg=cfg,
    )

    if helper_errors:
        for error in helper_errors:
            result["blockers"].append({
                "name": "Helper",
                "ok": False,
                "detail": error,
            })
        result["status"] = "NO-GO"
        result["go_for_manual_canary_preparation"] = False

    atomic_json(Path(args.output), result)
    print_report(result)

    return 0 if result["go_for_manual_canary_preparation"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
