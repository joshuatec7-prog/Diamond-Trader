#!/usr/bin/env python3
"""
Diamond Trader - LONG Shadow latest-500 updater v1.1

Doel:
- Repareert het vaste OHLCV-venster in drie read-only LONG Shadow Labs.
- De labs halen daarna steeds de nieuwste 500 candles op.
- Bestaande shadow-state/resultaten blijven behouden.
- Strategie, inzet, orders en config blijven onaangeraakt.

Gebruik:
  python3 apply_long_shadow_latest500_v1_1.py --check
  python3 apply_long_shadow_latest500_v1_1.py --apply
"""

from __future__ import annotations

import argparse
import py_compile
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path("/opt/render/project/src")
BACKUP_DIR = Path("/var/data/diamond_code_backups")
MARKER = "LONG_SHADOW_LATEST_500_V1"

TARGETS = [
    PROJECT_DIR / "long_entry_shadow_lab.py",
    PROJECT_DIR / "long_min_profit_shadow_lab.py",
    PROJECT_DIR / "long_combo_shadow_lab.py",
]

OLD_BLOCKS = {
    "long_entry_shadow_lab.py": """    # Maximaal genoeg terug voor indicatoren + 48h testhistorie.
    since_ms = int(
        (
            baseline_dt.timestamp()
            - 36 * 60 * 60
        )
        * 1000
    )

""",
    "long_min_profit_shadow_lab.py": """    # Genoeg terug voor indicatoren en maximaal 48h virtuele posities.
    since_ms = int(
        (
            baseline_dt.timestamp()
            - 36 * 60 * 60
        )
        * 1000
    )

""",
    "long_combo_shadow_lab.py": """    since_ms = int(
        (
            baseline_dt.timestamp()
            - 36 * 60 * 60
        )
        * 1000
    )

""",
}

NEW_BLOCK = """    # LONG_SHADOW_LATEST_500_V1
    # Gebruik altijd de nieuwste 500 15m-candles.
    # Dat is ongeveer 125 uur actuele historie en ruim voldoende
    # voor indicatoren plus maximaal 48 uur virtuele positiehistorie.
    _ = baseline_dt

"""

SINCE_ARG_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)since_ms=since_ms,[ \t]*\n"
)


def read_text(path: Path) -> str:
    if not path.exists():
        raise SystemExit(f"FOUT: ontbreekt: {path}")
    return path.read_text(encoding="utf-8")


def validate(path: Path, text: str) -> list[str]:
    if MARKER in text:
        return []

    problems = []
    old_block = OLD_BLOCKS[path.name]

    block_count = text.count(old_block)
    since_arg_count = len(SINCE_ARG_RE.findall(text))

    if block_count != 1:
        problems.append(
            f"{path.name}: since-blok verwacht 1x, gevonden {block_count}x"
        )

    if since_arg_count != 1:
        problems.append(
            f"{path.name}: since_ms argument verwacht 1x, gevonden {since_arg_count}x"
        )

    return problems


def patch(path: Path, text: str) -> str:
    if MARKER in text:
        return text

    text = text.replace(OLD_BLOCKS[path.name], NEW_BLOCK, 1)
    text, count = SINCE_ARG_RE.subn("", text, count=1)

    if count != 1:
        raise RuntimeError(
            f"{path.name}: since_ms argument kon niet exact 1x worden verwijderd"
        )

    return text


def verify(path: Path, text: str) -> list[str]:
    problems = []

    if MARKER not in text:
        problems.append(f"{path.name}: marker ontbreekt")

    if "since_ms=since_ms" in text:
        problems.append(f"{path.name}: oude since_ms-fetch staat nog aanwezig")

    if "limit=500" not in text:
        problems.append(f"{path.name}: limit=500 ontbreekt")

    return problems


def do_check() -> int:
    print("=== LONG SHADOW LATEST-500 CHECK V1.1 ===")
    failed = False

    for path in TARGETS:
        text = read_text(path)

        if MARKER in text:
            problems = verify(path, text)
            if problems:
                failed = True
                for problem in problems:
                    print(f"[FOUT] {problem}")
            else:
                print(f"[OK] {path.name}: patch al aanwezig")
            continue

        problems = validate(path, text)
        if problems:
            failed = True
            for problem in problems:
                print(f"[FOUT] {problem}")
            continue

        try:
            preview = patch(path, text)
        except Exception as exc:
            failed = True
            print(f"[FOUT] {path.name}: preview mislukt: {exc}")
            continue

        problems = verify(path, preview)
        if problems:
            failed = True
            for problem in problems:
                print(f"[FOUT] preview {problem}")
        else:
            print(f"[OK] {path.name}: veilig herkend")

    if failed:
        return 1

    print()
    print("[OK] Alle 3 LONG Shadow Labs veilig herkenbaar.")
    print("[OK] limit blijft 500 candles.")
    print("[OK] Alleen het vaste oude since_ms-startpunt vervalt.")
    print("[OK] Opmaakverschillen van fetch_frame worden ondersteund.")
    print("[OK] Bestaande state/resultaten blijven behouden.")
    print("[OK] Strategie, inzet, orders en config blijven onaangeraakt.")
    return 0


def do_apply() -> int:
    current = {}

    for path in TARGETS:
        text = read_text(path)
        current[path] = text

        if MARKER not in text:
            problems = validate(path, text)
            if problems:
                for problem in problems:
                    print(f"[FOUT] {problem}")
                return 1

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    changed = []

    for path, text in current.items():
        if MARKER in text:
            continue

        backup = BACKUP_DIR / f"{path.name}.before_latest500_{stamp}.bak"
        shutil.copy2(path, backup)

        try:
            new_text = patch(path, text)
        except Exception as exc:
            print(f"[FOUT] {path.name}: patch mislukt: {exc}")
            return 1

        problems = verify(path, new_text)
        if problems:
            for problem in problems:
                print(f"[FOUT] {problem}")
            return 1

        path.write_text(new_text, encoding="utf-8")
        changed.append((path, backup))

    try:
        for path in TARGETS:
            py_compile.compile(str(path), doraise=True)
    except Exception as exc:
        print(f"[FOUT] Python syntaxcontrole mislukt: {exc}")
        return 1

    print("=== LONG SHADOW LATEST-500 APPLY V1.1 ===")
    for path, backup in changed:
        print(f"[OK] {path.name}")
        print(f"     backup: {backup}")

    if not changed:
        print("[OK] Patch was al aanwezig.")

    print()
    print("[OK] Python syntax geldig voor alle 3 bestanden.")
    print("[OK] Nieuwste 500 candles worden gebruikt.")
    print("[OK] Bestaande shadow-state is niet gereset.")
    print("[OK] Strategie/orders/config zijn onaangeraakt.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if args.check:
        return do_check()

    return do_apply()


if __name__ == "__main__":
    raise SystemExit(main())
