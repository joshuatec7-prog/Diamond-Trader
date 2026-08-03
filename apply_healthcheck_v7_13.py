#!/usr/bin/env python3
from pathlib import Path
import os, shutil, stat, subprocess, tempfile

project = Path("/opt/render/project/src")
target = project / "healthcheck.sh"
backup = project / "healthcheck_v7_12_backup.sh"

if not target.is_file():
    raise SystemExit("STOP: healthcheck.sh ontbreekt")

t = target.read_text(encoding="utf-8")

if "# Diamond Trader Healthcheck v7.13" in t:
    print("HEALTHCHECK_V7_13_ALREADY_INSTALLED")
    raise SystemExit(0)

if not t.startswith("#!/usr/bin/env bash"):
    raise SystemExit("STOP: healthcheck.sh is geen Bash-bestand")

if "# Diamond Trader Healthcheck v7.12" not in t:
    raise SystemExit("STOP: healthcheck.sh is niet v7.12")

markers = [
    'SCANNER_RUNNER_LOG="$DATA_DIR/diamond_market_scanner_runner.log"',
    'echo "2. PROJECTBESTANDEN"',
    '    scanner_healthcheck.sh \\\n    periodic_analysis_runner.py\n',
    'if str(report.get("version") or "") != "1.2":',
]
for m in markers:
    if m not in t:
        raise SystemExit("STOP: verwachte marker ontbreekt: " + m.splitlines()[0])

if not backup.exists():
    shutil.copy2(target, backup)
    print("Back-up gemaakt:", backup)

t = t.replace(
    "# Diamond Trader Healthcheck v7.12",
    "# Diamond Trader Healthcheck v7.13",
    1,
)

t = t.replace(
    'SCANNER_RUNNER_LOG="$DATA_DIR/diamond_market_scanner_runner.log"\n',
    'SCANNER_RUNNER_LOG="$DATA_DIR/diamond_market_scanner_runner.log"\n'
    'LONG_COMBO_SHADOW_REPORT_FILE="$DATA_DIR/diamond_long_combo_shadow_report.json"\n'
    'LONG_COMBO_SHADOW_RUNNER_LOG="$DATA_DIR/diamond_long_combo_shadow_runner.log"\n',
    1,
)

t = t.replace(
    '    scanner_healthcheck.sh \\\n    periodic_analysis_runner.py\n',
    '    scanner_healthcheck.sh \\\n'
    '    periodic_analysis_runner.py \\\n'
    '    long_combo_shadow_lab.py\n',
    1,
)

t = t.replace(
    'if str(report.get("version") or "") != "1.2":',
    'if str(report.get("version") or "") != "1.3":',
    1,
)

marker = '''echo
echo "2. PROJECTBESTANDEN"
echo "------------------------------------------------------------"
'''

section = r'''echo
echo "1B. LONG COMBO SHADOW"
echo "------------------------------------------------------------"

file_info "$LONG_COMBO_SHADOW_REPORT_FILE" "LONG Combo Shadow rapport" "true"
file_info "$LONG_COMBO_SHADOW_RUNNER_LOG" "LONG Combo Shadow runnerlog"

if [ -f "$PROJECT_DIR/long_combo_shadow_lab.py" ]; then
    if python3 -m py_compile "$PROJECT_DIR/long_combo_shadow_lab.py" 2>/dev/null; then
        echo "[OK]    LONG Combo Shadow Pythoncontrole geslaagd"
    else
        echo "[FOUT]  LONG Combo Shadow Pythoncontrole mislukt"
        ERRORS=$((ERRORS + 1))
    fi
fi

if [ -f "$LONG_COMBO_SHADOW_REPORT_FILE" ] && [ -f "$PERIODIC_ANALYSIS_STATE_FILE" ]; then
    if ! python3 - "$LONG_COMBO_SHADOW_REPORT_FILE" "$PERIODIC_ANALYSIS_STATE_FILE" "$NOW_EPOCH" <<'PY'
import json, sys
from datetime import datetime, timezone
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
runner = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
now = datetime.fromtimestamp(int(sys.argv[3]), tz=timezone.utc)

def age(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt.astimezone(timezone.utc)).total_seconds()/60.0)
    except Exception:
        return None

def i(v):
    try: return int(v)
    except Exception: return 0

def f(v):
    try: return float(v)
    except Exception: return 0.0

progress = report.get("progress") or {}
variants = report.get("variants") or {}
safety = report.get("safety") or {}
report_age = age(report.get("generated_at"))

print("[OK]    LONG Combo Shadow rapport leesbaar")
print(f"        Versie             : {report.get('version') or '-'}")
print(f"        Modus              : {report.get('mode') or '-'}")
print(f"        Nieuwe signalen    : {i(progress.get('signals_detected'))}/{i(progress.get('target_signals') or 20)}")
print("        Leeftijd rapport   : " + (f"{report_age:.1f} minuten" if report_age is not None else "-"))

for name in ("CURRENT", "WAIT30_100", "WAIT30_050"):
    row = variants.get(name) or {}
    print(
        f"        {name:12s}       : "
        f"closed={i(row.get('closed'))}, "
        f"wins={i(row.get('wins'))}, "
        f"losses={i(row.get('losses'))}, "
        f"open={i(row.get('open'))}, "
        f"pending={i(row.get('pending_entry'))}, "
        f"pnl=€{f(row.get('net_pnl_eur')):+.4f}"
    )

errors = []

if str(report.get("version") or "") != "1.0":
    errors.append("onverwachte Combo Shadow-versie")
if report.get("mode") != "READ_ONLY_LONG_COMBO_SHADOW":
    errors.append("onverwachte Combo Shadow-modus")
if report_age is None or report_age > 40:
    errors.append("Combo Shadow-rapport is niet actueel")
for name in ("CURRENT", "WAIT30_100", "WAIT30_050"):
    if name not in variants:
        errors.append("variant ontbreekt: " + name)
for key in ("orders_possible","private_exchange_calls","api_keys_loaded","config_write","bot_state_write","transactions_write"):
    if safety.get(key) is not False:
        errors.append("veiligheidsveld niet False: " + key)
if safety.get("own_files_only") is not True:
    errors.append("own_files_only is niet True")

tasks = runner.get("tasks") or {}
task = tasks.get("long_combo_shadow") or {}
active = str(runner.get("active_task") or "")
status = str(task.get("last_status") or "NOG_NIET_GEDRAAID")
runs = i(task.get("run_count"))
exit_code = task.get("last_exit_code")
completed_age = age(task.get("last_completed_at"))
started_age = age(task.get("last_started_at"))

print("[OK]    LONG Combo automatische runnerstatus")
print(f"        Runner-versie      : {runner.get('version') or '-'}")
print(f"        Actieve taak       : {active or 'geen'}")
print(f"        Combo runs         : {runs}")
print(f"        Combo status       : {status}")
print(f"        Combo exitcode     : {exit_code}")

if str(runner.get("version") or "") != "1.4":
    errors.append("periodic runner is niet v1.4")
if runner.get("mode") != "SEQUENTIAL_PERIODIC_ANALYSIS":
    errors.append("periodic runner-modus klopt niet")
if "long_combo_shadow" not in tasks:
    errors.append("long_combo_shadow ontbreekt in runner-state")
elif status == "BEZIG":
    if active != "long_combo_shadow":
        errors.append("Combo BEZIG maar niet active_task")
    elif started_age is None or started_age > 35:
        errors.append("actieve Combo-run duurt te lang")
else:
    if status != "OK":
        errors.append("laatste Combo-status is " + status)
    if exit_code != 0:
        errors.append("laatste Combo-exitcode is niet 0")
    if completed_age is None or completed_age > 35:
        errors.append("laatste Combo-run is niet actueel")

if runs < 1:
    errors.append("Combo heeft nog geen automatische run")

if errors:
    for e in errors:
        print("[FOUT]  " + e)
    raise SystemExit(1)

print("[OK]    LONG Combo Shadow is actueel, automatisch en alleen-lezen")
PY
    then
        ERRORS=$((ERRORS + 1))
    fi
fi

'''

if marker not in t:
    raise SystemExit("STOP: sectiemarker ontbreekt")

t = t.replace(marker, section + marker, 1)

mode = stat.S_IMODE(target.stat().st_mode)

with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(project), delete=False) as tmp:
    tmp.write(t)
    tmp_path = Path(tmp.name)

try:
    os.chmod(tmp_path, mode)
    r = subprocess.run(["bash", "-n", str(tmp_path)], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("STOP: bash-syntaxfout: " + (r.stderr.strip() or r.stdout.strip()))
    os.replace(tmp_path, target)
finally:
    if tmp_path.exists():
        tmp_path.unlink()

print("HEALTHCHECK_V7_13_PATCH_OK")
print("Readiness verwacht v1.3")
print("Combo Shadow-controle toegevoegd")
print("Bash-syntax OK")
