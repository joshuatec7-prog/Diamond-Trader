#!/usr/bin/env bash

# Diamond Trader Healthcheck v7.13
# Geheugenarme controle: wijzigt geen bot-, test-, scanner-, Strategy Lab- of Readiness-bestanden.

set -u

DATA_DIR="/var/data"
PROJECT_DIR="/opt/render/project/src"

STATE_FILE="$DATA_DIR/diamond_state.json"
CONTROL_FILE="$DATA_DIR/diamond_control.json"
AGENT_STATE_FILE="$DATA_DIR/diamond_agent_state.json"
DIAG_STATS_FILE="$DATA_DIR/diamond_diagnose_stats.json"
SUPERVISOR_FILE="$DATA_DIR/diamond_supervisor_state.json"
TRADES_FILE="$DATA_DIR/diamond_transactions.csv"

LONG_BASELINE_FILE="$DATA_DIR/diamond_test_baseline.json"
LONG_REPORT_FILE="$DATA_DIR/diamond_test_report.json"
SHORT_BASELINE_FILE="$DATA_DIR/diamond_short_test_baseline.json"
SHORT_REPORT_FILE="$DATA_DIR/diamond_short_test_report.json"
SHORT_INTERIM_5_FILE="$DATA_DIR/diamond_short_test_interim_5.json"
SHORT_INTERIM_10_FILE="$DATA_DIR/diamond_short_test_interim_10.json"

STRATEGY_LAB_JSON_FILE="$DATA_DIR/diamond_strategy_lab.json"
STRATEGY_LAB_TEXT_FILE="$DATA_DIR/diamond_strategy_lab.txt"
STRATEGY_LAB_GROUPS_FILE="$DATA_DIR/diamond_strategy_lab_groups.csv"
STRATEGY_LAB_RUNNER_LOG="$DATA_DIR/diamond_strategy_lab_runner.log"

PERIODIC_ANALYSIS_STATE_FILE="$DATA_DIR/diamond_periodic_analysis_state.json"
PERIODIC_ANALYSIS_RUNNER_LOG="$DATA_DIR/diamond_periodic_analysis_runner.log"
DIAG_RUNNER_LOG="$DATA_DIR/diamond_diagnose_runner.log"
SCANNER_RUNNER_LOG="$DATA_DIR/diamond_market_scanner_runner.log"
LONG_COMBO_SHADOW_REPORT_FILE="$DATA_DIR/diamond_long_combo_shadow_report.json"
LONG_COMBO_SHADOW_RUNNER_LOG="$DATA_DIR/diamond_long_combo_shadow_runner.log"

SHADOW_MILESTONE_5_JSON="$DATA_DIR/diamond_market_shadow_milestone_5.json"
SHADOW_MILESTONE_5_TEXT="$DATA_DIR/diamond_market_shadow_milestone_5.txt"
SHADOW_MILESTONE_10_JSON="$DATA_DIR/diamond_market_shadow_milestone_10.json"
SHADOW_MILESTONE_10_TEXT="$DATA_DIR/diamond_market_shadow_milestone_10.txt"
SHADOW_MILESTONE_20_JSON="$DATA_DIR/diamond_market_shadow_milestone_20.json"
SHADOW_MILESTONE_20_TEXT="$DATA_DIR/diamond_market_shadow_milestone_20.txt"

READINESS_GATE_JSON_FILE="$DATA_DIR/diamond_readiness_gate.json"
READINESS_GATE_TEXT_FILE="$DATA_DIR/diamond_readiness_gate.txt"
FINAL_VALIDATION_FILE="$DATA_DIR/diamond_final_validation.json"
LIVE_APPROVAL_FILE="$DATA_DIR/diamond_live_approval.json"

BACKUP_DIR="$DATA_DIR/backups"

NOW_EPOCH=$(date +%s)
ERRORS=0

echo
echo "============================================================"
echo " DIAMOND TRADER CONTROLE"
echo " $(date)"
echo "============================================================"
echo

check_process() {
    local pattern="$1"
    local display_name="$2"
    local result

    result=$(pgrep -af "$pattern" 2>/dev/null || true)

    if [ -n "$result" ]; then
        echo "[OK]    $display_name draait"
        echo "$result" | sed 's/^/        /'
    else
        echo "[FOUT]  $display_name draait NIET"
        ERRORS=$((ERRORS + 1))
    fi
}

file_info() {
    local path="$1"
    local label="$2"
    local required="${3:-false}"

    if [ -f "$path" ]; then
        local size
        local modified
        local age

        size=$(stat -c %s "$path" 2>/dev/null || echo 0)
        modified=$(stat -c %Y "$path" 2>/dev/null || echo 0)
        age=$(( (NOW_EPOCH - modified) / 60 ))

        echo "[OK]    $label aanwezig"
        echo "        Bestand: $path"
        echo "        Grootte: $size bytes"
        echo "        Laatst gewijzigd: $age minuten geleden"
    else
        if [ "$required" = "true" ]; then
            echo "[FOUT]  $label ontbreekt"
            ERRORS=$((ERRORS + 1))
        else
            echo "[INFO]  $label nog niet aanwezig"
        fi
        echo "        Bestand: $path"
    fi
}

echo "1. PROCESSEN"
echo "------------------------------------------------------------"

check_process \
    'python3[[:space:]]+agent\.py([[:space:]]|$)' \
    "Diamond Agent"

check_process \
    'python3[[:space:]]+supervisor_agent\.py([[:space:]]|$)' \
    "Diamond Supervisor"

check_process \
    'python3[[:space:]]+closed_candle_runner\.py[[:space:]]+bot([[:space:]]|$)' \
    "Diamond Bot"

check_process \
    'python3[[:space:]]+strategy_lab\.py[[:space:]]+--loop[[:space:]]+--interval-minutes[[:space:]]+360[[:space:]]+--no-print([[:space:]]|$)' \
    "Diamond Strategy Lab"

check_process \
    'python3[[:space:]]+periodic_analysis_runner\.py([[:space:]]|$)' \
    "Diamond Periodieke Analyse"

echo
echo "1A. PERIODIEKE DIAGNOSE EN MARKET SCANNER"
echo "------------------------------------------------------------"

file_info "$PERIODIC_ANALYSIS_STATE_FILE" "Periodieke analyse-state" "true"
file_info "$PERIODIC_ANALYSIS_RUNNER_LOG" "Periodieke analyse-runnerlog"
file_info "$DIAG_RUNNER_LOG" "Diagnose-runnerlog"
file_info "$SCANNER_RUNNER_LOG" "Market Scanner-runnerlog"

if [ -f "$PERIODIC_ANALYSIS_STATE_FILE" ]; then
    if ! python3 - "$PERIODIC_ANALYSIS_STATE_FILE" "$NOW_EPOCH" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
now_epoch = int(sys.argv[2])

try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"[FOUT]  Periodieke analyse-state lezen mislukt: {exc}")
    raise SystemExit(1)

if not isinstance(data, dict):
    print("[FOUT]  Periodieke analyse-state bevat geen object")
    raise SystemExit(1)


def age_minutes(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.fromtimestamp(now_epoch, tz=timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 60.0)
    except Exception:
        return None

mode = data.get("mode")
interval = int(data.get("interval_seconds", 0) or 0)
sequential = data.get("sequential") is True
active = data.get("active_task") or "geen"
tasks = data.get("tasks") or {}

print(f"[OK]    Modus              : {mode or '-'}")
print(f"        Interval           : {interval} seconden")
print(f"        Sequentieel        : {'JA' if sequential else 'NEE'}")
print(f"        Actieve taak       : {active}")
print(f"        Cycli              : {int(data.get('cycle_count', 0) or 0)}")

if mode != "SEQUENTIAL_PERIODIC_ANALYSIS":
    print("[FOUT]  Onverwachte modus voor periodieke analyse")
    raise SystemExit(1)

if interval != 900:
    print("[FOUT]  Periodieke analyse gebruikt niet het verwachte interval van 900 seconden")
    raise SystemExit(1)

if not sequential:
    print("[FOUT]  Diagnose en Scanner zijn niet als sequentieel gemarkeerd")
    raise SystemExit(1)

errors = 0

for key, label in (("diagnose", "Diamond Diagnose"), ("scanner", "Diamond Market Scanner")):
    task = tasks.get(key) or {}
    status = task.get("last_status") or "NOG_NIET_GEDRAAID"
    completed = task.get("last_completed_at")
    age = age_minutes(completed)
    exit_code = task.get("last_exit_code")
    runs = int(task.get("run_count", 0) or 0)
    duration = task.get("last_duration_seconds")

    if status in {"NOG_NIET_GEDRAAID", "BEZIG"} and active == key:
        print(f"[INFO]  {label}: periodieke run is bezig")
        continue

    age_text = f"{age:.1f} min" if age is not None else "-"
    print(
        f"[{'OK' if status == 'OK' else 'FOUT'}]    {label}: "
        f"status={status} | runs={runs} | leeftijd={age_text} | "
        f"duur={duration if duration is not None else '-'}s | exit={exit_code}"
    )

    if status != "OK":
        errors += 1
        continue

    if age is None or age > 35.0:
        print(f"[FOUT]  {label} is ouder dan 35 minuten")
        errors += 1

if errors:
    raise SystemExit(1)

print("[OK]    Diagnose en Market Scanner draaien geheugenarm en nooit tegelijk")
PY
    then
        ERRORS=$((ERRORS + 1))
    fi
fi

echo
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

echo
echo "2. PROJECTBESTANDEN"
echo "------------------------------------------------------------"

for file_name in \
    agent.py \
    config.yaml \
    closed_candle_runner.py \
    diagnose.py \
    supervisor_agent.py \
    diamond_bot.py \
    short_diagnose.py \
    market_scanner.py \
    strategy_lab.py \
    readiness_gate.py \
    requirements.txt \
    start.sh \
    healthcheck.sh \
    scanner_healthcheck.sh \
    periodic_analysis_runner.py \
    long_combo_shadow_lab.py
do
    if [ -f "$PROJECT_DIR/$file_name" ]; then
        echo "[OK]    $file_name"
    else
        echo "[FOUT]  $file_name ontbreekt"
        ERRORS=$((ERRORS + 1))
    fi
done

echo
echo "3. BOT-STATE"
echo "------------------------------------------------------------"

file_info "$STATE_FILE" "Bot-state" "true"

if [ -f "$STATE_FILE" ]; then
    if ! python3 - "$STATE_FILE" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])

try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"        [FOUT] JSON lezen mislukt: {exc}")
    raise SystemExit(1)

positions = data.get("positions") or {}
shorts = data.get("short_positions") or {}

print(f"        Open spotposities : {len(positions)}")
print(f"        Open shorts       : {len(shorts)}")
print(f"        Spot trades       : {int(data.get('trades', 0) or 0)}")
print(f"        Spot winsttrades  : {int(data.get('wins', 0) or 0)}")
print(f"        Spot PnL          : {float(data.get('pnl_quote', 0) or 0):+.2f} EUR")
print(f"        Dry-run saldo     : {float(data.get('simulated_free_quote', 0) or 0):.2f} EUR")
print(f"        Short trades      : {int(data.get('short_trades', 0) or 0)}")
print(f"        Short winsttrades : {int(data.get('short_wins', 0) or 0)}")
print(f"        Short PnL         : {float(data.get('short_pnl_quote', 0) or 0):+.4f} EUR")
PY
    then
        ERRORS=$((ERRORS + 1))
    fi
fi

echo
echo "4. VEILIGHEIDSCONTROLE"
echo "------------------------------------------------------------"

file_info "$CONTROL_FILE" "Controlebestand" "true"

if [ -f "$CONTROL_FILE" ]; then
    if ! python3 - "$CONTROL_FILE" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])

try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"        [FOUT] JSON lezen mislukt: {exc}")
    raise SystemExit(1)

print(f"        Gepauzeerd        : {bool(data.get('paused', False))}")
print(f"        Reden             : {data.get('pause_reason') or '-'}")
print(f"        Gepauzeerd sinds  : {data.get('paused_at') or '-'}")
PY
    then
        ERRORS=$((ERRORS + 1))
    fi
fi

echo
echo "5. AGENT"
echo "------------------------------------------------------------"

file_info "$AGENT_STATE_FILE" "Agent-state"

if [ -f "$AGENT_STATE_FILE" ]; then
    if ! python3 - "$AGENT_STATE_FILE" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])

try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"        [FOUT] JSON lezen mislukt: {exc}")
    raise SystemExit(1)


def format_time(value):
    if value in (None, "", 0):
        return "-"

    if isinstance(value, str):
        return value

    try:
        return datetime.fromtimestamp(
            float(value),
            tz=timezone.utc,
        ).isoformat()
    except Exception:
        return str(value)


reports = data.get("sent_reports") or data.get("sent_daily_reports") or []
weekly = data.get("sent_weekly_reports") or []

print(f"        Laatste analyse   : {format_time(data.get('last_analysis_ts'))}")
print(f"        Statusmails       : {len(reports)}")
print(f"        Weekrapporten     : {len(weekly)}")
print(
    "        Schaduw-openmails: "
    f"{int(data.get('shadow_open_notifications_sent', 0) or 0)}"
)
print(
    "        Schaduw-sluitmail: "
    f"{int(data.get('shadow_close_notifications_sent', 0) or 0)}"
)
print(
    "        Laatste openmail : "
    f"{data.get('last_shadow_open_email_at') or '-'}"
)
print(
    "        Laatste open munt: "
    f"{data.get('last_shadow_open_symbol') or '-'}"
)
print(
    "        Laatste sluitmail: "
    f"{data.get('last_shadow_close_email_at') or '-'}"
)
print(
    "        Laatste sluitmunt: "
    f"{data.get('last_shadow_close_symbol') or '-'}"
)
print(
    "        Lab-verversingen : "
    f"{int(data.get('strategy_lab_refresh_count', 0) or 0)}"
)
print(
    "        Laatste Lab-run  : "
    f"{data.get('last_strategy_lab_refresh_at') or '-'}"
)
print(
    "        Lab-runstatus    : "
    f"{data.get('last_strategy_lab_refresh_status') or '-'}"
)
print(
    "        Lab-runfout      : "
    f"{data.get('last_strategy_lab_refresh_error') or '-'}"
)
print(
    "        Scannerwatch     : "
    f"{data.get('scanner_watch_last_status') or '-'}"
)
print(
    "        Watch-controles  : "
    f"{int(data.get('scanner_watch_checks', 0) or 0)}"
)
print(
    "        Laatste watch    : "
    f"{data.get('scanner_watch_last_check_at') or '-'}"
)
print(
    "        Laatste geschikt : "
    f"{data.get('scanner_watch_last_suitable_at') or '-'}"
)
print(
    "        Uren zonder      : "
    f"{float(data.get('scanner_watch_hours_without_suitable', 0) or 0):.1f}"
)
print(
    "        Signalen 24 uur  : "
    f"{int(data.get('scanner_watch_signals_window', 0) or 0)}"
)
print(
    "        Geschikt 24 uur  : "
    f"{int(data.get('scanner_watch_eligible_window', 0) or 0)}"
)
print(
    "        Afgewezen 24 uur : "
    f"{int(data.get('scanner_watch_rejected_window', 0) or 0)}"
)
print(
    "        Dominant filter  : "
    f"{data.get('scanner_watch_dominant_filter') or '-'} | "
    f"{float(data.get('scanner_watch_dominant_share_pct', 0) or 0):.1f}%"
)
print(
    "        Watchmail actief : "
    f"{'JA' if data.get('scanner_watch_alert_active') else 'NEE'}"
)
print(
    "        Waarschuwingen   : "
    f"{int(data.get('scanner_watch_alert_count', 0) or 0)}"
)
print(
    "        Herstelmails     : "
    f"{int(data.get('scanner_watch_recovery_count', 0) or 0)}"
)
print(
    "        Watchfout        : "
    f"{data.get('scanner_watch_last_error') or '-'}"
)
print(
    "        Readiness-runs   : "
    f"{int(data.get('readiness_gate_runs', 0) or 0)}"
)
print(
    "        Readiness-status : "
    f"{data.get('readiness_gate_last_status') or '-'}"
)
print(
    "        Readiness-fase   : "
    f"{data.get('readiness_gate_last_phase') or '-'}"
)
print(
    "        Testvoortgang    : "
    f"{float(data.get('readiness_gate_test_completion_pct', 0) or 0):.1f}%"
)
print(
    "        Readiness kritiek: "
    f"{int(data.get('readiness_gate_critical_count', 0) or 0)}"
)
print(
    "        Readiness waars. : "
    f"{int(data.get('readiness_gate_warning_count', 0) or 0)}"
)
print(
    "        Readiness stap   : "
    f"{data.get('readiness_gate_last_next_step') or '-'}"
)
print(
    "        Readiness-mail   : "
    f"{int(data.get('readiness_gate_email_count', 0) or 0)}"
)
print(
    "        Laatste gate-mail: "
    f"{data.get('readiness_gate_last_email_at') or '-'}"
)
print(
    "        Readiness-fout   : "
    f"{data.get('readiness_gate_last_error') or '-'}"
)
print(f"        Laatste back-up   : {data.get('last_backup_at') or '-'}")
print(f"        Back-upstatus     : {data.get('last_backup_status') or '-'}")

lab_status = str(
    data.get(
        "last_strategy_lab_refresh_status"
    )
    or ""
).strip().lower()

if lab_status == "failed":
    print("        [FOUT] Laatste directe Strategy Lab-verversing is mislukt")
    raise SystemExit(1)

watch_status = str(
    data.get(
        "scanner_watch_last_status"
    )
    or ""
).strip().upper()

if watch_status == "FOUT":
    print("        [FOUT] Laatste Scannerwatch-controle is mislukt")
    raise SystemExit(1)

readiness_error = str(
    data.get(
        "readiness_gate_last_error"
    )
    or ""
).strip()

if readiness_error:
    print(
        "        [FOUT] Laatste Readiness Gate-run is mislukt: "
        f"{readiness_error}"
    )
    raise SystemExit(1)
PY
    then
        ERRORS=$((ERRORS + 1))
    fi
fi

echo
echo "6. DIAGNOSE"
echo "------------------------------------------------------------"

file_info "$DIAG_STATS_FILE" "Diagnosestatistieken"

if [ -f "$DIAG_STATS_FILE" ]; then
    if ! python3 - "$DIAG_STATS_FILE" "$NOW_EPOCH" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
now_epoch = int(sys.argv[2])

try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"        [FOUT] JSON lezen mislukt: {exc}")
    raise SystemExit(1)

last_round_at = data.get("last_round_at")
age_minutes = None

try:
    dt = datetime.fromisoformat(str(last_round_at).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age_minutes = max(
        0.0,
        (
            datetime.fromtimestamp(now_epoch, tz=timezone.utc)
            - dt.astimezone(timezone.utc)
        ).total_seconds() / 60.0,
    )
except Exception:
    pass

print(f"        Diagnoserondes    : {int(data.get('total_rounds', 0) or 0)}")
print(f"        Laatste ronde     : {last_round_at or '-'}")
print(
    "        Leeftijd ronde   : "
    + (f"{age_minutes:.1f} minuten" if age_minutes is not None else "-")
)

for symbol, stats in sorted((data.get("symbols") or {}).items()):
    print(
        f"          - {symbol}: "
        f"controles={int(stats.get('checks', 0) or 0)}, "
        f"bijna={int(stats.get('near_signals', 0) or 0)}, "
        f"signalen={int(stats.get('technical_signals', 0) or 0)}, "
        f"laatste score={float(stats.get('last_score_pct', 0) or 0):.0f}%"
    )

if age_minutes is None:
    print("[FOUT]  Laatste diagnoseronde heeft geen geldige tijd")
    raise SystemExit(1)

if age_minutes > 35.0:
    print("[FOUT]  Laatste diagnoseronde is ouder dan 35 minuten")
    raise SystemExit(1)

print("[OK]    Periodieke Diagnose is actueel")
PY
    then
        ERRORS=$((ERRORS + 1))
    fi
fi

echo
echo "7. SUPERVISOR"
echo "------------------------------------------------------------"

file_info "$SUPERVISOR_FILE" "Supervisorrapport"

if [ -f "$SUPERVISOR_FILE" ]; then
    if ! python3 - "$SUPERVISOR_FILE" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])

try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"        [FOUT] JSON lezen mislukt: {exc}")
    raise SystemExit(1)

spot = int(data.get("open_spot_positions", 0) or 0)
short = int(data.get("open_short_positions", 0) or 0)

print(f"        Gegenereerd op    : {data.get('generated_at') or '-'}")
print(f"        Modus             : {data.get('mode') or '-'}")
print(f"        Diagnoserondes    : {int(data.get('total_diagnose_rounds', 0) or 0)}")
print(f"        Open spotposities : {spot}")
print(f"        Open shorts       : {short}")
print(f"        Open totaal       : {spot + short}")
print(f"        Gepauzeerd        : {bool(data.get('paused', False))}")

health = data.get("health") or []
recommendations = data.get("recommendations") or []

if health:
    print("        Gezondheid:")
    for item in health:
        print(f"          - {item}")

if recommendations:
    print("        Adviezen:")
    for item in recommendations:
        print(f"          - {item}")
PY
    then
        ERRORS=$((ERRORS + 1))
    fi
fi

echo
echo "8. TRANSACTIES"
echo "------------------------------------------------------------"

if [ -f "$TRADES_FILE" ]; then
    trade_lines=$(wc -l < "$TRADES_FILE")
    trade_count=$((trade_lines > 0 ? trade_lines - 1 : 0))

    echo "[OK]    Transactiebestand aanwezig"
    echo "        Aantal transactieregels: $trade_count"
    echo
    echo "        Laatste vijf regels:"
    tail -n 5 "$TRADES_FILE" | sed 's/^/        /'
else
    echo "[INFO]  Nog geen transactiebestand"
fi

echo
echo "9. TESTVOORTGANG"
echo "------------------------------------------------------------"

if ! python3 - \
    "$STATE_FILE" \
    "$PROJECT_DIR/config.yaml" \
    "$LONG_BASELINE_FILE" \
    "$LONG_REPORT_FILE" \
    "$SHORT_BASELINE_FILE" \
    "$SHORT_REPORT_FILE" \
    "$SHORT_INTERIM_5_FILE" \
    "$SHORT_INTERIM_10_FILE" <<'PY'
import json
import sys
from pathlib import Path

try:
    import yaml
except Exception as exc:
    print(f"[FOUT]  PyYAML laden mislukt: {exc}")
    raise SystemExit(1)

(
    state_file,
    config_file,
    long_baseline_file,
    long_report_file,
    short_baseline_file,
    short_report_file,
    short_interim_5_file,
    short_interim_10_file,
) = [Path(value) for value in sys.argv[1:]]


def load_json(path):
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[FOUT]  JSON lezen mislukt ({path}): {exc}")
        raise SystemExit(1)
    if not isinstance(data, dict):
        print(f"[FOUT]  JSON bevat geen object: {path}")
        raise SystemExit(1)
    return data


def find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            yield obj[key]
        for value in obj.values():
            yield from find_key(value, key)
    elif isinstance(obj, list):
        for value in obj:
            yield from find_key(value, key)


def first_bool(obj, key):
    for value in find_key(obj, key):
        if isinstance(value, bool):
            return value
    return None


def as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def report_status(report_file, reached, target_total, display_name):
    if report_file.exists():
        print(f"[OK]    {display_name}rapport aanwezig")
        print(f"        Bestand           : {report_file}")
    elif reached:
        print(
            f"[WAARSCHUWING] {display_name}doel bereikt, "
            f"maar rapport ontbreekt"
        )
    else:
        print(
            f"[INFO]  {display_name}eindrapport wordt gemaakt "
            f"zodra trade {target_total} is bereikt"
        )


state = load_json(state_file)
long_base = load_json(long_baseline_file)
short_base = load_json(short_baseline_file)

if state is None:
    print(f"[FOUT]  Bot-state ontbreekt: {state_file}")
    raise SystemExit(1)

try:
    config = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
except Exception as exc:
    print(f"[FOUT]  Configuratie lezen mislukt: {exc}")
    raise SystemExit(1)

print("LONGTEST")

if long_base is None:
    print("[FOUT]  Longtestbaseline ontbreekt")
    print(f"        Bestand           : {long_baseline_file}")
    raise SystemExit(1)

long_start = as_int(long_base.get("start_spot_trades"), 0)
long_target_total = as_int(long_base.get("target_total_trades"), 0)

if long_target_total <= long_start:
    target_new = as_int(long_base.get("target_new_trades"), 20)
    long_target_total = long_start + max(0, target_new)

long_current = as_int(state.get("trades"), 0)
long_new = max(0, long_current - long_start)
long_target_new = max(0, long_target_total - long_start)
long_remaining = max(0, long_target_total - long_current)
long_reached = long_current >= long_target_total and long_target_total > 0
long_dry_run = first_bool(config, "dry_run")

print("[OK]    Longtestbaseline actief")
print(f"        Bestand           : {long_baseline_file}")
print(f"        Start trades      : {long_start}")
print(f"        Huidige trades    : {long_current}")
print(f"        Nieuwe testtrades : {long_new}/{long_target_new}")
print(f"        Nog nodig         : {long_remaining}")
print(f"        Doel totaal       : {long_target_total}")
print(
    f"        Dry-run           : "
    f"{'JA' if long_dry_run is True else 'NEE' if long_dry_run is False else '-'}"
)
print(
    f"        Teststop actief   : "
    f"{'JA' if long_dry_run is True else 'NEE' if long_dry_run is False else '-'}"
)
print(f"        Doel bereikt      : {'JA' if long_reached else 'NEE'}")

if long_dry_run is not True:
    print("[FOUT]  Longtest staat niet aantoonbaar in dry-run")
    raise SystemExit(1)

report_status(
    long_report_file,
    long_reached,
    long_target_total,
    "Longtest",
)

print()
print("PAPER-SHORTTEST")

if short_base is None:
    print("[FOUT]  Paper-shortbaseline ontbreekt")
    print(f"        Bestand           : {short_baseline_file}")
    raise SystemExit(1)

short_start = as_int(short_base.get("start_short_trades"), 0)
short_target_total = as_int(short_base.get("target_total_short_trades"), 0)

if short_target_total <= short_start:
    target_new = as_int(short_base.get("target_new_short_trades"), 20)
    short_target_total = short_start + max(0, target_new)

short_current = as_int(state.get("short_trades"), 0)
short_new = max(0, short_current - short_start)
short_target_new = max(0, short_target_total - short_start)
short_remaining = max(0, short_target_total - short_current)
short_reached = short_current >= short_target_total and short_target_total > 0
short_settings = short_base.get("settings") or {}
short_paper_only = short_settings.get("paper_only")

if not isinstance(short_paper_only, bool):
    short_paper_only = first_bool(config, "paper_only")

max_open = short_settings.get("max_open_positions", 1)
leverage = short_settings.get("leverage", 1)
strategy_version = short_settings.get("strategy_version") or "-"

print("[OK]    Paper-shortbaseline actief")
print(f"        Bestand           : {short_baseline_file}")
print(f"        Strategie         : {strategy_version}")
print(f"        Start shorts      : {short_start}")
print(f"        Huidige shorts    : {short_current}")
print(f"        Nieuwe shorts     : {short_new}/{short_target_new}")
print(f"        Nog nodig         : {short_remaining}")
print(f"        Doel totaal       : {short_target_total}")
print(
    f"        Paper only        : "
    f"{'JA' if short_paper_only is True else 'NEE' if short_paper_only is False else '-'}"
)
print(f"        Maximaal open     : {max_open}")
print(f"        Hefboom           : {leverage}x")
print(f"        Doel bereikt      : {'JA' if short_reached else 'NEE'}")

if short_paper_only is not True:
    print("[FOUT]  Paper-shorttest staat niet aantoonbaar op paper-only")
    raise SystemExit(1)

if short_interim_5_file.exists():
    print("[OK]    Tussenrapport 5/20 aanwezig")
    print(f"        Bestand           : {short_interim_5_file}")
else:
    print("[INFO]  Tussenrapport 5/20 nog niet aanwezig")

if short_interim_10_file.exists():
    print("[OK]    Tussenrapport 10/20 aanwezig")
    print(f"        Bestand           : {short_interim_10_file}")
else:
    print("[INFO]  Tussenrapport 10/20 nog niet aanwezig")

report_status(
    short_report_file,
    short_reached,
    short_target_total,
    "Paper-short",
)
PY
then
    ERRORS=$((ERRORS + 1))
fi

echo
echo "10. PAPER-SHORTDIAGNOSE"
echo "------------------------------------------------------------"

if [ -f "$PROJECT_DIR/short_diagnose.py" ]; then
    if python3 -m py_compile "$PROJECT_DIR/short_diagnose.py" 2>/dev/null; then
        echo "[OK]    short_diagnose.py Pythoncontrole geslaagd"
        echo "[INFO]  Volledige paper-shortdiagnose wordt hier bewust niet gestart"
        echo "        Reden: geheugenarme healthcheck voorkomt een extra RAM-piek."
        echo "        De paper-shortveiligheid en testvoortgang zijn hierboven gecontroleerd."
    else
        echo "[FOUT]  short_diagnose.py Pythoncontrole mislukt"
        ERRORS=$((ERRORS + 1))
    fi
else
    echo "[FOUT]  short_diagnose.py ontbreekt"
    ERRORS=$((ERRORS + 1))
fi

echo
echo "11. DAGELIJKSE BACK-UP"
echo "------------------------------------------------------------"

if [ -d "$BACKUP_DIR" ]; then
    if ! python3 - "$BACKUP_DIR" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

backup_root = Path(sys.argv[1])
directories = sorted(
    [path for path in backup_root.iterdir() if path.is_dir()],
    key=lambda path: path.stat().st_mtime,
    reverse=True,
)

if not directories:
    print("[FOUT]  Nog geen dagelijkse back-upmap gevonden")
    raise SystemExit(1)

latest = directories[0]
manifest_file = latest / "manifest.json"

if not manifest_file.exists():
    print("[FOUT]  Manifest ontbreekt in nieuwste back-up")
    print(f"        Map                : {latest}")
    raise SystemExit(1)

try:
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"[FOUT]  Manifest lezen mislukt: {exc}")
    raise SystemExit(1)

created_at = manifest.get("created_at") or "-"
created = None

try:
    created = datetime.fromisoformat(
        str(created_at).replace("Z", "+00:00")
    )
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
except Exception:
    pass

age_hours = (
    max(
        0.0,
        (
            datetime.now(timezone.utc)
            - created.astimezone(timezone.utc)
        ).total_seconds()
        / 3600.0,
    )
    if created is not None
    else 0.0
)

copied = manifest.get("copied_files") or []
file_count = int(manifest.get("file_count", len(copied)) or 0)
total_bytes = int(manifest.get("total_bytes", 0) or 0)
retention = int(manifest.get("retention_days", 30) or 30)
status = manifest.get("status") or "-"
required_missing = manifest.get("required_missing") or []

integrity_ok = (
    status == "complete"
    and not required_missing
    and file_count == len(copied)
)

print("[OK]    Dagelijkse back-up aanwezig en gecontroleerd")
print(f"        Map                : {latest}")
print(f"        Gemaakt op         : {created_at}")
print(f"        Leeftijd           : {age_hours:.1f} uur")
print(f"        Bestanden          : {file_count}")
print(f"        Totale grootte     : {total_bytes} bytes")
print(f"        Bewaartermijn      : {retention} dagen")
print(f"        Integriteit        : {'OK' if integrity_ok else 'NIET OK'}")
print(f"        Back-ups aanwezig  : {len(directories)}")

if not integrity_ok:
    raise SystemExit(1)
PY
    then
        ERRORS=$((ERRORS + 1))
    fi
else
    echo "[FOUT]  Back-upmap ontbreekt"
    echo "        Map                : $BACKUP_DIR"
    ERRORS=$((ERRORS + 1))
fi

echo
echo "12. STRATEGY LAB"
echo "------------------------------------------------------------"

file_info "$STRATEGY_LAB_JSON_FILE" "Strategy Lab JSON" "true"
file_info "$STRATEGY_LAB_TEXT_FILE" "Strategy Lab tekstrapport" "true"
file_info "$STRATEGY_LAB_GROUPS_FILE" "Strategy Lab groepen-CSV" "true"
file_info "$STRATEGY_LAB_RUNNER_LOG" "Strategy Lab runnerlog" "true"

if [ -f "$PROJECT_DIR/strategy_lab.py" ]; then
    if python3 -m py_compile "$PROJECT_DIR/strategy_lab.py" 2>/dev/null; then
        echo "[OK]    Strategy Lab Pythoncontrole geslaagd"
    else
        echo "[FOUT]  Strategy Lab Pythoncontrole mislukt"
        ERRORS=$((ERRORS + 1))
    fi
fi

if     grep -q "def append_strategy_lab_status" "$PROJECT_DIR/agent.py"     && grep -q "def append_strategy_lab_weekly" "$PROJECT_DIR/agent.py"     && grep -q "Strategy Lab e-mailintegratie: statusmail en weekrapport" "$PROJECT_DIR/agent.py"
then
    echo "[OK]    Strategy Lab is gekoppeld aan statusmail en weekrapport"
else
    echo "[FOUT]  Strategy Lab e-mailintegratie ontbreekt of is onvolledig"
    ERRORS=$((ERRORS + 1))
fi

if     grep -q "def handle_scanner_watch_alerts" "$PROJECT_DIR/agent.py"     && grep -q "def analyse_scanner_watch" "$PROJECT_DIR/agent.py"     && grep -q "Scannerbewaking: 24 uur stilte en dominant afwijzingsfilter" "$PROJECT_DIR/agent.py"
then
    echo "[OK]    Scannerbewaking en waarschuwingse-mails zijn actief"
else
    echo "[FOUT]  Scannerbewaking ontbreekt of is onvolledig"
    ERRORS=$((ERRORS + 1))
fi

if \
    grep -q "def refresh_readiness_gate" "$PROJECT_DIR/agent.py" \
    && grep -q "def append_readiness_gate_status" "$PROJECT_DIR/agent.py" \
    && grep -q "Readiness Gate: centrale alleen-lezen gereedheidscontrole" "$PROJECT_DIR/agent.py"
then
    echo "[OK]    Readiness Gate is gekoppeld aan Agent, statusmail en weekrapport"
else
    echo "[FOUT]  Readiness Gate-integratie ontbreekt of is onvolledig"
    ERRORS=$((ERRORS + 1))
fi

if [ -f "$STRATEGY_LAB_JSON_FILE" ]; then
    if ! python3 - \
        "$STRATEGY_LAB_JSON_FILE" \
        "$NOW_EPOCH" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
now_epoch = int(sys.argv[2])

try:
    report = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"[FOUT]  Strategy Lab JSON lezen mislukt: {exc}")
    raise SystemExit(1)

if not isinstance(report, dict):
    print("[FOUT]  Strategy Lab JSON bevat geen object")
    raise SystemExit(1)

generated_at = report.get("generated_at")
generated = None

try:
    generated = datetime.fromisoformat(
        str(generated_at).replace("Z", "+00:00")
    )
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    generated = generated.astimezone(timezone.utc)
except Exception:
    pass

if generated is None:
    print("[FOUT]  Strategy Lab heeft geen geldige genereertijd")
    raise SystemExit(1)

age_minutes = max(
    0.0,
    (
        datetime.fromtimestamp(now_epoch, tz=timezone.utc)
        - generated
    ).total_seconds()
    / 60.0,
)

safety = report.get("safety") or {}
signals = report.get("signals") or {}
shadow = report.get("shadow_trades") or {}
scanner_state = report.get("scanner_state") or {}
errors = report.get("errors") or []
open_positions = scanner_state.get("open_positions") or []

safe = (
    safety.get("orders_possible") is False
    and safety.get("exchange_connection_used") is False
    and safety.get("bot_state_modified") is False
    and safety.get("scanner_state_modified") is False
    and safety.get("settings_modified") is False
    and safety.get("automatic_strategy_changes") is False
)

print("[OK]    Strategy Lab rapport leesbaar")
print(f"        Versie             : {report.get('version') or '-'}")
print(f"        Modus              : {report.get('mode') or '-'}")
print(f"        Gegenereerd        : {generated_at}")
print(f"        Leeftijd rapport   : {age_minutes:.1f} minuten")
print(f"        Scans totaal       : {int(scanner_state.get('scan_count', 0) or 0)}")
print(f"        Signalen CSV       : {int(signals.get('signals', 0) or 0)}")
print(f"        Filters gepasseerd : {int(signals.get('shadow_eligible', 0) or 0)}")
print(f"        Open schaduw       : {len(open_positions)}")
print(f"        Gesloten schaduw   : {int(shadow.get('trades', 0) or 0)}")
print(f"        Winrate            : {float(shadow.get('winrate_pct', 0) or 0):.2f}%")
print(f"        Nettoresultaat     : €{float(shadow.get('net_pnl_eur', 0) or 0):+.4f}")
closed_count = int(shadow.get("trades", 0) or 0)

next_milestone = None

for milestone in (5, 10, 20):
    if closed_count < milestone:
        next_milestone = milestone
        break

if next_milestone is None:
    milestone_text = "20/20 bereikt"
    remaining = 0
else:
    remaining = max(0, next_milestone - closed_count)
    milestone_text = f"{closed_count}/{next_milestone}"

print(f"        Datastatus         : {shadow.get('data_status') or '-'}")
print(f"        Volgende mijlpaal  : {milestone_text}")
print(f"        Nog nodig          : {remaining}")
print(f"        Rapportfouten      : {len(errors)}")
print(f"        Alleen-lezen       : {'JA' if safe else 'NEE'}")

if str(report.get("version") or "") != "1.0":
    print("[FOUT]  Onverwachte Strategy Lab-versie")
    raise SystemExit(1)

if report.get("mode") != "READ_ONLY_STRATEGY_ANALYSIS":
    print("[FOUT]  Onverwachte Strategy Lab-modus")
    raise SystemExit(1)

if not safe:
    print("[FOUT]  Strategy Lab veiligheidsstatus is niet volledig alleen-lezen")
    raise SystemExit(1)

# Het proces draait iedere zes uur. Een marge van 30 minuten voorkomt
# onnodige foutmeldingen tijdens een rapportcyclus of deploy.
if age_minutes > 390.0:
    print("[FOUT]  Strategy Lab rapport is ouder dan 390 minuten")
    raise SystemExit(1)

print("[OK]    Strategy Lab is actueel en veilig")
PY
    then
        ERRORS=$((ERRORS + 1))
    fi
fi

echo
echo "13. SCHADUWMIJLPAALRAPPORTEN"
echo "------------------------------------------------------------"

python3 - \
    "$STRATEGY_LAB_JSON_FILE" \
    "$SHADOW_MILESTONE_5_JSON" \
    "$SHADOW_MILESTONE_10_JSON" \
    "$SHADOW_MILESTONE_20_JSON" <<'PY'
import json
import sys
from pathlib import Path

strategy_path = Path(sys.argv[1])
report_paths = {
    5: Path(sys.argv[2]),
    10: Path(sys.argv[3]),
    20: Path(sys.argv[4]),
}

closed = 0

if strategy_path.is_file():
    try:
        strategy = json.loads(
            strategy_path.read_text(
                encoding="utf-8"
            )
        )
        shadow = strategy.get("shadow_trades") or {}
        closed = int(
            shadow.get("trades", 0)
            or 0
        )
    except Exception:
        closed = 0

next_milestone = None

for milestone in (5, 10, 20):
    if closed < milestone:
        next_milestone = milestone
        break

if next_milestone is None:
    print("[OK]    Alle vaste mijlpalen 5/10/20 zijn bereikt")
else:
    print(
        f"[INFO]  Voortgang: {closed}/{next_milestone} "
        f"| nog {next_milestone - closed} gesloten trades nodig"
    )

for milestone, path in report_paths.items():
    if not path.is_file():
        if closed >= milestone:
            print(
                f"[FOUT]  Mijlpaalrapport {milestone}/20 ontbreekt "
                f"terwijl {closed} trades zijn gesloten"
            )
        else:
            print(
                f"[INFO]  Mijlpaalrapport {milestone}/20 nog niet verwacht"
            )
        continue

    try:
        report = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )
    except Exception as exc:
        print(
            f"[FOUT]  Mijlpaalrapport {milestone}/20 onleesbaar: {exc}"
        )
        continue

    summary = report.get("summary") or {}

    print(
        f"[OK]    Mijlpaalrapport {milestone}/20 aanwezig | "
        f"trades={summary.get('trades', 0)} | "
        f"winrate={float(summary.get('winrate_pct', 0) or 0):.2f}% | "
        f"pnl=€{float(summary.get('net_pnl_eur', 0) or 0):+.4f} | "
        f"mail={'JA' if report.get('email_sent_at') else 'NEE'}"
    )
PY

closed_shadow_count=0

if [ -f "$STRATEGY_LAB_JSON_FILE" ]; then
    closed_shadow_count=$(
        python3 - "$STRATEGY_LAB_JSON_FILE" <<'PY'
import json
import sys
from pathlib import Path

try:
    data = json.loads(
        Path(sys.argv[1]).read_text(
            encoding="utf-8"
        )
    )
    print(
        int(
            (
                data.get("shadow_trades")
                or {}
            ).get("trades", 0)
            or 0
        )
    )
except Exception:
    print(0)
PY
    )
fi

if [ "$closed_shadow_count" -ge 5 ] && [ ! -f "$SHADOW_MILESTONE_5_JSON" ]; then
    ERRORS=$((ERRORS + 1))
fi

if [ "$closed_shadow_count" -ge 10 ] && [ ! -f "$SHADOW_MILESTONE_10_JSON" ]; then
    ERRORS=$((ERRORS + 1))
fi

if [ "$closed_shadow_count" -ge 20 ] && [ ! -f "$SHADOW_MILESTONE_20_JSON" ]; then
    ERRORS=$((ERRORS + 1))
fi

for report_file in \
    "$SHADOW_MILESTONE_5_TEXT" \
    "$SHADOW_MILESTONE_10_TEXT" \
    "$SHADOW_MILESTONE_20_TEXT"
do
    if [ -f "$report_file" ]; then
        echo "[OK]    Tekstrapport aanwezig: $report_file"
    fi
done

echo
echo "14. READINESS GATE"
echo "------------------------------------------------------------"

file_info "$READINESS_GATE_JSON_FILE" "Readiness Gate JSON" "true"
file_info "$READINESS_GATE_TEXT_FILE" "Readiness Gate tekstrapport" "true"

if [ -f "$PROJECT_DIR/readiness_gate.py" ]; then
    if python3 -m py_compile "$PROJECT_DIR/readiness_gate.py" 2>/dev/null; then
        echo "[OK]    Readiness Gate Pythoncontrole geslaagd"
    else
        echo "[FOUT]  Readiness Gate Pythoncontrole mislukt"
        ERRORS=$((ERRORS + 1))
    fi

    if python3 "$PROJECT_DIR/readiness_gate.py" --self-test 2>/dev/null | grep -q "READINESS_GATE_SELF_TEST_OK"; then
        echo "[OK]    Readiness Gate interne statustest geslaagd"
    else
        echo "[FOUT]  Readiness Gate interne statustest mislukt"
        ERRORS=$((ERRORS + 1))
    fi
fi

if [ -f "$READINESS_GATE_JSON_FILE" ]; then
    if ! python3 - "$READINESS_GATE_JSON_FILE" "$NOW_EPOCH" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
now_epoch = int(sys.argv[2])

try:
    report = json.loads(
        path.read_text(
            encoding="utf-8"
        )
    )
except Exception as exc:
    print(f"[FOUT]  Readiness Gate JSON lezen mislukt: {exc}")
    raise SystemExit(1)

if not isinstance(report, dict):
    print("[FOUT]  Readiness Gate JSON bevat geen object")
    raise SystemExit(1)

try:
    generated = datetime.fromisoformat(
        str(
            report.get(
                "generated_at"
            )
        ).replace(
            "Z",
            "+00:00",
        )
    )

    if generated.tzinfo is None:
        generated = generated.replace(
            tzinfo=timezone.utc
        )

    generated = generated.astimezone(
        timezone.utc
    )

except Exception:
    print("[FOUT]  Readiness Gate heeft geen geldige genereertijd")
    raise SystemExit(1)

age_minutes = max(
    0.0,
    (
        datetime.fromtimestamp(
            now_epoch,
            tz=timezone.utc,
        )
        - generated
    ).total_seconds()
    / 60.0,
)

progress = (
    report.get(
        "test_progress"
    )
    or {}
)

long_progress = (
    progress.get(
        "long"
    )
    or {}
)

short_progress = (
    progress.get(
        "paper_short"
    )
    or {}
)

shadow_progress = (
    progress.get(
        "shadow"
    )
    or {}
)

safety = (
    report.get(
        "safety"
    )
    or {}
)

safe = (
    safety.get(
        "orders_possible"
    )
    is False
    and safety.get(
        "exchange_connection_used"
    )
    is False
    and safety.get(
        "bot_state_modified"
    )
    is False
    and safety.get(
        "control_state_modified"
    )
    is False
    and safety.get(
        "scanner_state_modified"
    )
    is False
    and safety.get(
        "settings_modified"
    )
    is False
    and safety.get(
        "automatic_live_activation"
    )
    is False
    and safety.get(
        "manual_live_approval_required"
    )
    is True
)

print("[OK]    Readiness Gate rapport leesbaar")
print(f"        Versie             : {report.get('version') or '-'}")
print(f"        Modus              : {report.get('mode') or '-'}")
print(f"        Gegenereerd        : {report.get('generated_at') or '-'}")
print(f"        Leeftijd rapport   : {age_minutes:.1f} minuten")
print(f"        Centrale status    : {report.get('status') or '-'}")
print(f"        Huidige fase       : {report.get('phase') or '-'}")
print(f"        Testvoortgang      : {float(report.get('test_completion_pct', 0) or 0):.1f}%")
print(
    "        Longtest          : "
    f"{int(long_progress.get('completed', 0) or 0)}/"
    f"{int(long_progress.get('target', 20) or 20)}"
)
print(
    "        Paper-shorttest   : "
    f"{int(short_progress.get('completed', 0) or 0)}/"
    f"{int(short_progress.get('target', 20) or 20)}"
)
print(
    "        Schaduwtest       : "
    f"{int(shadow_progress.get('completed', 0) or 0)}/"
    f"{int(shadow_progress.get('target', 20) or 20)}"
)
print(f"        Kritieke problemen: {int(report.get('critical_failure_count', 0) or 0)}")
print(f"        Waarschuwingen     : {int(report.get('warning_count', 0) or 0)}")
print(f"        Volgende stap      : {report.get('next_step') or '-'}")
print(f"        Alleen-lezen       : {'JA' if safe else 'NEE'}")
print("        Automatisch live   : NEE")

if str(report.get("version") or "") != "1.3":
    print("[FOUT]  Onverwachte Readiness Gate-versie")
    raise SystemExit(1)

if report.get("mode") != "READ_ONLY_READINESS_GATE":
    print("[FOUT]  Onverwachte Readiness Gate-modus")
    raise SystemExit(1)

if not safe:
    print("[FOUT]  Readiness Gate is niet volledig alleen-lezen")
    raise SystemExit(1)

if age_minutes > 35.0:
    print("[FOUT]  Readiness Gate-rapport is ouder dan 35 minuten")
    raise SystemExit(1)

if int(report.get("critical_failure_count", 0) or 0) > 0:
    print("[FOUT]  Readiness Gate meldt één of meer kritieke problemen")
    raise SystemExit(1)

print("[OK]    Readiness Gate is actueel en veilig")
PY
    then
        ERRORS=$((ERRORS + 1))
    fi
fi

if [ -f "$FINAL_VALIDATION_FILE" ]; then
    echo "[OK]    Definitieve eindtestregistratie aanwezig"
else
    echo "[INFO]  Definitieve eindtestregistratie nog niet verwacht"
fi

if [ -f "$LIVE_APPROVAL_FILE" ]; then
    echo "[OK]    Handmatig live-goedkeuringsbestand aanwezig"
else
    echo "[INFO]  Handmatige live-goedkeuring nog niet aanwezig"
fi

echo
echo "15. SCHIJFRUIMTE"
echo "------------------------------------------------------------"

df -h "$DATA_DIR" 2>/dev/null || df -h

echo
echo "16. EINDCONTROLE"
echo "------------------------------------------------------------"

if [ "$ERRORS" -eq 0 ]; then
    echo "[OK]    Alle belangrijke onderdelen zijn actief."
    EXIT_CODE=0
else
    echo "[FOUT]  Er zijn $ERRORS belangrijke problemen gevonden."
    EXIT_CODE=1
fi

echo
echo "============================================================"
echo " CONTROLE AFGEROND"
echo "============================================================"
echo

exit "$EXIT_CODE"
