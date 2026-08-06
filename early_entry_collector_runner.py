#!/usr/bin/env python3
"""
Diamond Trader Early Entry Collector Runner v1.0

Houdt early_entry_collector_v1_2.py permanent actief.
Als alleen de collector stopt, wordt alleen die collector opnieuw gestart.
De runner zelf blijft actief zodat start.sh / wait -n niet reageert op
een gewone collector-restart.
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

VERSION = "1.0"
PROJECT_DIR = Path("/opt/render/project/src")
COLLECTOR = PROJECT_DIR / "early_entry_collector_v1_2.py"
RESTART_DELAY_SECONDS = 15
MAX_RESTART_DELAY_SECONDS = 300

STOP_REQUESTED = False
CHILD: Optional[subprocess.Popen] = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    print(f"{now_iso()} | {message}", flush=True)


def request_stop(*_args) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    child = CHILD
    if child is not None and child.poll() is None:
        try:
            child.terminate()
        except Exception:
            pass


def self_test() -> None:
    assert VERSION == "1.0"
    assert RESTART_DELAY_SECONDS == 15
    assert MAX_RESTART_DELAY_SECONDS == 300
    assert COLLECTOR.name == "early_entry_collector_v1_2.py"

    if not COLLECTOR.exists():
        raise SystemExit(
            f"EARLY_ENTRY_RUNNER_SELF_TEST_FOUT: collector ontbreekt: {COLLECTOR}"
        )

    result = subprocess.run(
        [sys.executable, str(COLLECTOR), "--self-test"],
        cwd=str(PROJECT_DIR),
        text=True,
        capture_output=True,
        timeout=30,
    )

    if result.returncode != 0:
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        raise SystemExit(
            f"EARLY_ENTRY_RUNNER_SELF_TEST_FOUT: collector exit={result.returncode}"
        )

    print("EARLY_ENTRY_RUNNER_SELF_TEST_OK")
    print(f"Runner versie    : {VERSION}")
    print(f"Collector        : {COLLECTOR.name}")
    print("Collector-restart: JA")
    print("Orders mogelijk  : NEE")
    print("Private API      : NEE")
    print("Bot/config       : ONGEWIJZIGD")


def run_forever() -> int:
    global CHILD

    if not COLLECTOR.exists():
        log(f"FOUT: collector ontbreekt: {COLLECTOR}")
        return 2

    delay = RESTART_DELAY_SECONDS
    restart_count = 0

    log(
        "Early Entry Collector Runner gestart | "
        f"version={VERSION} | collector={COLLECTOR.name}"
    )

    while not STOP_REQUESTED:
        started = time.monotonic()

        log(
            "Collector starten | "
            f"restart_count={restart_count}"
        )

        try:
            CHILD = subprocess.Popen(
                [sys.executable, str(COLLECTOR)],
                cwd=str(PROJECT_DIR),
            )
        except Exception as exc:
            log(
                "FOUT bij starten collector | "
                f"{type(exc).__name__}: {exc}"
            )
            CHILD = None

            if STOP_REQUESTED:
                break

            time.sleep(delay)
            delay = min(delay * 2, MAX_RESTART_DELAY_SECONDS)
            restart_count += 1
            continue

        returncode = CHILD.wait()
        runtime = time.monotonic() - started
        CHILD = None

        if STOP_REQUESTED:
            break

        restart_count += 1

        log(
            "Collector gestopt; alleen collector wordt herstart | "
            f"exit={returncode} | runtime={runtime:.1f}s | "
            f"restart_count={restart_count}"
        )

        if runtime >= 300:
            delay = RESTART_DELAY_SECONDS
        else:
            delay = min(
                max(RESTART_DELAY_SECONDS, delay * 2),
                MAX_RESTART_DELAY_SECONDS,
            )

        end_wait = time.monotonic() + delay
        while not STOP_REQUESTED and time.monotonic() < end_wait:
            time.sleep(
                min(
                    1.0,
                    max(0.0, end_wait - time.monotonic()),
                )
            )

    child = CHILD
    if child is not None and child.poll() is None:
        try:
            child.terminate()
            child.wait(timeout=10)
        except Exception:
            try:
                child.kill()
            except Exception:
                pass

    log("Early Entry Collector Runner gestopt")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def main() -> int:
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    args = parse_args()

    if args.self_test:
        self_test()
        return 0

    return run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
