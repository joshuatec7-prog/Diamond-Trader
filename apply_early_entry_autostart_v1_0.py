#!/usr/bin/env python3
"""
Diamond Trader - Early Entry autostart updater v1.0

Past uitsluitend start.sh aan zodat:
- early_entry_collector_runner.py automatisch wordt gestart;
- de runner in PIDS komt;
- de echte collector NIET rechtstreeks in PIDS komt;
- als alleen de collector stopt, wait -n de worker dus niet herstart.

Gebruik:
  python3 apply_early_entry_autostart_v1_0.py --check
  python3 apply_early_entry_autostart_v1_0.py --apply
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path("/opt/render/project/src")
START_FILE = PROJECT_DIR / "start.sh"
RUNNER_FILE = PROJECT_DIR / "early_entry_collector_runner.py"
COLLECTOR_FILE = PROJECT_DIR / "early_entry_collector_v1_2.py"
BACKUP_DIR = Path("/var/data/diamond_code_backups")

MARKER = "EARLY_ENTRY_AUTOSTART_V1"

OLD_HEADER = "# Diamond Trader startscript v2.1"
NEW_HEADER = "# Diamond Trader startscript v2.2"

LOG_ANCHOR = """PERIODIC_LOG="$DATA_DIR/diamond_periodic_analysis_runner.log"
STRATEGY_LAB_LOG="$DATA_DIR/diamond_strategy_lab_runner.log"
"""

LOG_REPLACEMENT = """PERIODIC_LOG="$DATA_DIR/diamond_periodic_analysis_runner.log"
STRATEGY_LAB_LOG="$DATA_DIR/diamond_strategy_lab_runner.log"
EARLY_ENTRY_LOG="$DATA_DIR/diamond_early_entry/collector_v1_2_runner.log"
"""

MKDIR_ANCHOR = """cd "$PROJECT_DIR"
mkdir -p "$DATA_DIR"
"""

MKDIR_REPLACEMENT = """cd "$PROJECT_DIR"
mkdir -p "$DATA_DIR"
mkdir -p "$DATA_DIR/diamond_early_entry"
"""

PROCESS_ANCHOR = """echo "[START] Diamond Periodieke Analyse"
python3 periodic_analysis_runner.py \
    >> "$PERIODIC_LOG" 2>&1 &
PERIODIC_PID=$!
PIDS+=("$PERIODIC_PID")
echo "        PID $PERIODIC_PID"
echo "        Diagnose + Scanner: sequentieel iedere 15 minuten"
echo "        Log: $PERIODIC_LOG"

echo
echo "[OK] Alle Diamond Trader-hoofdprocessen zijn gestart."
"""

PROCESS_REPLACEMENT = """echo "[START] Diamond Periodieke Analyse"
python3 periodic_analysis_runner.py \
    >> "$PERIODIC_LOG" 2>&1 &
PERIODIC_PID=$!
PIDS+=("$PERIODIC_PID")
echo "        PID $PERIODIC_PID"
echo "        Diagnose + Scanner: sequentieel iedere 15 minuten"
echo "        Log: $PERIODIC_LOG"

# EARLY_ENTRY_AUTOSTART_V1
# De runner blijft permanent actief en bewaakt alleen de collector.
# Daardoor staat de collector zelf NIET rechtstreeks in PIDS/wait -n.
echo "[START] Diamond Early Entry Collector"
python3 early_entry_collector_runner.py \
    >> "$EARLY_ENTRY_LOG" 2>&1 &
EARLY_ENTRY_RUNNER_PID=$!
PIDS+=("$EARLY_ENTRY_RUNNER_PID")
echo "        Runner PID $EARLY_ENTRY_RUNNER_PID"
echo "        Collector: early_entry_collector_v1_2.py"
echo "        Transport: publieke REST-only"
echo "        Log: $EARLY_ENTRY_LOG"

echo
echo "[OK] Alle Diamond Trader-hoofdprocessen zijn gestart."
"""

COMMENT_ANCHOR = "# - Diagnose en Scanner draaien nooit tegelijk.\n"

COMMENT_REPLACEMENT = """# - Diagnose en Scanner draaien nooit tegelijk.
# - Early Entry Collector v1.2 verzamelt alleen publieke marktdata.
# - De aparte Early Entry Runner herstart alleen de collector als die stopt.
"""


def read_start() -> str:
    if not START_FILE.exists():
        raise SystemExit(f"FOUT: {START_FILE} ontbreekt")
    return START_FILE.read_text(encoding="utf-8")


def already_installed(text: str) -> bool:
    return MARKER in text


def validate_target(text: str) -> list[str]:
    problems: list[str] = []

    if already_installed(text):
        return problems

    for label, anchor in [
        ("header", OLD_HEADER),
        ("logblok", LOG_ANCHOR),
        ("mkdir-blok", MKDIR_ANCHOR),
        ("procesblok", PROCESS_ANCHOR),
        ("commentaarblok", COMMENT_ANCHOR),
    ]:
        if anchor not in text:
            problems.append(f"anker ontbreekt: {label}")

    if not RUNNER_FILE.exists():
        problems.append(f"runner ontbreekt: {RUNNER_FILE.name}")

    if not COLLECTOR_FILE.exists():
        problems.append(f"collector ontbreekt: {COLLECTOR_FILE.name}")

    return problems


def patched_text(text: str) -> str:
    if already_installed(text):
        return text

    result = text
    result = result.replace(OLD_HEADER, NEW_HEADER, 1)
    result = result.replace(COMMENT_ANCHOR, COMMENT_REPLACEMENT, 1)
    result = result.replace(LOG_ANCHOR, LOG_REPLACEMENT, 1)
    result = result.replace(MKDIR_ANCHOR, MKDIR_REPLACEMENT, 1)
    result = result.replace(PROCESS_ANCHOR, PROCESS_REPLACEMENT, 1)
    return result


def syntax_check(path: Path) -> None:
    result = subprocess.run(
        ["bash", "-n", str(path)],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "bash -n mislukt:\n"
            + result.stdout
            + result.stderr
        )


def runner_self_test() -> None:
    result = subprocess.run(
        [sys.executable, str(RUNNER_FILE), "--self-test"],
        cwd=str(PROJECT_DIR),
        text=True,
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "runner self-test mislukt:\n"
            + result.stdout
            + result.stderr
        )


def show_check(text: str) -> int:
    print("=== DIAMOND EARLY ENTRY AUTOSTART CHECK ===")
    print(f"Bestand               : {START_FILE.name}")
    print(
        "Autostart al aanwezig:",
        "JA" if already_installed(text) else "NEE",
    )
    print(
        "Runner aanwezig       :",
        "JA" if RUNNER_FILE.exists() else "NEE",
    )
    print(
        "Collector v1.2 aanwezig:",
        "JA" if COLLECTOR_FILE.exists() else "NEE",
    )

    if already_installed(text):
        print("[OK] Early Entry autostart is al aanwezig.")
        return 0

    problems = validate_target(text)

    if problems:
        for problem in problems:
            print(f"[FOUT] {problem}")
        return 1

    print("[OK] start.sh v2.1 exact herkend.")
    print("[OK] Alleen start.sh zal worden aangepast.")
    print("[OK] Collector zelf komt NIET rechtstreeks in PIDS/wait -n.")
    print("[OK] Runner bewaakt alleen early_entry_collector_v1_2.py.")
    print("[OK] Geen strategie-, order- of configwijziging.")
    return 0


def apply_patch(text: str) -> int:
    if already_installed(text):
        print("EARLY_ENTRY_AUTOSTART_ALREADY_INSTALLED")
        return 0

    problems = validate_target(text)
    if problems:
        for problem in problems:
            print(f"[FOUT] {problem}")
        return 1

    runner_self_test()

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = BACKUP_DIR / f"start.sh.before_early_entry_autostart_{stamp}.bak"
    shutil.copy2(START_FILE, backup)

    original_mode = stat.S_IMODE(START_FILE.stat().st_mode)
    new_text = patched_text(text)

    tmp = START_FILE.with_suffix(".sh.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.chmod(tmp, original_mode)

    try:
        syntax_check(tmp)
        os.replace(tmp, START_FILE)
        os.chmod(START_FILE, original_mode)
        syntax_check(START_FILE)
    finally:
        if tmp.exists():
            tmp.unlink()

    print("=== DIAMOND EARLY ENTRY AUTOSTART APPLY ===")
    print(f"[OK] Backup              : {backup}")
    print("[OK] start.sh             : v2.2")
    print("[OK] Early Entry Runner   : autostart")
    print("[OK] Collector direct PIDS: NEE")
    print("[OK] wait -n gedrag       : beschermd via runner")
    print("[OK] bash syntax          : geldig")
    print("[OK] Strategie/orders     : onaangeraakt")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    text = read_start()

    if args.check:
        return show_check(text)

    return apply_patch(text)


if __name__ == "__main__":
    raise SystemExit(main())
