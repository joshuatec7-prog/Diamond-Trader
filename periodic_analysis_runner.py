#!/usr/bin/env python3
"""
Diamond Trader Periodic Analysis Runner v2.3

Geheugenarme, sequentiële uitvoering van:
1. Diamond Diagnose: exact één ronde met closed-candlecorrectie.
2. Diamond Market Scanner: exact één virtuele scan.
3. Shadow V2 Signal Lab: volgt daarna scannersignalen virtueel.
4. LONG Entry Timing Shadow Lab: vergelijkt CURRENT / WAIT_15M / WAIT_30M.
5. LONG Min-Profit Shadow Lab: vergelijkt €1.00 / €0.50 / €0.25 netto minimumwinst.
6. LONG Combo Shadow Lab: vergelijkt CURRENT / WAIT30_100 / WAIT30_050.
7. Scanner Selective Shadow Lab: vergelijkt CURRENT / SELECTIVE / STRONG.
8. Execution Quality Shadow: verwerkt nieuwe SELECTIVE trades prospectief.
9. Scanner Session Shadow Lab: volgt sessie-effecten research-only.
10. SELECTIVE Prospective Candidate Tracker: vergelijkt vanaf vaste baseline
   alleen NIEUWE gesloten SELECTIVE trades voor CURRENT / GUARDED_MIX /
   RR_GE_140 / LONG_ALL.

Belangrijk:
- Alle elf taken draaien strikt na elkaar en nooit tegelijk.
- De handelsbot, Agent, Supervisor en Strategy Lab blijven ongemoeid.
- Deze runner plaatst zelf geen orders en wijzigt geen strategie-instellingen.
- Alle drie LONG Shadow Labs gebruiken alleen publieke marktdata.
- Interval blijft 15 minuten.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

VERSION = "2.3"
MODE = "SEQUENTIAL_PERIODIC_ANALYSIS"

PROJECT_DIR = Path("/opt/render/project/src")
DATA_DIR = Path("/var/data")

STATE_FILE = DATA_DIR / "diamond_periodic_analysis_state.json"

DIAG_LOG = DATA_DIR / "diamond_diagnose_runner.log"
SCANNER_LOG = DATA_DIR / "diamond_market_scanner_runner.log"
SHADOW_V2_LOG = DATA_DIR / "diamond_shadow_v2_runner.log"
LONG_ENTRY_SHADOW_LOG = DATA_DIR / "diamond_long_entry_shadow_runner.log"
LONG_MIN_PROFIT_SHADOW_LOG = DATA_DIR / "diamond_long_min_profit_shadow_runner.log"
LONG_COMBO_SHADOW_LOG = DATA_DIR / "diamond_long_combo_shadow_runner.log"
LONG_COMBO_SHADOW_V2_LOG = DATA_DIR / "diamond_long_combo_shadow_v2_runner.log"
SCANNER_SELECTIVE_SHADOW_LOG = DATA_DIR / "diamond_scanner_selective_shadow_runner.log"
EXECUTION_QUALITY_SHADOW_LOG = DATA_DIR / "diamond_execution_quality_shadow_runner.log"
SELECTIVE_PROSPECTIVE_CANDIDATE_LOG = DATA_DIR / "diamond_selective_prospective_candidate_runner.log"
ENTRY_TIMING_PROSPECTIVE_LOG = DATA_DIR / "diamond_entry_timing_prospective_runner.log"
LIST4_FUSION_LOG = DATA_DIR / "diamond_list4_fusion_runner.log"
LIST4_ADMISSION_LOG = DATA_DIR / "diamond_list4_admission_runner.log"
LIST4_DEEP_SCAN_LOG = DATA_DIR / "diamond_list4_deep_scan_runner.log"
LIST4_MULTI_EXCHANGE_LOG = DATA_DIR / "diamond_list4_multi_exchange_runner.log"
EVENT_OUTCOME_LOG = DATA_DIR / "diamond_event_outcome_runner.log"
RESEARCH_RETENTION_LOG = DATA_DIR / "diamond_research_retention_runner.log"
SCANNER_SESSION_SHADOW_LOG = DATA_DIR / "diamond_scanner_session_shadow_runner.log"

INTERVAL_SECONDS = 15 * 60
LIST4_REFRESH_EVERY_CYCLES = 4  # circa 1x per uur
MAX_LOG_BYTES = 5_000_000

STOP_REQUESTED = False
CURRENT_CHILD: Optional[subprocess.Popen[Any]] = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
    ) as tmp:
        json.dump(data, tmp, indent=2, ensure_ascii=False)
        tmp_name = tmp.name

    os.replace(tmp_name, path)


def rotate_log(path: Path) -> None:
    try:
        if not path.exists() or path.stat().st_size <= MAX_LOG_BYTES:
            return

        rotated = path.with_suffix(path.suffix + ".1")

        if rotated.exists():
            rotated.unlink()

        path.replace(rotated)

    except OSError:
        # Logrotatie mag de analyse nooit blokkeren.
        pass


def default_task(command: list[str]) -> Dict[str, Any]:
    return {
        "command": command,
        "run_count": 0,
        "last_started_at": None,
        "last_completed_at": None,
        "last_exit_code": None,
        "last_status": "NOG_NIET_GEDRAAID",
        "last_duration_seconds": None,
    }


def task_commands() -> Dict[str, list[str]]:
    return {
        "diagnose": [
            sys.executable,
            "closed_candle_runner.py",
            "diagnose-once",
        ],
        "scanner": [
            sys.executable,
            "market_scanner.py",
            "--top",
            "20",
        ],
        "shadow_v2": [
            sys.executable,
            "shadow_v2_filter.py",
            "--update",
            "--no-print",
        ],
        "long_entry_shadow": [
            sys.executable,
            "long_entry_shadow_lab.py",
            "--update",
            "--no-print",
        ],
        "long_min_profit_shadow": [
            sys.executable,
            "long_min_profit_shadow_lab.py",
            "--update",
            "--no-print",
        ],
        "long_combo_shadow": [
            sys.executable,
            "long_combo_shadow_lab.py",
            "--update",
            "--no-print",
        ],
        "long_combo_shadow_v2": [
            sys.executable,
            "long_combo_shadow_lab_v2.py",
            "--update",
            "--no-print",
        ],
        "scanner_selective_shadow": [
            sys.executable,
            "scanner_selective_shadow_lab.py",
            "--update",
            "--no-print",
        ],
        "execution_quality_shadow": [
            sys.executable,
            "scanner_execution_quality_shadow.py",
        ],
        "selective_prospective_candidate": [
            sys.executable,
            "diamond_selective_prospective_candidate_tracker.py",
            "diamond_selective_v2_candidate_tracker.py",
        ],
        "entry_timing_prospective": [
            sys.executable,
            "diamond_entry_timing_prospective_tracker.py",
        ],
        "list4_fusion": [
            sys.executable,
            "diamond_event_market_fusion.py",
        ],
        "list4_admission": [
            sys.executable,
            "diamond_coin_admission_shadow_gate.py",
        ],
        "list4_deep_scan": [
            sys.executable,
            "diamond_dynamic_deep_scan_scheduler.py",
            "--no-refresh",
        ],
        "list4_multi_exchange": [
            sys.executable,
            "diamond_multi_exchange_confirmation.py",
            "--no-refresh",
        ],
        "event_outcome": [
            sys.executable,
            "diamond_event_outcome_tracker.py",
        ],
        "research_retention": [
            sys.executable,
            "diamond_research_data_retention.py",
            "--apply",
        ],
        "scanner_session_shadow": [
            sys.executable,
            "scanner_session_shadow_lab.py",
            "--update",
            "--no-print",
        ],
    }


def default_state() -> Dict[str, Any]:
    commands = task_commands()

    return {
        "version": VERSION,
        "mode": MODE,
        "pid": os.getpid(),
        "started_at": now_iso(),
        "interval_seconds": INTERVAL_SECONDS,
        "list4_refresh_every_cycles": LIST4_REFRESH_EVERY_CYCLES,
        "sequential": True,
        "active_task": None,
        "cycle_count": 0,
        "last_cycle_started_at": None,
        "last_cycle_completed_at": None,
        "next_cycle_not_before": None,
        "list4_last_refresh_cycle": None,
        "list4_last_refresh_at": None,
        "research_retention_last_success_date": None,
        "research_retention_last_success_at": None,
        "tasks": {
            name: default_task(command)
            for name, command in commands.items()
        },
    }


def load_state() -> Dict[str, Any]:
    state = default_state()

    if STATE_FILE.exists():
        try:
            loaded = json.loads(
                STATE_FILE.read_text(encoding="utf-8")
            )
        except Exception:
            loaded = None

        if isinstance(loaded, dict):
            state.update(loaded)

    state["version"] = VERSION
    state["mode"] = MODE
    state["pid"] = os.getpid()
    state["interval_seconds"] = INTERVAL_SECONDS
    state["list4_refresh_every_cycles"] = LIST4_REFRESH_EVERY_CYCLES
    state["sequential"] = True

    tasks = state.setdefault("tasks", {})

    # Bestaande run-counts/statussen behouden.
    # Bestaande taken blijven behouden; nieuwe taken worden veilig toegevoegd.
    for name, command in task_commands().items():
        old = tasks.get(name)
        fresh = default_task(command)

        if isinstance(old, dict):
            fresh.update(old)

        fresh["command"] = command
        tasks[name] = fresh

    return state


def handle_signal(signum: int, _frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True

    child = CURRENT_CHILD

    if child is not None and child.poll() is None:
        try:
            child.terminate()
        except OSError:
            pass


def run_task(
    state: Dict[str, Any],
    name: str,
    command: list[str],
    log_file: Path,
) -> int:
    global CURRENT_CHILD

    if STOP_REQUESTED:
        return 143

    task = state["tasks"][name]
    started_monotonic = time.monotonic()
    started_at = now_iso()

    state["active_task"] = name
    task["last_started_at"] = started_at
    task["last_status"] = "BEZIG"
    save_json_atomic(STATE_FILE, state)

    rotate_log(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    exit_code = 1

    try:
        with log_file.open("a", encoding="utf-8") as log:
            log.write("\n" + "=" * 72 + "\n")
            log.write(f"{now_iso()} | START | {name}\n")
            log.write("COMMAND: " + " ".join(command) + "\n")
            log.flush()

            CURRENT_CHILD = subprocess.Popen(
                command,
                cwd=str(PROJECT_DIR),
                stdout=log,
                stderr=subprocess.STDOUT,
            )

            exit_code = int(CURRENT_CHILD.wait())

            log.write(
                f"{now_iso()} | EINDE | {name} | exit={exit_code}\n"
            )
            log.flush()

    except Exception as exc:
        try:
            with log_file.open("a", encoding="utf-8") as log:
                log.write(
                    f"{now_iso()} | RUNNERFOUT | {name} | "
                    f"{type(exc).__name__}: {exc}\n"
                )
        except OSError:
            pass

        exit_code = 1

    finally:
        CURRENT_CHILD = None

    duration = max(
        0.0,
        time.monotonic() - started_monotonic,
    )

    task["run_count"] = (
        int(task.get("run_count", 0) or 0) + 1
    )
    task["last_completed_at"] = now_iso()
    task["last_exit_code"] = exit_code
    task["last_status"] = (
        "OK" if exit_code == 0 else "FOUT"
    )
    task["last_duration_seconds"] = round(
        duration,
        2,
    )

    state["active_task"] = None
    save_json_atomic(STATE_FILE, state)

    return exit_code



def list4_refresh_due(state: Dict[str, Any]) -> bool:
    """
    v2.1:
    - als nog nooit een succesvolle Lijst-4 refresh is geregistreerd: NU draaien;
    - daarna opnieuw zodra minimaal 4 runner-cycli verstreken zijn (~1 uur).

    Hierdoor blokkeert een oude/persistente cycle_count de eerste refresh
    na introductie van deze taken niet meer.
    """
    cycle_count = int(state.get("cycle_count", 0) or 0)
    last_cycle = state.get("list4_last_refresh_cycle")

    if last_cycle is None:
        return True

    try:
        last_cycle = int(last_cycle)
    except (TypeError, ValueError):
        return True

    return (cycle_count - last_cycle) >= LIST4_REFRESH_EVERY_CYCLES


def research_retention_due(
    state: Dict[str, Any],
    utc_date: Optional[str] = None,
) -> bool:
    """
    Eén succesvolle retention-run per UTC-dag.

    Bij eerste introductie van v2.2 ontbreekt de datum en draait de taak
    direct. diamond_research_data_retention.py --apply is idempotent:
    bestaande snapshots van dezelfde UTC-dag worden veilig overgeslagen.
    """
    today = utc_date or datetime.now(timezone.utc).date().isoformat()
    last_date = state.get("research_retention_last_success_date")
    return str(last_date or "") != today


def run_forever() -> None:
    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    signal.signal(
        signal.SIGTERM,
        handle_signal,
    )
    signal.signal(
        signal.SIGINT,
        handle_signal,
    )

    state = load_state()
    state["started_at"] = now_iso()
    state["active_task"] = None
    save_json_atomic(
        STATE_FILE,
        state,
    )

    print(
        f"Diamond Periodic Analysis Runner v{VERSION} gestart | "
        f"interval={INTERVAL_SECONDS}s | sequential=True | "
        f"tasks={','.join(task_commands().keys())}",
        flush=True,
    )

    while not STOP_REQUESTED:
        cycle_started_monotonic = time.monotonic()

        state["cycle_count"] = (
            int(state.get("cycle_count", 0) or 0) + 1
        )
        state["last_cycle_started_at"] = now_iso()
        state["next_cycle_not_before"] = None
        save_json_atomic(
            STATE_FILE,
            state,
        )

        run_task(
            state,
            "diagnose",
            task_commands()["diagnose"],
            DIAG_LOG,
        )

        if STOP_REQUESTED:
            break

        run_task(
            state,
            "scanner",
            task_commands()["scanner"],
            SCANNER_LOG,
        )

        if STOP_REQUESTED:
            break

        run_task(
            state,
            "shadow_v2",
            task_commands()["shadow_v2"],
            SHADOW_V2_LOG,
        )

        if STOP_REQUESTED:
            break

        run_task(
            state,
            "long_entry_shadow",
            task_commands()["long_entry_shadow"],
            LONG_ENTRY_SHADOW_LOG,
        )

        if STOP_REQUESTED:
            break

        run_task(
            state,
            "long_min_profit_shadow",
            task_commands()["long_min_profit_shadow"],
            LONG_MIN_PROFIT_SHADOW_LOG,
        )

        if STOP_REQUESTED:
            break

        run_task(
            state,
            "long_combo_shadow",
            task_commands()["long_combo_shadow"],
            LONG_COMBO_SHADOW_LOG,
        )

        if STOP_REQUESTED:
            break

        run_task(
            state,
            "long_combo_shadow_v2",
            task_commands()["long_combo_shadow_v2"],
            LONG_COMBO_SHADOW_V2_LOG,
        )

        if STOP_REQUESTED:
            break

        run_task(
            state,
            "scanner_selective_shadow",
            task_commands()["scanner_selective_shadow"],
            SCANNER_SELECTIVE_SHADOW_LOG,
        )

        if STOP_REQUESTED:
            break

        run_task(
            state,
            "execution_quality_shadow",
            task_commands()["execution_quality_shadow"],
            EXECUTION_QUALITY_SHADOW_LOG,
        )

        if STOP_REQUESTED:
            break

        run_task(
            state,
            "selective_prospective_candidate",
            task_commands()["selective_prospective_candidate"],
            SELECTIVE_PROSPECTIVE_CANDIDATE_LOG,
        )

        if STOP_REQUESTED:
            break

        run_task(
            state,
            "entry_timing_prospective",
            task_commands()["entry_timing_prospective"],
            ENTRY_TIMING_PROSPECTIVE_LOG,
        )

        if STOP_REQUESTED:
            break

        if list4_refresh_due(state):
            list4_exit_codes = []

            list4_exit_codes.append(
                run_task(
                    state,
                    "list4_fusion",
                    task_commands()["list4_fusion"],
                    LIST4_FUSION_LOG,
                )
            )

            if STOP_REQUESTED:
                break

            list4_exit_codes.append(
                run_task(
                    state,
                    "list4_admission",
                    task_commands()["list4_admission"],
                    LIST4_ADMISSION_LOG,
                )
            )

            if STOP_REQUESTED:
                break

            list4_exit_codes.append(
                run_task(
                    state,
                    "list4_deep_scan",
                    task_commands()["list4_deep_scan"],
                    LIST4_DEEP_SCAN_LOG,
                )
            )

            if STOP_REQUESTED:
                break

            list4_exit_codes.append(
                run_task(
                    state,
                    "list4_multi_exchange",
                    task_commands()["list4_multi_exchange"],
                    LIST4_MULTI_EXCHANGE_LOG,
                )
            )

            if STOP_REQUESTED:
                break

            if all(code == 0 for code in list4_exit_codes):
                state["list4_last_refresh_cycle"] = int(
                    state.get("cycle_count", 0) or 0
                )
                state["list4_last_refresh_at"] = now_iso()
                save_json_atomic(STATE_FILE, state)

        run_task(
            state,
            "event_outcome",
            task_commands()["event_outcome"],
            EVENT_OUTCOME_LOG,
        )

        if STOP_REQUESTED:
            break

        if research_retention_due(state):
            retention_exit = run_task(
                state,
                "research_retention",
                task_commands()["research_retention"],
                RESEARCH_RETENTION_LOG,
            )

            if STOP_REQUESTED:
                break

            if retention_exit == 0:
                state["research_retention_last_success_date"] = (
                    datetime.now(timezone.utc).date().isoformat()
                )
                state["research_retention_last_success_at"] = now_iso()
                save_json_atomic(STATE_FILE, state)

        run_task(
            state,
            "scanner_session_shadow",
            task_commands()["scanner_session_shadow"],
            SCANNER_SESSION_SHADOW_LOG,
        )

        if STOP_REQUESTED:
            break

        state["last_cycle_completed_at"] = now_iso()

        elapsed = max(
            0.0,
            time.monotonic()
            - cycle_started_monotonic,
        )

        sleep_seconds = max(
            5.0,
            INTERVAL_SECONDS - elapsed,
        )

        state["next_cycle_not_before"] = (
            datetime.now(timezone.utc)
            + timedelta(seconds=sleep_seconds)
        ).isoformat()

        save_json_atomic(
            STATE_FILE,
            state,
        )

        deadline = (
            time.monotonic()
            + sleep_seconds
        )

        while not STOP_REQUESTED:
            remaining = (
                deadline
                - time.monotonic()
            )

            if remaining <= 0:
                break

            time.sleep(
                min(
                    5.0,
                    remaining,
                )
            )

    state["active_task"] = None
    state["stopped_at"] = now_iso()
    save_json_atomic(
        STATE_FILE,
        state,
    )


def self_test() -> None:
    state = default_state()
    commands = task_commands()

    assert state["version"] == "2.3"
    assert state["mode"] == MODE
    assert state["interval_seconds"] == 900
    assert state["list4_refresh_every_cycles"] == 4
    assert state["research_retention_last_success_date"] is None
    assert state["research_retention_last_success_at"] is None
    assert state["sequential"] is True

    assert list(commands.keys()) == [
        "diagnose",
        "scanner",
        "shadow_v2",
        "long_entry_shadow",
        "long_min_profit_shadow",
        "long_combo_shadow",
        "long_combo_shadow_v2",
        "scanner_selective_shadow",
        "execution_quality_shadow",
        "selective_prospective_candidate",
        "entry_timing_prospective",
        "list4_fusion",
        "list4_admission",
        "list4_deep_scan",
        "list4_multi_exchange",
        "event_outcome",
        "research_retention",
        "scanner_session_shadow",
    ]

    assert (
        state["tasks"]["diagnose"]["command"][-1]
        == "diagnose-once"
    )

    assert (
        "--loop"
        not in state["tasks"]["scanner"]["command"]
    )

    assert (
        state["tasks"]["scanner"]["command"][-2:]
        == ["--top", "20"]
    )

    assert (
        state["tasks"]["shadow_v2"]["command"][-3:]
        == [
            "shadow_v2_filter.py",
            "--update",
            "--no-print",
        ]
    )

    assert (
        state["tasks"]["long_entry_shadow"]["command"][-3:]
        == [
            "long_entry_shadow_lab.py",
            "--update",
            "--no-print",
        ]
    )

    assert (
        LONG_ENTRY_SHADOW_LOG.name
        == "diamond_long_entry_shadow_runner.log"
    )

    assert (
        state["tasks"]["long_min_profit_shadow"]["command"][-3:]
        == [
            "long_min_profit_shadow_lab.py",
            "--update",
            "--no-print",
        ]
    )

    assert (
        LONG_MIN_PROFIT_SHADOW_LOG.name
        == "diamond_long_min_profit_shadow_runner.log"
    )

    assert (
        state["tasks"]["long_combo_shadow"]["command"][-3:]
        == [
            "long_combo_shadow_lab.py",
            "--update",
            "--no-print",
        ]
    )

    assert (
        LONG_COMBO_SHADOW_LOG.name
        == "diamond_long_combo_shadow_runner.log"
    )

    assert (
        state["tasks"]["scanner_selective_shadow"]["command"][-3:]
        == [
            "scanner_selective_shadow_lab.py",
            "--update",
            "--no-print",
        ]
    )

    assert (
        SCANNER_SELECTIVE_SHADOW_LOG.name
        == "diamond_scanner_selective_shadow_runner.log"
    )

    assert (
        state["tasks"]["execution_quality_shadow"]["command"][-1]
        == "scanner_execution_quality_shadow.py"
    )

    assert (
        EXECUTION_QUALITY_SHADOW_LOG.name
        == "diamond_execution_quality_shadow_runner.log"
    )

    assert (
        state["tasks"]["selective_prospective_candidate"]["command"][-1]
        == "diamond_selective_prospective_candidate_tracker.py"
        == "diamond_selective_v2_candidate_tracker.py"
    )

    assert (
        SELECTIVE_PROSPECTIVE_CANDIDATE_LOG.name
        == "diamond_selective_prospective_candidate_runner.log"
    )

    assert (
        state["tasks"]["entry_timing_prospective"]["command"][-1]
        == "diamond_entry_timing_prospective_tracker.py"
    )

    assert (
        state["tasks"]["list4_fusion"]["command"][-1]
        == "diamond_event_market_fusion.py"
    )

    assert (
        state["tasks"]["list4_admission"]["command"][-1]
        == "diamond_coin_admission_shadow_gate.py"
    )

    assert (
        state["tasks"]["list4_deep_scan"]["command"][-2:]
        == ["diamond_dynamic_deep_scan_scheduler.py", "--no-refresh"]
    )

    assert (
        state["tasks"]["list4_multi_exchange"]["command"][-2:]
        == ["diamond_multi_exchange_confirmation.py", "--no-refresh"]
    )

    assert (
        state["tasks"]["event_outcome"]["command"][-1]
        == "diamond_event_outcome_tracker.py"
    )

    assert (
        state["tasks"]["research_retention"]["command"][-2:]
        == ["diamond_research_data_retention.py", "--apply"]
    )

    assert (
        RESEARCH_RETENTION_LOG.name
        == "diamond_research_retention_runner.log"
    )

    # Dagcadans retention: eerste keer/direct, daarna pas volgende UTC-dag.
    assert research_retention_due(
        {"research_retention_last_success_date": None},
        "2026-08-15",
    ) is True
    assert research_retention_due(
        {"research_retention_last_success_date": "2026-08-15"},
        "2026-08-15",
    ) is False
    assert research_retention_due(
        {"research_retention_last_success_date": "2026-08-15"},
        "2026-08-16",
    ) is True

    # Cadence sanity-check.
    # Belangrijk: bestaande hoge cycle_count zonder last-refresh moet direct draaien.
    assert list4_refresh_due({"cycle_count": 1346}) is True
    assert list4_refresh_due({
        "cycle_count": 1347,
        "list4_last_refresh_cycle": 1346,
    }) is False
    assert list4_refresh_due({
        "cycle_count": 1350,
        "list4_last_refresh_cycle": 1346,
    }) is True

    assert (
        state["tasks"]["scanner_session_shadow"]["command"][-3:]
        == [
            "scanner_session_shadow_lab.py",
            "--update",
            "--no-print",
        ]
    )

    assert (
        SCANNER_SESSION_SHADOW_LOG.name
        == "diamond_scanner_session_shadow_runner.log"
    )

    print(
        "PERIODIC_ANALYSIS_SELF_TEST_OK"
    )
    print(
        "Taken: "
        + " -> ".join(task_commands().keys())
    )
    print(
        "Sequentieel: JA | Interval: 900 seconden"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Diamond Trader geheugenarme periodieke analyse"
        )
    )

    parser.add_argument(
        "--self-test",
        action="store_true",
        help=(
            "Test alleen de runnerconfiguratie; "
            "start geen analyseprocessen."
        ),
    )

    args = parser.parse_args()

    if args.self_test:
        self_test()
        return

    run_forever()


if __name__ == "__main__":
    main()