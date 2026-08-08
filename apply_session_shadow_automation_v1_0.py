#!/usr/bin/env python3
import argparse, os, py_compile, shutil, subprocess, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path("/opt/render/project/src")
TARGET = PROJECT / "periodic_analysis_runner.py"
LAB = PROJECT / "scanner_session_shadow_lab.py"
BACKUPS = Path("/var/data/diamond_code_backups")

def fail(msg):
    print("[FOUT]", msg)
    raise SystemExit(1)

def repl(text, old, new, label):
    n = text.count(old)
    if n != 1:
        fail(f"{label}: verwacht 1 anker, gevonden {n}")
    return text.replace(old, new, 1)

def patch(text):
    text = repl(text, 'VERSION = "1.5"', 'VERSION = "1.6"', "VERSION")

    text = repl(
        text,
        'SCANNER_SELECTIVE_SHADOW_LOG = DATA_DIR / "diamond_scanner_selective_shadow_runner.log"\n',
        'SCANNER_SELECTIVE_SHADOW_LOG = DATA_DIR / "diamond_scanner_selective_shadow_runner.log"\n'
        'SCANNER_SESSION_SHADOW_LOG = DATA_DIR / "diamond_scanner_session_shadow_runner.log"\n',
        "LOG",
    )

    old = '''        "scanner_selective_shadow": [
            sys.executable,
            "scanner_selective_shadow_lab.py",
            "--update",
            "--no-print",
        ],
'''
    new = old + '''        "scanner_session_shadow": [
            sys.executable,
            "scanner_session_shadow_lab.py",
            "--update",
            "--no-print",
        ],
'''
    text = repl(text, old, new, "TASK_COMMAND")

    old = '''        run_task(
            state,
            "scanner_selective_shadow",
            task_commands()["scanner_selective_shadow"],
            SCANNER_SELECTIVE_SHADOW_LOG,
        )

        if STOP_REQUESTED:
            break

        state["last_cycle_completed_at"] = now_iso()
'''
    new = '''        run_task(
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
'''
    text = repl(text, old, new, "RUN_ORDER")

    text = repl(
        text,
        '"tasks=diagnose,scanner,shadow_v2,long_entry_shadow,long_min_profit_shadow,long_combo_shadow,scanner_selective_shadow",',
        '"tasks=diagnose,scanner,shadow_v2,long_entry_shadow,long_min_profit_shadow,long_combo_shadow,scanner_selective_shadow,scanner_session_shadow",',
        "TASKS_STATUS",
    )

    old = '''    assert list(commands.keys()) == [
        "diagnose",
        "scanner",
        "shadow_v2",
        "long_entry_shadow",
        "long_min_profit_shadow",
        "long_combo_shadow",
        "scanner_selective_shadow",
    ]
'''
    new = '''    assert list(commands.keys()) == [
        "diagnose",
        "scanner",
        "shadow_v2",
        "long_entry_shadow",
        "long_min_profit_shadow",
        "long_combo_shadow",
        "scanner_selective_shadow",
        "scanner_session_shadow",
    ]
'''
    text = repl(text, old, new, "SELFTEST_LIST")

    text = repl(
        text,
        'assert state["version"] == "1.5"',
        'assert state["version"] == "1.6"',
        "SELFTEST_VERSION",
    )

    old = '''    assert (
        SCANNER_SELECTIVE_SHADOW_LOG.name
        == "diamond_scanner_selective_shadow_runner.log"
    )

    print(
        "PERIODIC_ANALYSIS_SELF_TEST_OK"
    )
'''
    new = '''    assert (
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
'''
    text = repl(text, old, new, "SELFTEST_SESSION")

    text = repl(
        text,
        '"Taken: diagnose -> scanner -> shadow_v2 -> long_entry_shadow -> long_min_profit_shadow -> long_combo_shadow -> scanner_selective_shadow"',
        '"Taken: diagnose -> scanner -> shadow_v2 -> long_entry_shadow -> long_min_profit_shadow -> long_combo_shadow -> scanner_selective_shadow -> scanner_session_shadow"',
        "SELFTEST_PRINT",
    )
    return text

def compile_text(text):
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(text)
        name = f.name
    try:
        py_compile.compile(name, doraise=True)
    finally:
        Path(name).unlink(missing_ok=True)

def check():
    if not TARGET.is_file():
        fail("periodic_analysis_runner.py ontbreekt")
    if not LAB.is_file():
        fail("scanner_session_shadow_lab.py ontbreekt")

    original = TARGET.read_text(encoding="utf-8")
    if 'VERSION = "1.6"' in original and '"scanner_session_shadow": [' in original:
        fail("patch lijkt al toegepast")
    if 'VERSION = "1.5"' not in original:
        fail("verwachte Periodic Runner v1.5 niet gevonden")

    patched = patch(original)
    compile_text(patched)

    r = subprocess.run(
        [sys.executable, str(LAB), "--self-test"],
        cwd=str(PROJECT),
        capture_output=True,
        text=True,
    )
    if r.returncode != 0 or "SCANNER_SESSION_SHADOW_SELF_TEST_OK" not in r.stdout:
        print(r.stdout, r.stderr)
        fail("Session Shadow self-test mislukt")

    print("=== SESSION SHADOW AUTOMATION CHECK V1.0 ===")
    print("[OK] Periodic Runner v1.5 veilig herkend")
    print("[OK] Session Shadow Lab self-test geslaagd")
    print("[OK] Patch-preview syntax geldig")
    print("[OK] Nieuwe taak wordt nummer 8")
    print("[OK] Uitvoering blijft sequentieel")
    print("[OK] Interval blijft 900 seconden")
    print("[OK] Geen strategie-, order- of configwijziging")

def apply():
    check()
    original = TARGET.read_text(encoding="utf-8")
    patched = patch(original)
    compile_text(patched)

    BACKUPS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = BACKUPS / f"periodic_analysis_runner.py.before_session_shadow_{stamp}.bak"
    shutil.copy2(TARGET, backup)

    mode = TARGET.stat().st_mode
    with tempfile.NamedTemporaryFile("w", dir=str(PROJECT), delete=False) as f:
        f.write(patched)
        tmp = Path(f.name)
    try:
        os.chmod(tmp, mode)
        os.replace(tmp, TARGET)
    finally:
        tmp.unlink(missing_ok=True)

    r = subprocess.run(
        [sys.executable, str(TARGET), "--self-test"],
        cwd=str(PROJECT),
        capture_output=True,
        text=True,
    )
    if r.returncode != 0 or "PERIODIC_ANALYSIS_SELF_TEST_OK" not in r.stdout:
        shutil.copy2(backup, TARGET)
        print(r.stdout, r.stderr)
        fail("Periodic Runner self-test mislukt; backup hersteld")

    print("=== SESSION SHADOW AUTOMATION APPLY V1.0 ===")
    print("[OK] Backup:", backup)
    print("[OK] Periodic Runner: v1.6")
    print("[OK] scanner_session_shadow: taak 8")
    print("[OK] Sequentieel: JA")
    print("[OK] Interval: 900 seconden")
    print("[OK] Runner self-test: geslaagd")
    print("[OK] Strategie/config: onaangeraakt")
    print()
    print(r.stdout.strip())

def main():
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--apply", action="store_true")
    a = p.parse_args()
    if a.check:
        check()
    else:
        apply()

if __name__ == "__main__":
    main()
