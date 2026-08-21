#!/usr/bin/env python3
# Diamond Trader Canary Preflight v1.0
#
# Eén read-only GO / NO-GO controle vóór de eerste echte canary.
# Plaatst geen orders, gebruikt geen private exchange-API en zet live niet aan.

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple

try:
    import yaml
except Exception:
    yaml = None


VERSION = "1.0"
ROOT = Path(__file__).resolve().parent
DATA = Path("/var/data")

DEFAULT_READINESS = ROOT / "diamond_release_go_live_readiness.py"
DEFAULT_PHASE_STATUS = DATA / "diamond_release_phase_status.json"
DEFAULT_CONFIG = ROOT / "config.yaml"

REQUIRED_PROCESSES = (
    "agent.py",
    "supervisor_agent.py",
    "closed_candle_runner.py",
    "periodic_analysis_runner.py",
)


def load_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
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
        return int(float(value))
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_config(path: Path) -> Dict[str, Any]:
    if yaml is None or not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def run_readiness(path: Path) -> Tuple[bool, str]:
    if not path.exists():
        return False, f"readiness script ontbreekt: {path.name}"

    try:
        result = subprocess.run(
            ["python3", str(path)],
            cwd=str(path.parent),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except Exception as exc:
        return False, f"readiness kon niet draaien: {type(exc).__name__}"

    # Readiness kan NOT READY zijn zolang Execution nog niet klaar is.
    # Dat is geen scriptfout. Alleen een echte crash / Python-fout telt hier.
    combined = (result.stdout or "") + "\n" + (result.stderr or "")
    if "Traceback (most recent call last)" in combined:
        return False, "readiness gaf een Python-fout"

    return True, ""


def process_running(pattern: str) -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        return False


def add_check(
    checks: List[Dict[str, Any]],
    name: str,
    ok: bool,
    detail: str,
    hard: bool = True,
) -> None:
    checks.append(
        {
            "name": name,
            "ok": bool(ok),
            "detail": detail,
            "hard": bool(hard),
        }
    )


def evaluate(
    phase: Dict[str, Any],
    cfg: Dict[str, Any],
    *,
    check_processes: bool = True,
) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []

    selective = str(phase.get("selective_status") or "").upper()
    execution = str(phase.get("execution_status") or "").upper()
    execution_closed = as_int(phase.get("execution_closed"), 0)

    safety_passed = as_int(phase.get("safety_passed"), 0)
    safety_total = as_int(phase.get("safety_total"), 0)

    buy_recovery = as_bool(phase.get("buy_recovery_ready"), False)
    sell_recovery = as_bool(phase.get("sell_recovery_ready"), False)
    canary_logging = as_bool(phase.get("canary_logging_ready"), False)
    slippage_status = as_bool(phase.get("slippage_status_ready"), False)
    matrix_ready = as_bool(phase.get("pre_canary_matrix_ready"), False)
    matrix_passed = as_int(phase.get("pre_canary_matrix_passed"), 0)
    matrix_total = as_int(phase.get("pre_canary_matrix_total"), 16)

    pending_orders = as_int(phase.get("pending_orders"), 999)
    recovery_required = as_bool(phase.get("recovery_required"), True)
    canary_ready = as_bool(phase.get("canary_ready"), False)
    live_active = as_bool(phase.get("live_active"), False)

    dry_run = as_bool(cfg_value(cfg, "risk.dry_run", True), True)
    reserve = as_float(cfg_value(cfg, "risk.eur_reserve", 0), 0.0)
    max_open = as_int(
        cfg_value(cfg, "risk.max_open_positions", 999),
        999,
    )
    max_total = as_int(
        cfg_value(cfg, "trading.max_total_positions", 999),
        999,
    )

    add_check(
        checks,
        "SELECTIVE",
        selective == "PASS",
        f"status={selective or 'ONBEKEND'}",
    )
    add_check(
        checks,
        "Execution",
        execution == "PASS" and execution_closed >= 20,
        f"{execution_closed}/20 status={execution or 'ONBEKEND'}",
    )
    add_check(
        checks,
        "Safety / Recovery",
        safety_total >= 8 and safety_passed == safety_total,
        f"{safety_passed}/{safety_total or 8}",
    )
    add_check(
        checks,
        "BUY recovery",
        buy_recovery,
        "PASS" if buy_recovery else "NIET PASS",
    )
    add_check(
        checks,
        "SELL recovery",
        sell_recovery,
        "PASS" if sell_recovery else "NIET PASS",
    )
    add_check(
        checks,
        "Canary logging",
        canary_logging,
        "PASS" if canary_logging else "NIET PASS",
    )
    add_check(
        checks,
        "Slippage monitoring",
        slippage_status,
        "PASS" if slippage_status else "NIET PASS",
    )
    add_check(
        checks,
        "Safety matrix",
        matrix_ready and matrix_total >= 16 and matrix_passed == matrix_total,
        f"{matrix_passed}/{matrix_total or 16}",
    )
    add_check(
        checks,
        "Dry-run vóór handmatige GO",
        dry_run,
        f"dry_run={dry_run}",
    )
    add_check(
        checks,
        "Reserve",
        reserve >= 250.0,
        f"€{reserve:.2f} (minimaal €250)",
    )
    add_check(
        checks,
        "Max posities",
        max_open <= 5 and max_total <= 5,
        f"spot={max_open} totaal={max_total}",
    )
    add_check(
        checks,
        "Pending orders",
        pending_orders == 0,
        str(pending_orders),
    )
    add_check(
        checks,
        "Recovery vrij",
        not recovery_required,
        "NEE" if not recovery_required else "JA",
    )
    add_check(
        checks,
        "Live nog uit",
        not live_active,
        "LIVE ACTIVE=NEE" if not live_active else "LIVE ACTIVE=JA",
    )

    if check_processes:
        for pattern in REQUIRED_PROCESSES:
            running = process_running(pattern)
            add_check(
                checks,
                f"Proces {pattern}",
                running,
                "RUNNING" if running else "NIET GEVONDEN",
            )

    # Dit is de bestaande centrale readiness-uitkomst.
    # Hij moet eveneens JA zijn; zo kan deze checker nooit per ongeluk
    # soepeler zijn dan diamond_release_go_live_readiness.py.
    add_check(
        checks,
        "Centrale CANARY READY",
        canary_ready,
        "JA" if canary_ready else "NEE",
    )

    blockers = [
        check
        for check in checks
        if check["hard"] and not check["ok"]
    ]

    return {
        "go": len(blockers) == 0,
        "checks": checks,
        "blockers": blockers,
        "execution_closed": execution_closed,
        "canary_ready": canary_ready,
        "live_active": live_active,
    }


def print_report(result: Dict[str, Any]) -> None:
    print("=" * 72)
    print(f" DIAMOND CANARY PREFLIGHT v{VERSION}")
    print("=" * 72)

    for check in result["checks"]:
        mark = "PASS" if check["ok"] else "WAIT"
        print(
            f"[{mark:<4}] {check['name']:<28} | {check['detail']}"
        )

    print("\n=== EINDOORDEEL ===")
    if result["go"]:
        print("GO")
        print("Alle harde pre-canary controles zijn PASS.")
        print("Dit is alleen GO voor een APARTE HANDMATIGE canary-activering.")
    else:
        print("NO-GO")
        print("Blokkers:")
        for blocker in result["blockers"]:
            print(
                f"- {blocker['name']}: {blocker['detail']}"
            )

    print("\nAutomatische livegang : NEE")
    print("Dry-run wijzigen      : NEE")
    print("Orders/private API    : NEE")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Diamond Trader read-only canary GO/NO-GO preflight."
    )
    parser.add_argument(
        "--readiness",
        default=str(DEFAULT_READINESS),
    )
    parser.add_argument(
        "--phase-status",
        default=str(DEFAULT_PHASE_STATUS),
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
    )
    parser.add_argument(
        "--skip-readiness",
        action="store_true",
        help="Alleen voor offline tests: centrale readiness niet eerst draaien.",
    )
    parser.add_argument(
        "--skip-processes",
        action="store_true",
        help="Alleen voor offline tests: proceschecks overslaan.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.skip_readiness:
        ok, reason = run_readiness(Path(args.readiness))
        if not ok:
            print("=" * 72)
            print(f" DIAMOND CANARY PREFLIGHT v{VERSION}")
            print("=" * 72)
            print("NO-GO")
            print(f"- Centrale readiness kon niet veilig draaien: {reason}")
            print("\nAutomatische livegang : NEE")
            print("Orders/private API    : NEE")
            return 2

    phase = load_json(Path(args.phase_status))
    cfg = load_config(Path(args.config))

    if not phase:
        print("=" * 72)
        print(f" DIAMOND CANARY PREFLIGHT v{VERSION}")
        print("=" * 72)
        print("NO-GO")
        print("- diamond_release_phase_status.json ontbreekt of is ongeldig")
        print("\nAutomatische livegang : NEE")
        print("Orders/private API    : NEE")
        return 2

    if not cfg:
        print("=" * 72)
        print(f" DIAMOND CANARY PREFLIGHT v{VERSION}")
        print("=" * 72)
        print("NO-GO")
        print("- config.yaml ontbreekt of kan niet worden gelezen")
        print("\nAutomatische livegang : NEE")
        print("Orders/private API    : NEE")
        return 2

    result = evaluate(
        phase,
        cfg,
        check_processes=not args.skip_processes,
    )
    print_report(result)

    return 0 if result["go"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
