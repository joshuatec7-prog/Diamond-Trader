#!/usr/bin/env python3
"""
Diamond Trader Periodic Analysis Runner v1.1

Geheugenarme, sequentiële uitvoering van:
1. Diamond Diagnose: exact één ronde met closed-candlecorrectie.
2. Diamond Market Scanner: exact één virtuele scan.
3. Shadow V2 Signal Lab: volgt daarna de nieuw geschreven scannersignalen virtueel.

Belangrijk:
- Diagnose, Scanner en Shadow V2 draaien nooit tegelijk.
- De handelsbot, Agent, Supervisor en Strategy Lab blijven ongemoeid.
- Deze runner plaatst zelf geen orders en wijzigt geen strategie-instellingen.
- Interval blijft 15 minuten, gelijk aan de bestaande diagnose/scanner-loop.
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

VERSION = "1.1"
MODE = "SEQUENTIAL_PERIODIC_ANALYSIS"

PROJECT_DIR = Path("/opt/render/project/src")
DATA_DIR = Path("/var/data")
STATE_FILE = DATA_DIR / "diamond_periodic_analysis_state.json"
DIAG_LOG = DATA_DIR / "diamond_diagnose_runner.log"
SCANNER_LOG = DATA_DIR / "diamond_market_scanner_runner.log"
SHADOW_V2_LOG = DATA_DIR / "diamond_shadow_v2_runner.log"

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


def default_state() -> Dict[str, Any]:
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
            "diagnose": default_task(
                [
                    sys.executable,
                    "closed_candle_runner.py",
                    "diagnose-once",
                ]
            ),
            "scanner": default_task(
                [
                    sys.executable,
                    "market_scanner.py",
                    "--top",
                    "20",
                ]
            ),
            "shadow_v2": default_task(
                [
                    sys.executable,
                    "shadow_v2_filter.py",
                    "--update",
                    "--no-print",
                ]
            ),
        },
    }


def load_state() -> Dict[str, Any]:
    state = default_state()

    if not STATE_FILE.exists():
        return state

    try:
        loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return state

    if not isinstance(loaded, dict):
        return state

    state.update(loaded)
    state["version"] = VERSION
    state["mode"] = MODE
    state["pid"] = os.getpid()
    state["interval_seconds"] = INTERVAL_SECONDS
    state["sequential"] = True

    tasks = state.setdefault("tasks", {})

    for name, command in {
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
    }.items():
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

    duration = max(0.0, time.monotonic() - started_monotonic)

    task["run_count"] = int(task.get("run_count", 0) or 0) + 1
    task["last_completed_at"] = now_iso()
    task["last_exit_code"] = exit_code
    task["last_status"] = "OK" if exit_code == 0 else "FOUT"
    task["last_duration_seconds"] = round(duration, 2)
    state["active_task"] = None
    save_json_atomic(STATE_FILE, state)

    return exit_code


def run_forever() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    state = load_state()
    state["started_at"] = now_iso()
    state["active_task"] = None
    save_json_atomic(STATE_FILE, state)

    print(
        "Diamond Periodic Analysis Runner v1.1 gestart | "
        "interval=900s | sequential=True",
        flush=True,
    )

    while not STOP_REQUESTED:
        cycle_started_monotonic = time.monotonic()
        state["cycle_count"] = int(state.get("cycle_count", 0) or 0) + 1
        state["last_cycle_started_at"] = now_iso()
        state["next_cycle_not_before"] = None
        save_json_atomic(STATE_FILE, state)

        run_task(
            state,
            "diagnose",
            [
                sys.executable,
                "closed_candle_runner.py",
                "diagnose-once",
            ],
            DIAG_LOG,
        )

        if STOP_REQUESTED:
            break

        run_task(
            state,
            "scanner",
            [
                sys.executable,
                "market_scanner.py",
                "--top",
                "20",
            ],
            SCANNER_LOG,
        )

        if STOP_REQUESTED:
            break

        run_task(
            state,
            "shadow_v2",
            [
                sys.executable,
                "shadow_v2_filter.py",
                "--update",
                "--no-print",
            ],
            SHADOW_V2_LOG,
        )

        if STOP_REQUESTED:
            break

        state["last_cycle_completed_at"] = now_iso()

        elapsed = max(0.0, time.monotonic() - cycle_started_monotonic)
        sleep_seconds = max(5.0, INTERVAL_SECONDS - elapsed)
        state["next_cycle_not_before"] = (
            datetime.now(timezone.utc)
            + timedelta(seconds=sleep_seconds)
        ).isoformat()
        save_json_atomic(STATE_FILE, state)

        deadline = time.monotonic() + sleep_seconds

        while not STOP_REQUESTED:
            remaining = deadline - time.monotonic()

            if remaining <= 0:
                break

            time.sleep(min(5.0, remaining))

    state["active_task"] = None
    state["stopped_at"] = now_iso()
    save_json_atomic(STATE_FILE, state)


def self_test() -> None:
    state = default_state()

    assert state["mode"] == MODE
    assert state["interval_seconds"] == 900
    assert state["sequential"] is True
    assert state["tasks"]["diagnose"]["command"][-1] == "diagnose-once"
    assert "--loop" not in state["tasks"]["scanner"]["command"]
    assert state["tasks"]["scanner"]["command"][-2:] == ["--top", "20"]
    assert state["tasks"]["shadow_v2"]["command"][-3:] == [
        "shadow_v2_filter.py",
        "--update",
        "--no-print",
    ]

    print("PERIODIC_ANALYSIS_SELF_TEST_OK")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Diamond Trader geheugenarme periodieke analyse"
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Test alleen de runnerconfiguratie; start geen Diagnose of Scanner.",
    )
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return

    run_forever()


if __name__ == "__main__":
    main()
