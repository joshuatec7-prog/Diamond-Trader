#!/usr/bin/env python3
"""
Diamond Trader Periodic Analysis Runner v1.6

Geheugenarme, sequentiële uitvoering van:
1. Diamond Diagnose: exact één ronde met closed-candlecorrectie.
2. Diamond Market Scanner: exact één virtuele scan.
3. Shadow V2 Signal Lab: volgt daarna scannersignalen virtueel.
4. LONG Entry Timing Shadow Lab: vergelijkt CURRENT / WAIT_15M / WAIT_30M.
5. LONG Min-Profit Shadow Lab: vergelijkt €1.00 / €0.50 / €0.25 netto minimumwinst.
6. LONG Combo Shadow Lab: vergelijkt CURRENT / WAIT30_100 / WAIT30_050.
7. Scanner Selective Shadow Lab: vergelijkt CURRENT / SELECTIVE / STRONG.

Belangrijk:
- Alle zeven taken draaien strikt na elkaar en nooit tegelijk.
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

VERSION = "1.6"
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
SCANNER_SELECTIVE_SHADOW_LOG = DATA_DIR / "diamond_scanner_selective_shadow_runner.log"
SCANNER_SESSION_SHADOW_LOG = DATA_DIR / "diamond_scanner_session_shadow_runner.log"

INTERVAL_SECONDS = 15 * 60
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
        "scanner_selective_shadow": [
            sys.executable,
            "scanner_selective_shadow_lab.py",
            "--update",
            "--no-print",
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
        "sequential": True,
        "active_task": None,
        "cycle_count": 0,
        "last_cycle_started_at": None,
        "last_cycle_completed_at": None,
        "next_cycle_not_before": None,
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
        "Diamond Periodic Analysis Runner v1.6 gestart | "
        "interval=900s | sequential=True | "
        "tasks=diagnose,scanner,shadow_v2,long_entry_shadow,long_min_profit_shadow,long_combo_shadow,scanner_selective_shadow,scanner_session_shadow",
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
            "scanner_selective_shadow",
            task_commands()["scanner_selective_shadow"],
            SCANNER_SELECTIVE_SHADOW_LOG,
        )

        if STOP_REQUESTED:
            break

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

    assert state["version"] == "1.6"
    assert state["mode"] == MODE
    assert state["interval_seconds"] == 900
    assert state["sequential"] is True

    assert list(commands.keys()) == [
        "diagnose",
        "scanner",
        "shadow_v2",
        "long_entry_shadow",
        "long_min_profit_shadow",
        "long_combo_shadow",
        "scanner_selective_shadow",
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
        "Taken: diagnose -> scanner -> shadow_v2 -> long_entry_shadow -> long_min_profit_shadow -> long_combo_shadow -> scanner_selective_shadow -> scanner_session_shadow"
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
