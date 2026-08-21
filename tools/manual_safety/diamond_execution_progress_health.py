#!/usr/bin/env python3
"""
Diamond Trader Execution Progress Health v1.0

Read-only controle of Execution BASELINE gewoon op nieuwe trades wacht
of technisch niet meer wordt ververst.

Controleert:
- huidige BASELINE stand via diamond_prospective_final_analyzer.py;
- bestaan van scanner_execution_quality_shadow.py;
- welke projectbestanden die execution-scriptnaam aanroepen/verwijzen;
- bekende execution-quality state/report/trades bestanden in /var/data;
- freshness van execution-output versus SELECTIVE shadow-tradebron;
- periodic runner state/fouten.

Wijzigt niets aan strategy, filters, state, config of live.
Schrijft geen rapportbestand; alleen terminaloutput.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VERSION = "1.0"

ROOT = Path(os.getenv("DIAMOND_PROJECT_DIR", "/opt/render/project/src"))
DATA = Path(os.getenv("DIAMOND_DATA_DIR", "/var/data"))

EXEC_SCRIPT = ROOT / "scanner_execution_quality_shadow.py"
ANALYZER = ROOT / "diamond_prospective_final_analyzer.py"
PERIODIC_STATE = DATA / "diamond_periodic_analysis_state.json"
SELECTIVE_TRADES = DATA / "diamond_scanner_selective_shadow_trades.csv"

FRESH_MINUTES = 45
STALE_MINUTES = 90


def now_ts() -> float:
    return time.time()


def age_minutes(path: Path) -> Optional[float]:
    try:
        return max(0.0, (now_ts() - path.stat().st_mtime) / 60.0)
    except OSError:
        return None


def age_text(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    if value < 1:
        return "<1m"
    if value < 60:
        return f"{value:.0f}m"
    return f"{value / 60.0:.1f}h"


def load_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def run_analyzer() -> str:
    if not ANALYZER.is_file():
        return ""
    try:
        result = subprocess.run(
            ["python3", str(ANALYZER)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        return (result.stdout or "") + (
            ("\n" + result.stderr) if result.stderr else ""
        )
    except Exception:
        return ""


def parse_baseline(text: str) -> Dict[str, Any]:
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip().upper().startswith("BASELINE")
    ]

    for line in lines:
        if "W/L" not in line.upper() and "PNL" not in line.upper():
            continue

        n_match = re.search(r"(\d+)\s*/\s*20", line)
        wl_match = re.search(r"W/L\s*=?\s*(\d+)\s*/\s*(\d+)", line, re.I)
        pnl_match = re.search(r"pnl\s*=?\s*€?\s*([+-]?\d+(?:[.,]\d+)?)", line, re.I)
        pf_match = re.search(r"\bPF\s*=?\s*([0-9.+-]+|inf)", line, re.I)

        return {
            "found": True,
            "line": line,
            "closed": int(n_match.group(1)) if n_match else None,
            "wins": int(wl_match.group(1)) if wl_match else None,
            "losses": int(wl_match.group(2)) if wl_match else None,
            "pnl": (
                float(pnl_match.group(1).replace(",", "."))
                if pnl_match else None
            ),
            "pf": pf_match.group(1) if pf_match else None,
        }

    return {"found": False}


def scan_callers() -> List[str]:
    """
    Zoek alleen naar concrete referenties naar scanner_execution_quality_shadow.py
    in project .py/.sh bestanden. Het script zelf telt niet als caller.
    """
    found: List[str] = []
    target = EXEC_SCRIPT.name

    if not ROOT.is_dir():
        return found

    for pattern in ("*.py", "*.sh"):
        for path in ROOT.glob(pattern):
            if path.name == target:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            if target in text:
                found.append(path.name)

    return sorted(set(found))


def extract_declared_data_files() -> List[Path]:
    """
    Haal /var/data bestandsnamen uit simpele constante-declaraties in het
    execution-script. Daarna voegen we een veilige glob-fallback toe.
    """
    files: List[Path] = []

    if EXEC_SCRIPT.is_file():
        try:
            text = EXEC_SCRIPT.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""

        patterns = [
            r'(?:DATA_DIR|DATA)\s*/\s*["\']([^"\']*execution[^"\']*)["\']',
            r'(?:DATA_DIR|DATA)\s*/\s*["\']([^"\']*quality[^"\']*)["\']',
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.I):
                path = DATA / match.group(1)
                if path not in files:
                    files.append(path)

    if DATA.is_dir():
        for path in DATA.glob("*execution*quality*"):
            if path.is_file() and path not in files:
                files.append(path)

    return sorted(files, key=lambda p: p.name)


def newest_execution_output(files: List[Path]) -> Optional[Path]:
    existing = [path for path in files if path.is_file()]
    if not existing:
        return None
    return max(existing, key=lambda p: p.stat().st_mtime)


def periodic_info() -> Dict[str, Any]:
    state = load_json(PERIODIC_STATE)
    tasks = state.get("tasks") or {}

    errors = 0
    for task in tasks.values():
        if not isinstance(task, dict):
            continue
        if str(task.get("last_status") or "").upper() == "FOUT":
            errors += 1

    exact_task = None
    for name, task in tasks.items():
        if not isinstance(task, dict):
            continue
        command = " ".join(str(x) for x in (task.get("command") or []))
        if EXEC_SCRIPT.name in command:
            exact_task = {
                "name": name,
                "status": task.get("last_status"),
                "last_completed_at": task.get("last_completed_at"),
                "last_exit_code": task.get("last_exit_code"),
            }
            break

    return {
        "available": bool(state),
        "version": state.get("version"),
        "active_task": state.get("active_task"),
        "cycle_count": state.get("cycle_count"),
        "errors": errors,
        "execution_task": exact_task,
    }


def evaluate(
    baseline: Dict[str, Any],
    callers: List[str],
    execution_files: List[Path],
    periodic: Dict[str, Any],
) -> Tuple[str, List[str]]:
    reasons: List[str] = []

    if not EXEC_SCRIPT.is_file():
        return "TECHNISCH_PROBLEEM", ["execution-script ontbreekt"]

    if not baseline.get("found"):
        return "TECHNISCH_PROBLEEM", ["BASELINE niet gevonden in analyzer-output"]

    newest = newest_execution_output(execution_files)
    exec_age = age_minutes(newest) if newest else None
    source_age = age_minutes(SELECTIVE_TRADES)

    has_refresh_path = bool(callers) or bool(periodic.get("execution_task"))

    if periodic.get("execution_task"):
        task = periodic["execution_task"]
        if str(task.get("status") or "").upper() == "FOUT":
            return "TECHNISCH_PROBLEEM", [
                f"periodieke execution-taak staat op FOUT ({task.get('last_exit_code')})"
            ]

    if newest is None:
        if has_refresh_path:
            return "CONTROLEREN", [
                "refresh-pad gevonden maar geen execution state/report/trades bestand gevonden"
            ]
        return "TECHNISCH_PROBLEEM", [
            "geen execution output gevonden",
            "geen automatische refresh-route gevonden",
        ]

    # Source is duidelijk nieuwer dan execution-output: execution lijkt achter te lopen.
    if (
        source_age is not None
        and exec_age is not None
        and exec_age - source_age > 30
        and exec_age > FRESH_MINUTES
    ):
        return "MOGELIJK_VAST", [
            f"SELECTIVE bron is {age_text(source_age)} oud",
            f"nieuwste execution-output is {age_text(exec_age)} oud",
        ]

    if exec_age is not None and exec_age > STALE_MINUTES and not has_refresh_path:
        return "MOGELIJK_VAST", [
            f"execution-output is {age_text(exec_age)} oud",
            "geen automatische refresh-route gevonden",
        ]

    if has_refresh_path:
        reasons.append("automatische/verwijzende refresh-route gevonden")

    if exec_age is not None:
        reasons.append(f"nieuwste execution-output {age_text(exec_age)} oud")

    closed = baseline.get("closed")
    if isinstance(closed, int) and closed < 20:
        reasons.append(f"BASELINE staat op {closed}/20")

    if exec_age is not None and exec_age <= FRESH_MINUTES:
        return "WACHT_OP_NIEUWE_TRADES", reasons

    return "GEZOND_MAAR_LANGZAAM", reasons


def main() -> int:
    analyzer_text = run_analyzer()
    baseline = parse_baseline(analyzer_text)
    callers = scan_callers()
    execution_files = extract_declared_data_files()
    periodic = periodic_info()

    status, reasons = evaluate(
        baseline,
        callers,
        execution_files,
        periodic,
    )

    print("=" * 86)
    print(f" DIAMOND EXECUTION PROGRESS HEALTH v{VERSION}")
    print("=" * 86)

    print("=== EXECUTION BASELINE ===")
    if baseline.get("found"):
        print(baseline["line"])
    else:
        print("BASELINE: NIET GEVONDEN")

    print("\n=== REFRESH-PAD ===")
    print(f"Execution script       : {'AANWEZIG' if EXEC_SCRIPT.is_file() else 'ONTBREEKT'}")

    task = periodic.get("execution_task")
    if task:
        print(
            f"Periodic taak          : JA | {task['name']} | "
            f"{task.get('status')} | exit={task.get('last_exit_code')}"
        )
    else:
        print("Periodic taak          : NEE")

    if callers:
        print("Andere verwijzers      : " + ", ".join(callers[:8]))
    else:
        print("Andere verwijzers      : GEEN")

    print("\n=== DATA FRESHNESS ===")
    if SELECTIVE_TRADES.is_file():
        print(
            f"SELECTIVE tradebron    : AANWEZIG | "
            f"leeftijd={age_text(age_minutes(SELECTIVE_TRADES))}"
        )
    else:
        print("SELECTIVE tradebron    : ONTBREEKT")

    existing = [p for p in execution_files if p.is_file()]
    if not existing:
        print("Execution datafiles    : GEEN GEVONDEN")
    else:
        for path in sorted(
            existing,
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:8]:
            print(
                f"{path.name:<40} "
                f"leeftijd={age_text(age_minutes(path))}"
            )

    print("\n=== PERIODIC RUNNER ===")
    if periodic.get("available"):
        print(
            f"versie={periodic.get('version')} | "
            f"cycles={periodic.get('cycle_count')} | "
            f"fouten={periodic.get('errors')} | "
            f"active={periodic.get('active_task') or '-'}"
        )
    else:
        print("Periodic state: ONTBREEKT")

    print("\n=== OORDEEL ===")
    print(status)
    for reason in reasons:
        print(f"- {reason}")

    print("\n=== VEILIGHEID ===")
    print("Execution regels gewijzigd : NEE")
    print("Strategy/filter gewijzigd  : NEE")
    print("State/config gewijzigd      : NEE")
    print("Orders/private API          : NEE")
    print("Live wijziging              : NEE")

    return 0 if status not in {"TECHNISCH_PROBLEEM"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
