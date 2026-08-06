#!/usr/bin/env python3
from pathlib import Path
import os
import shutil
import stat
import subprocess
import tempfile

PROJECT = Path("/opt/render/project/src")
RUNNER = PROJECT / "periodic_analysis_runner.py"
HEALTH = PROJECT / "healthcheck.sh"
LAB = PROJECT / "scanner_selective_shadow_lab.py"
RUNNER_BAK = PROJECT / "periodic_analysis_runner_v1_4_backup.py"
HEALTH_BAK = PROJECT / "healthcheck_v7_13_backup.sh"


def fail(msg):
    raise SystemExit("STOP: " + msg)


def atomic_write(path, text, mode):
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False
    ) as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    os.chmod(tmp_path, mode)
    os.replace(tmp_path, path)


for p in (RUNNER, HEALTH, LAB):
    if not p.is_file():
        fail(f"{p.name} ontbreekt")

r = RUNNER.read_text(encoding="utf-8")
h = HEALTH.read_text(encoding="utf-8")

if 'VERSION = "1.5"' in r and "# Diamond Trader Healthcheck v7.14" in h:
    print("SELECTIVE_SHADOW_AUTOMATION_ALREADY_INSTALLED")
    raise SystemExit(0)

if 'VERSION = "1.4"' not in r:
    fail("periodic_analysis_runner.py is niet v1.4")
if "# Diamond Trader Healthcheck v7.13" not in h:
    fail("healthcheck.sh is niet v7.13")

if not RUNNER_BAK.exists():
    shutil.copy2(RUNNER, RUNNER_BAK)
if not HEALTH_BAK.exists():
    shutil.copy2(HEALTH, HEALTH_BAK)

# Runner v1.4 -> v1.5
r = r.replace(
    "Diamond Trader Periodic Analysis Runner v1.4",
    "Diamond Trader Periodic Analysis Runner v1.5",
    1,
)
r = r.replace('VERSION = "1.4"', 'VERSION = "1.5"', 1)
r = r.replace(
    'assert state["version"] == "1.4"',
    'assert state["version"] == "1.5"',
    1,
)
r = r.replace(
    "Diamond Periodic Analysis Runner v1.4 gestart",
    "Diamond Periodic Analysis Runner v1.5 gestart",
    1,
)

marker = "6. LONG Combo Shadow Lab: vergelijkt CURRENT / WAIT30_100 / WAIT30_050.\n"
if marker not in r:
    fail("runner doc-marker ontbreekt")
r = r.replace(
    marker,
    marker
    + "7. Scanner Selective Shadow Lab: vergelijkt CURRENT / SELECTIVE / STRONG.\n",
    1,
)
r = r.replace(
    "Alle zes taken draaien strikt na elkaar en nooit tegelijk.",
    "Alle zeven taken draaien strikt na elkaar en nooit tegelijk.",
    1,
)

marker = 'LONG_COMBO_SHADOW_LOG = DATA_DIR / "diamond_long_combo_shadow_runner.log"\n'
if marker not in r:
    fail("runner log-marker ontbreekt")
r = r.replace(
    marker,
    marker
    + 'SCANNER_SELECTIVE_SHADOW_LOG = DATA_DIR / "diamond_scanner_selective_shadow_runner.log"\n',
    1,
)

old_cmd = (
    '        "long_combo_shadow": [\n'
    '            sys.executable,\n'
    '            "long_combo_shadow_lab.py",\n'
    '            "--update",\n'
    '            "--no-print",\n'
    '        ],\n'
)
new_cmd = old_cmd + (
    '        "scanner_selective_shadow": [\n'
    '            sys.executable,\n'
    '            "scanner_selective_shadow_lab.py",\n'
    '            "--update",\n'
    '            "--no-print",\n'
    '        ],\n'
)
if old_cmd not in r:
    fail("runner command-marker ontbreekt")
r = r.replace(old_cmd, new_cmd, 1)

old_run = (
    '        run_task(\n'
    '            state,\n'
    '            "long_combo_shadow",\n'
    '            task_commands()["long_combo_shadow"],\n'
    '            LONG_COMBO_SHADOW_LOG,\n'
    '        )\n\n'
    '        if STOP_REQUESTED:\n'
    '            break\n\n'
    '        state["last_cycle_completed_at"] = now_iso()\n'
)
new_run = (
    '        run_task(\n'
    '            state,\n'
    '            "long_combo_shadow",\n'
    '            task_commands()["long_combo_shadow"],\n'
    '            LONG_COMBO_SHADOW_LOG,\n'
    '        )\n\n'
    '        if STOP_REQUESTED:\n'
    '            break\n\n'
    '        run_task(\n'
    '            state,\n'
    '            "scanner_selective_shadow",\n'
    '            task_commands()["scanner_selective_shadow"],\n'
    '            SCANNER_SELECTIVE_SHADOW_LOG,\n'
    '        )\n\n'
    '        if STOP_REQUESTED:\n'
    '            break\n\n'
    '        state["last_cycle_completed_at"] = now_iso()\n'
)
if old_run not in r:
    fail("runner run-marker ontbreekt")
r = r.replace(old_run, new_run, 1)

old_list = (
    '        "long_min_profit_shadow",\n'
    '        "long_combo_shadow",\n'
    '    ]\n'
)
new_list = (
    '        "long_min_profit_shadow",\n'
    '        "long_combo_shadow",\n'
    '        "scanner_selective_shadow",\n'
    '    ]\n'
)
if old_list not in r:
    fail("runner selftest-lijstmarker ontbreekt")
r = r.replace(old_list, new_list, 1)

old_assert = (
    '    assert (\n'
    '        LONG_COMBO_SHADOW_LOG.name\n'
    '        == "diamond_long_combo_shadow_runner.log"\n'
    '    )\n\n'
    '    print(\n'
    '        "PERIODIC_ANALYSIS_SELF_TEST_OK"\n'
    '    )\n'
)
new_assert = (
    '    assert (\n'
    '        LONG_COMBO_SHADOW_LOG.name\n'
    '        == "diamond_long_combo_shadow_runner.log"\n'
    '    )\n\n'
    '    assert (\n'
    '        state["tasks"]["scanner_selective_shadow"]["command"][-3:]\n'
    '        == [\n'
    '            "scanner_selective_shadow_lab.py",\n'
    '            "--update",\n'
    '            "--no-print",\n'
    '        ]\n'
    '    )\n\n'
    '    assert (\n'
    '        SCANNER_SELECTIVE_SHADOW_LOG.name\n'
    '        == "diamond_scanner_selective_shadow_runner.log"\n'
    '    )\n\n'
    '    print(\n'
    '        "PERIODIC_ANALYSIS_SELF_TEST_OK"\n'
    '    )\n'
)
if old_assert not in r:
    fail("runner selftest-assertmarker ontbreekt")
r = r.replace(old_assert, new_assert, 1)

old_tasks_text = (
    "Taken: diagnose -> scanner -> shadow_v2 -> long_entry_shadow -> "
    "long_min_profit_shadow -> long_combo_shadow"
)
r = r.replace(
    old_tasks_text,
    old_tasks_text + " -> scanner_selective_shadow",
    1,
)
r = r.replace(
    "tasks=diagnose,scanner,shadow_v2,long_entry_shadow,long_min_profit_shadow,long_combo_shadow",
    "tasks=diagnose,scanner,shadow_v2,long_entry_shadow,long_min_profit_shadow,long_combo_shadow,scanner_selective_shadow",
    1,
)

# Healthcheck v7.13 -> v7.14
h = h.replace(
    "# Diamond Trader Healthcheck v7.13",
    "# Diamond Trader Healthcheck v7.14",
    1,
)

version_marker = 'if str(runner.get("version") or "") != "1.4":'
if version_marker not in h:
    fail("healthcheck runner-versiemarker ontbreekt")
h = h.replace(
    version_marker,
    'if str(runner.get("version") or "") != "1.5":',
    1,
)
h = h.replace(
    'errors.append("periodic runner is niet v1.4")',
    'errors.append("periodic runner is niet v1.5")',
    1,
)

old_files = (
    "    periodic_analysis_runner.py \\\n"
    "    long_combo_shadow_lab.py\n"
)
new_files = (
    "    periodic_analysis_runner.py \\\n"
    "    long_combo_shadow_lab.py \\\n"
    "    scanner_selective_shadow_lab.py\n"
)
if old_files not in h:
    fail("healthcheck projectbestanden-marker ontbreekt")
h = h.replace(old_files, new_files, 1)

# Syntaxcheck tijdelijke versies.
with tempfile.NamedTemporaryFile(
    "w", encoding="utf-8", dir=str(PROJECT), suffix=".py", delete=False
) as tmp:
    tmp.write(r)
    r_tmp = Path(tmp.name)

with tempfile.NamedTemporaryFile(
    "w", encoding="utf-8", dir=str(PROJECT), suffix=".sh", delete=False
) as tmp:
    tmp.write(h)
    h_tmp = Path(tmp.name)

try:
    py = subprocess.run(
        ["python3", "-m", "py_compile", str(r_tmp)],
        text=True,
        capture_output=True,
    )
    if py.returncode != 0:
        fail("nieuwe runner Python-syntax fout: " + (py.stderr.strip() or py.stdout.strip()))

    sh = subprocess.run(
        ["bash", "-n", str(h_tmp)],
        text=True,
        capture_output=True,
    )
    if sh.returncode != 0:
        fail("nieuwe healthcheck Bash-syntax fout: " + (sh.stderr.strip() or sh.stdout.strip()))
finally:
    r_tmp.unlink(missing_ok=True)
    h_tmp.unlink(missing_ok=True)

atomic_write(RUNNER, r, stat.S_IMODE(RUNNER.stat().st_mode))
atomic_write(HEALTH, h, stat.S_IMODE(HEALTH.stat().st_mode))

subprocess.run(["python3", "-m", "py_compile", str(RUNNER)], check=True)
subprocess.run(["bash", "-n", str(HEALTH)], check=True)

test = subprocess.run(
    ["python3", str(RUNNER), "--self-test"],
    cwd=str(PROJECT),
    text=True,
    capture_output=True,
)
if test.returncode != 0 or "PERIODIC_ANALYSIS_SELF_TEST_OK" not in test.stdout:
    fail("runner self-test mislukt: " + (test.stderr.strip() or test.stdout.strip()))

print("SELECTIVE_SHADOW_AUTOMATION_PATCH_OK")
print("periodic_analysis_runner.py -> v1.5")
print("healthcheck.sh -> v7.14")
print("Nieuwe taak: scanner_selective_shadow")
print("Alle 7 analysetaken blijven strikt sequentieel")
print("Geen config/strategie/bot-state/transacties gewijzigd")
print(test.stdout.strip())
