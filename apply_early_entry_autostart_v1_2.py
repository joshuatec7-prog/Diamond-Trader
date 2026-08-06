#!/usr/bin/env python3
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
RUNNER_FILE = PROJECT_DIR / "early_entry_collector_runner_v1_1.py"
COLLECTOR_FILE = PROJECT_DIR / "early_entry_collector_v1_3_1.py"
BACKUP_DIR = Path("/var/data/diamond_code_backups")

MARKER = "# EARLY_ENTRY_AUTOSTART_V1_3_1"
OLD_HEADER = "# Diamond Trader startscript v2.1"
NEW_HEADER = "# Diamond Trader startscript v2.2"

COMMENT_LINE = "# - Diagnose en Scanner draaien nooit tegelijk."
COMMENT_EXTRA = (
    "# - Early Entry Collector v1.3.1 verzamelt alleen publieke marktdata.\n"
    "# - De aparte Early Entry Runner v1.1 herstart alleen de collector als die stopt."
)

LOG_LINE = 'STRATEGY_LAB_LOG="$DATA_DIR/diamond_strategy_lab_runner.log"'
EARLY_LOG_LINE = (
    'EARLY_ENTRY_LOG="$DATA_DIR/diamond_early_entry/'
    'collector_v1_3_1_runner.log"'
)

MKDIR_LINE = 'mkdir -p "$DATA_DIR"'
EARLY_MKDIR_LINE = 'mkdir -p "$DATA_DIR/diamond_early_entry"'
PERIODIC_LOG_ECHO = 'echo "        Log: $PERIODIC_LOG"'

EARLY_BLOCK = '''# EARLY_ENTRY_AUTOSTART_V1_3_1
# De runner blijft permanent actief en bewaakt alleen collector v1.3.1.
# Daardoor staat de collector zelf NIET rechtstreeks in PIDS/wait -n.
echo "[START] Diamond Early Entry Collector"
python3 early_entry_collector_runner_v1_1.py \\
    >> "$EARLY_ENTRY_LOG" 2>&1 &
EARLY_ENTRY_RUNNER_PID=$!
PIDS+=("$EARLY_ENTRY_RUNNER_PID")
echo "        Runner PID $EARLY_ENTRY_RUNNER_PID"
echo "        Runner: early_entry_collector_runner_v1_1.py"
echo "        Collector: early_entry_collector_v1_3_1.py"
echo "        Transport: publieke native REST"
echo "        Log: $EARLY_ENTRY_LOG"'''


def read_start() -> str:
    if not START_FILE.exists():
        raise SystemExit(f"FOUT: {START_FILE} ontbreekt")
    return START_FILE.read_text(encoding="utf-8")


def installed(text: str) -> bool:
    return MARKER in text


def validate_target(text: str) -> list[str]:
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
        found = text.count(needle)
        if found != expected:
            problems.append(
                f"{label}: verwacht {expected} keer, gevonden {found}"
            )

    if not RUNNER_FILE.exists():
        problems.append(f"runner ontbreekt: {RUNNER_FILE.name}")

    if not COLLECTOR_FILE.exists():
        problems.append(f"collector ontbreekt: {COLLECTOR_FILE.name}")

    return problems


def patch_text(text: str) -> str:
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


def verify_patched(text: str) -> list[str]:
    problems: list[str] = []

    if MARKER not in text:
        problems.append("autostart-marker ontbreekt")

    if NEW_HEADER not in text:
        problems.append("start.sh header is niet v2.2")

    if EARLY_LOG_LINE not in text:
        problems.append("Early Entry logvariabele ontbreekt")

    if EARLY_MKDIR_LINE not in text:
        problems.append("Early Entry datamap ontbreekt")

    if "python3 early_entry_collector_runner_v1_1.py \\" not in text:
        problems.append("runner-startregel ontbreekt")

    if 'PIDS+=("$EARLY_ENTRY_RUNNER_PID")' not in text:
        problems.append("runner staat niet in PIDS")

    direct_collector = [
        line for line in text.splitlines()
        if line.strip().startswith("python3 early_entry_collector_v1_3_1.py")
    ]
    if direct_collector:
        problems.append("collector wordt rechtstreeks door start.sh gestart")

    return problems


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
            "runner self-test mislukt:\n"
            + result.stdout
            + result.stderr
        )


def do_check(text: str) -> int:
    print("=== EARLY ENTRY V1.3.1 AUTOSTART CHECK V1.2 ===")
    print(f"Bestand                  : {START_FILE.name}")
    print(f"Autostart al aanwezig    : {'JA' if installed(text) else 'NEE'}")
    print(f"Runner v1.1 aanwezig     : {'JA' if RUNNER_FILE.exists() else 'NEE'}")
    print(f"Collector v1.3.1 aanwezig: {'JA' if COLLECTOR_FILE.exists() else 'NEE'}")

    if installed(text):
        problems = verify_patched(text)
        if problems:
            for problem in problems:
                print(f"[FOUT] {problem}")
            return 1
        print("[OK] Early Entry v1.3.1 autostart is volledig aanwezig.")
        return 0

    problems = validate_target(text)
    if problems:
        for problem in problems:
            print(f"[FOUT] {problem}")
        return 1

    preview = patch_text(text)
    problems = verify_patched(preview)
    if problems:
        for problem in problems:
            print(f"[FOUT] preview: {problem}")
        return 1

    runner_self_test()

    print("[OK] start.sh v2.1 veilig herkend.")
    print("[OK] Runner v1.1 self-test geslaagd.")
    print("[OK] Patch-preview volledig gevalideerd.")
    print("[OK] Runner komt in PIDS/wait -n.")
    print("[OK] Collector v1.3.1 komt NIET rechtstreeks in PIDS/wait -n.")
    print("[OK] Geen strategie-, order- of configwijziging.")
    return 0


def do_apply(text: str) -> int:
    if installed(text):
        print("EARLY_ENTRY_V1_3_1_AUTOSTART_ALREADY_INSTALLED")
        return do_check(text)

    problems = validate_target(text)
    if problems:
        for problem in problems:
            print(f"[FOUT] {problem}")
        return 1

    runner_self_test()

    new_text = patch_text(text)
    problems = verify_patched(new_text)
    if problems:
        for problem in problems:
            print(f"[FOUT] patch: {problem}")
        return 1

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = BACKUP_DIR / (
        f"start.sh.before_early_entry_v1_3_1_autostart_{stamp}.bak"
    )
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

    print("=== EARLY ENTRY V1.3.1 AUTOSTART APPLY V1.2 ===")
    print(f"[OK] Backup                 : {backup}")
    print("[OK] start.sh                : v2.2")
    print("[OK] Runner v1.1 autostart   : JA")
    print("[OK] Runner in PIDS          : JA")
    print("[OK] Collector direct in PIDS: NEE")
    print("[OK] bash syntax             : geldig")
    print("[OK] Strategie/orders/config : onaangeraakt")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    text = read_start()
    if args.check:
        return do_check(text)
    return do_apply(text)


if __name__ == "__main__":
    raise SystemExit(main())
