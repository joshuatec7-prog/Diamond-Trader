#!/usr/bin/env python3
'''
Diamond Trader Healthcheck upgrade v7.12 -> v7.13

Doet uitsluitend:
- maakt een back-up van de bestaande healthcheck.sh;
- wijzigt versienummer 7.12 naar 7.13;
- voegt LONG Combo Shadow-bestanden toe;
- voegt een alleen-lezen LONG Combo Shadow-statuscontrole toe;
- voegt long_combo_shadow_lab.py toe aan projectbestandscontrole;
- valideert de nieuwe shellsyntax voordat het bestand wordt vervangen.

Geen config-, bot-state-, strategie- of transactiebestanden worden gewijzigd.
'''

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

PROJECT_DIR = Path("/opt/render/project/src")
TARGET = PROJECT_DIR / "healthcheck.sh"
BACKUP = PROJECT_DIR / "healthcheck_v7_12_backup.sh"

OLD_VERSION = "# Diamond Trader Healthcheck v7.12"
NEW_VERSION = "# Diamond Trader Healthcheck v7.13"

VARIABLE_MARKER = (
    'SCANNER_RUNNER_LOG="$DATA_DIR/diamond_market_scanner_runner.log"\n'
)

VARIABLE_BLOCK = r'''SCANNER_RUNNER_LOG="$DATA_DIR/diamond_market_scanner_runner.log"

LONG_COMBO_SHADOW_REPORT_FILE="$DATA_DIR/diamond_long_combo_shadow_report.json"
LONG_COMBO_SHADOW_RUNNER_LOG="$DATA_DIR/diamond_long_combo_shadow_runner.log"
'''

PROJECT_FILES_OLD = r'''    scanner_healthcheck.sh \
    periodic_analysis_runner.py
'''

PROJECT_FILES_NEW = r'''    scanner_healthcheck.sh \
    periodic_analysis_runner.py \
    long_combo_shadow_lab.py
'''

SECTION_MARKER = '''echo
echo "2. PROJECTBESTANDEN"
echo "------------------------------------------------------------"
'''

COMBO_SECTION = r'''echo
echo "1B. LONG COMBO SHADOW"
echo "------------------------------------------------------------"

file_info "$LONG_COMBO_SHADOW_REPORT_FILE" "LONG Combo Shadow rapport" "true"
file_info "$LONG_COMBO_SHADOW_RUNNER_LOG" "LONG Combo Shadow runnerlog"

if [ -f "$LONG_COMBO_SHADOW_REPORT_FILE" ] && [ -f "$PERIODIC_ANALYSIS_STATE_FILE" ]; then
    if ! python3 - \
        "$LONG_COMBO_SHADOW_REPORT_FILE" \
        "$PERIODIC_ANALYSIS_STATE_FILE" \
        "$NOW_EPOCH" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

report_file = Path(sys.argv[1])
runner_state_file = Path(sys.argv[2])
now_epoch = int(sys.argv[3])


def load_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[FOUT]  JSON lezen mislukt ({path.name}): {exc}")
        raise SystemExit(1)

    if not isinstance(data, dict):
        print(f"[FOUT]  JSON bevat geen object: {path}")
        raise SystemExit(1)

    return data


def parse_time(value):
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    except Exception:
        return None


def age_minutes(value):
    dt = parse_time(value)

    if dt is None:
        return None

    now = datetime.fromtimestamp(
        now_epoch,
        tz=timezone.utc,
    )

    return max(
        0.0,
        (now - dt).total_seconds() / 60.0,
    )


def fnum(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def inum(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


report = load_json(report_file)
runner = load_json(runner_state_file)

mode = report.get("mode")
version = report.get("version")
generated_at = report.get("generated_at")
report_age = age_minutes(generated_at)

progress = report.get("progress") or {}
variants = report.get("variants") or {}
comparisons = report.get("comparisons") or {}
safety = report.get("safety") or {}
report_errors = report.get("errors") or []

signals = inum(progress.get("signals_detected"), 0)
target = inum(progress.get("target_signals"), 20)
progress_pct = fnum(progress.get("progress_pct"), 0.0)

print("[OK]    LONG Combo Shadow rapport leesbaar")
print(f"        Versie             : {version or '-'}")
print(f"        Modus              : {mode or '-'}")
print(f"        Gegenereerd        : {generated_at or '-'}")
print(
    "        Leeftijd rapport   : "
    + (
        f"{report_age:.1f} minuten"
        if report_age is not None
        else "-"
    )
)
print(f"        Nieuwe signalen    : {signals}/{target}")
print(f"        Voortgang          : {progress_pct:.1f}%")

required_variants = (
    "CURRENT",
    "WAIT30_100",
    "WAIT30_050",
)

for name in required_variants:
    row = variants.get(name) or {}

    print(
        f"        {name:12s}       : "
        f"closed={inum(row.get('closed'))}, "
        f"wins={inum(row.get('wins'))}, "
        f"losses={inum(row.get('losses'))}, "
        f"open={inum(row.get('open'))}, "
        f"pending={inum(row.get('pending_entry'))}, "
        f"pnl=€{fnum(row.get('net_pnl_eur')):+.4f}"
    )

wait100 = comparisons.get("WAIT30_100") or {}
wait050 = comparisons.get("WAIT30_050") or {}
between = comparisons.get(
    "WAIT30_050_vs_WAIT30_100"
) or {}

print(
    "        WAIT30_100 Δcurr : "
    f"€{fnum(wait100.get('delta_net_pnl_vs_current_eur')):+.4f}"
)
print(
    "        WAIT30_050 Δcurr : "
    f"€{fnum(wait050.get('delta_net_pnl_vs_current_eur')):+.4f}"
)
print(
    "        050 vs 100 ΔPnL  : "
    f"€{fnum(between.get('delta_net_pnl_eur')):+.4f}"
)

runner_version = runner.get("version")
runner_mode = runner.get("mode")
active = runner.get("active_task")
tasks = runner.get("tasks") or {}
task = tasks.get("long_combo_shadow") or {}

task_status = task.get("last_status") or "NOG_NIET_GEDRAAID"
task_runs = inum(task.get("run_count"), 0)
task_exit = task.get("last_exit_code")
task_completed = task.get("last_completed_at")
task_age = age_minutes(task_completed)

print()
print("[OK]    Automatische runnerstatus")
print(f"        Runner-versie      : {runner_version or '-'}")
print(f"        Runner-modus       : {runner_mode or '-'}")
print(f"        Actieve taak       : {active or 'geen'}")
print(f"        Combo runs         : {task_runs}")
print(f"        Combo status       : {task_status}")
print(f"        Combo exitcode     : {task_exit}")
print(f"        Laatste Combo-run  : {task_completed or '-'}")
print(
    "        Leeftijd run       : "
    + (
        f"{task_age:.1f} minuten"
        if task_age is not None
        else "-"
    )
)

errors = []

if str(version or "") != "1.0":
    errors.append(
        f"onverwachte Combo Shadow-versie: {version!r}"
    )

if mode != "READ_ONLY_LONG_COMBO_SHADOW":
    errors.append(
        f"onverwachte Combo Shadow-modus: {mode!r}"
    )

if report_age is None:
    errors.append(
        "Combo Shadow-rapport heeft geen geldige generated_at"
    )
elif report_age > 40.0:
    errors.append(
        f"Combo Shadow-rapport is te oud: {report_age:.1f} minuten"
    )

for name in required_variants:
    if name not in variants:
        errors.append(
            f"Combo Shadow-variant ontbreekt: {name}"
        )

expected_false = (
    "orders_possible",
    "private_exchange_calls",
    "api_keys_loaded",
    "config_write",
    "bot_state_write",
    "transactions_write",
)

for key in expected_false:
    if safety.get(key) is not False:
        errors.append(
            f"veiligheidsveld {key} is niet aantoonbaar False"
        )

if safety.get("own_files_only") is not True:
    errors.append(
        "own_files_only is niet aantoonbaar True"
    )

if report_errors:
    errors.append(
        "Combo Shadow rapporteert fouten: "
        + "; ".join(str(item) for item in report_errors)
    )

if runner_mode != "SEQUENTIAL_PERIODIC_ANALYSIS":
    errors.append(
        f"onverwachte periodic runner-modus: {runner_mode!r}"
    )

if "long_combo_shadow" not in tasks:
    errors.append(
        "long_combo_shadow ontbreekt in periodic runner-state"
    )
else:
    if task_runs < 1:
        errors.append(
            "long_combo_shadow heeft nog geen automatische run"
        )

    if active == "long_combo_shadow" and task_status == "BEZIG":
        print(
            "[INFO]  LONG Combo Shadow automatische run is momenteel bezig"
        )
    else:
        if task_status != "OK":
            errors.append(
                f"laatste Combo runnerstatus is {task_status!r}"
            )

        if task_exit != 0:
            errors.append(
                f"laatste Combo runner-exitcode is {task_exit!r}"
            )

        if task_age is None:
            errors.append(
                "laatste Combo run heeft geen geldige tijd"
            )
        elif task_age > 35.0:
            errors.append(
                f"laatste Combo run is te oud: {task_age:.1f} minuten"
            )

if errors:
    for item in errors:
        print(f"[FOUT]  {item}")
    raise SystemExit(1)

print("[OK]    LONG Combo Shadow is actueel, automatisch en alleen-lezen")
PY
    then
        ERRORS=$((ERRORS + 1))
    fi
fi

'''


def fail(message: str) -> None:
    raise SystemExit(f"UPGRADE AFGEBROKEN: {message}")


if not TARGET.exists():
    fail(f"{TARGET} ontbreekt")

text = TARGET.read_text(encoding="utf-8")

if NEW_VERSION in text and 'echo "1B. LONG COMBO SHADOW"' in text:
    print("HEALTHCHECK_V7_13_ALREADY_INSTALLED")
    print(f"Bestand: {TARGET}")
    raise SystemExit(0)

if OLD_VERSION not in text:
    fail(
        "verwachte v7.12-versieregel niet gevonden; "
        "er wordt niets gewijzigd"
    )

if VARIABLE_MARKER not in text:
    fail(
        "marker SCANNER_RUNNER_LOG niet gevonden; "
        "er wordt niets gewijzigd"
    )

if SECTION_MARKER not in text:
    fail(
        "marker 2. PROJECTBESTANDEN niet gevonden; "
        "er wordt niets gewijzigd"
    )

if PROJECT_FILES_OLD not in text:
    fail(
        "projectbestandenlijst heeft niet de verwachte v7.12-vorm; "
        "er wordt niets gewijzigd"
    )

if 'echo "1B. LONG COMBO SHADOW"' in text:
    fail(
        "Combo Shadow-sectie lijkt al aanwezig zonder v7.13-versieregel"
    )

if not BACKUP.exists():
    shutil.copy2(TARGET, BACKUP)
    print(f"Back-up gemaakt: {BACKUP}")
else:
    print(f"Back-up bestaat al: {BACKUP}")

new_text = text

new_text = new_text.replace(
    OLD_VERSION,
    NEW_VERSION,
    1,
)

new_text = new_text.replace(
    VARIABLE_MARKER,
    VARIABLE_BLOCK,
    1,
)

new_text = new_text.replace(
    PROJECT_FILES_OLD,
    PROJECT_FILES_NEW,
    1,
)

new_text = new_text.replace(
    SECTION_MARKER,
    COMBO_SECTION + SECTION_MARKER,
    1,
)

old_mode = stat.S_IMODE(
    TARGET.stat().st_mode
)

with tempfile.NamedTemporaryFile(
    mode="w",
    encoding="utf-8",
    dir=str(PROJECT_DIR),
    prefix=".healthcheck_v7_13_",
    suffix=".sh",
    delete=False,
) as tmp:
    tmp.write(new_text)
    tmp_path = Path(tmp.name)

try:
    os.chmod(
        tmp_path,
        old_mode,
    )

    syntax = subprocess.run(
        [
            "bash",
            "-n",
            str(tmp_path),
        ],
        text=True,
        capture_output=True,
    )

    if syntax.returncode != 0:
        detail = (
            syntax.stderr.strip()
            or syntax.stdout.strip()
            or "onbekende bash-syntaxfout"
        )
        fail(
            "nieuwe healthcheck heeft ongeldige shellsyntax: "
            + detail
        )

    os.replace(
        tmp_path,
        TARGET,
    )

finally:
    if tmp_path.exists():
        tmp_path.unlink()

final = TARGET.read_text(
    encoding="utf-8"
)

required_markers = (
    NEW_VERSION,
    'echo "1B. LONG COMBO SHADOW"',
    'LONG_COMBO_SHADOW_REPORT_FILE=',
    'LONG_COMBO_SHADOW_RUNNER_LOG=',
    "long_combo_shadow_lab.py",
    'tasks.get("long_combo_shadow")',
)

missing = [
    marker
    for marker in required_markers
    if marker not in final
]

if missing:
    fail(
        "nacontrole mislukt; ontbrekende markers: "
        + ", ".join(missing)
    )

print()
print("HEALTHCHECK_V7_13_UPGRADE_OK")
print(f"Actief bestand : {TARGET}")
print(f"Back-up        : {BACKUP}")
print("Bash-syntax    : OK")
print("Toegevoegd     : LONG Combo Shadow rapport + automatische runnerstatus")
print("Veiligheid     : alleen-lezen controle")
