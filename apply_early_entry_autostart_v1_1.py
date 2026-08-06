#!/usr/bin/env python3
"""
Diamond Trader - Early Entry autostart updater v1.1

Past uitsluitend start.sh aan zodat:
- early_entry_collector_runner.py automatisch wordt gestart;
- de runner in PIDS komt;
- de echte collector NIET rechtstreeks in PIDS komt;
- als alleen de collector stopt, wait -n de worker dus niet herstart.

Gebruik:
  python3 apply_early_entry_autostart_v1_1.py --check
  python3 apply_early_entry_autostart_v1_1.py --apply
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

MARKER = "# EARLY_ENTRY_AUTOSTART_V1"
OLD_HEADER = "# Diamond Trader startscript v2.1"
NEW_HEADER = "# Diamond Trader startscript v2.2"

LOG_LINE = 'STRATEGY_LAB_LOG="$DATA_DIR/diamond_strategy_lab_runner.log"'
EARLY_LOG_LINE = 'EARLY_ENTRY_LOG="$DATA_DIR/diamond_early_entry/collector_v1_2_runner.log"'

MKDIR_LINE = 'mkdir -p "$DATA_DIR"'
EARLY_MKDIR_LINE = 'mkdir -p "$DATA_DIR/diamond_early_entry"'

PERIODIC_LOG_ECHO = 'echo "        Log: $PERIODIC_LOG"'

EARLY_BLOCK = r'''
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
'''.strip("\n")

COMMENT_LINE = "# - Diagnose en Scanner draaien nooit tegelijk."
COMMENT_EXTRA = (
    "# - Early Entry Collector v1.2 verzamelt alleen publieke marktdata.\n"
    "# - De aparte Early Entry Runner herstart alleen de collector als die stopt."
)


def read_start() -> str:
    if not START_FILE.exists():
        raise SystemExit(f"FOUT: {START_FILE} ontbreekt")
    return START_FILE.read_text(encoding="utf-8")


def installed(text: str) -> bool:
    return MARKER in text


def occurrences(text: str, needle: str) -> int:
    return text.count(needle)


def validate(text: str) -> list[str]:
    problems: list[str] = []

    if installed(text):
        return problems

    checks = [
        ("startscript-header v2.1", OLD_HEADER, 1),
        ("Strategy Lab logregel", LOG_LINE, 1),
        ("DATA_DIR mkdir", MKDIR_LINE, 1),
        ("Periodic logregel", PERIODIC_LOG_ECHO, 1),
        ("geheugenarm commentaar", COMMENT_LINE, 1),
    ]

    for label, needle, expected in checks:
        count = occurrences(text, needle)
        if count != expected:
            problems.append(
                f"{label}: verwacht {expected} keer, gevonden {count}"
            )

    if not RUNNER_FILE.exists():
        problems.append(f"runner ontbreekt: {RUNNER_FILE.name}")

    if not COLLECTOR_FILE.exists():
        problems.append(f"collector ontbreekt: {COLLECTOR_FILE.name}")

    return problems


def patch(text: str) -> str:
    if installed(text):
        return text

    result = text.replace(OLD_HEADER, NEW_HEADER, 1)
    result = result.replace(
        COMMENT_LINE,
        COMMENT_LINE + "\n" + COMMENT_EXTRA,
        1,
    )
    result = result.replace(
        LOG_LINE,
        LOG_LINE + "\n" + EARLY_LOG_LINE,
        1,
    )
    result = result.replace(
        MKDIR_LINE,
        MKDIR_LINE + "\n" + EARLY_MKDIR_LINE,
        1,
    )
    result = result.replace(
        PERIODIC_LOG_ECHO,
        PERIODIC_LOG_ECHO + "\n\n" + EARLY_BLOCK,
        1,
    )
    return result


def bash_syntax_check(path: Path) -> None:
    result = subprocess.run(
        ["bash", "-n", str(path)],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "bash -n mislukt:\n" + result.stdout + result.stderr
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
            "runner self-test mislukt:\n" + result.stdout + result.stderr
        )


def verify_patched(text: str) -> list[str]:
    problems: list[str] = []

    if MARKER not in text:
        problems.append("autostart marker ontbreekt")
    if NEW_HEADER not in text:
        problems.append("start.sh header is niet v2.2")
    if EARLY_LOG_LINE not in text:
        problems.append("Early Entry logvariabele ontbreekt")
    if EARLY_MKDIR_LINE not in text:
        problems.append("Early Entry datamap ontbreekt")
    if "python3 early_entry_collector_runner.py \\" not in text:
        problems.append("runner-startregel ontbreekt")
    if 'PIDS+=("$EARLY_ENTRY_RUNNER_PID")' not in text:
        problems.append("runner staat niet in PIDS")

    direct_collector_lines = [
        line for line in text.splitlines()
        if line.strip().startswith("python3 early_entry_collector_v1_2.py")
    ]
    if direct_collector_lines:
        problems.append("collector wordt rechtstreeks door start.sh gestart")

    return problems


def show_check(text: str) -> int:
    print("=== DIAMOND EARLY ENTRY AUTOSTART CHECK V1.1 ===")
    print(f"Bestand                : {START_FILE.name}")
    print(f"Autostart al aanwezig  : {'JA' if installed(text) else 'NEE'}")
    print(f"Runner aanwezig        : {'JA' if RUNNER_FILE.exists() else 'NEE'}")
    print(f"Collector v1.2 aanwezig: {'JA' if COLLECTOR_FILE.exists() else 'NEE'}")

    if installed(text):
        patched_problems = verify_patched(text)
        if patched_problems:
            for problem in patched_problems:
                print(f"[FOUT] {problem}")
            return 1
        print("[OK] Early Entry autostart is volledig aanwezig.")
        return 0

    problems = validate(text)
    if problems:
        for problem in problems:
            print(f"[FOUT] {problem}")
        return 1

    preview = patch(text)
    preview_problems = verify_patched(preview)
    if preview_problems:
        for problem in preview_problems:
            print(f"[FOUT] preview: {problem}")
        return 1

    print("[OK] start.sh v2.1 veilig herkend.")
    print("[OK] Patch-preview volledig gevalideerd.")
    print("[OK] Alleen start.sh zal worden aangepast.")
    print("[OK] Runner komt in PIDS/wait -n.")
    print("[OK] Collector zelf komt NIET rechtstreeks in PIDS/wait -n.")
    print("[OK] Geen strategie-, order- of configwijziging.")
    return 0


def apply_patch(text: str) -> int:
    if installed(text):
        print("EARLY_ENTRY_AUTOSTART_ALREADY_INSTALLED")
        return show_check(text)

    problems = validate(text)
    if problems:
        for problem in problems:
            print(f"[FOUT] {problem}")
        return 1

    runner_self_test()
    new_text = patch(text)
    patched_problems = verify_patched(new_text)
    if patched_problems:
        for problem in patched_problems:
            print(f"[FOUT] patch: {problem}")
        return 1

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = BACKUP_DIR / f"start.sh.before_early_entry_autostart_{stamp}.bak"
    shutil.copy2(START_FILE, backup)

    original_mode = stat.S_IMODE(START_FILE.stat().st_mode)
    tmp = START_FILE.with_suffix(".sh.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.chmod(tmp, original_mode)

    try:
        bash_syntax_check(tmp)
        os.replace(tmp, START_FILE)
        os.chmod(START_FILE, original_mode)
        bash_syntax_check(START_FILE)
    finally:
        if tmp.exists():
            tmp.unlink()

    print("=== DIAMOND EARLY ENTRY AUTOSTART APPLY V1.1 ===")
    print(f"[OK] Backup               : {backup}")
    print("[OK] start.sh              : v2.2")
    print("[OK] Early Entry Runner    : autostart")
    print("[OK] Runner in PIDS        : JA")
    print("[OK] Collector direct PIDS : NEE")
    print("[OK] bash syntax           : geldig")
    print("[OK] Strategie/orders      : onaangeraakt")
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
