#!/usr/bin/env bash

# Diamond Trader Healthcheck v7.8
# Alleen lezen: wijzigt geen bot-, test-, scanner-, Strategy Lab- of Readiness-bestanden.

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
    'python3[[:space:]]+closed_candle_runner\.py[[:space:]]+diagnose([[:space:]]|$)' \
    "Diamond Diagnose"

check_process \
    'python3[[:space:]]+supervisor_agent\.py([[:space:]]|$)' \
    "Diamond Supervisor"

check_process \
    'python3[[:space:]]+closed_candle_runner\.py[[:space:]]+bot([[:space:]]|$)' \
    "Diamond Bot"

check_process \
    'python3[[:space:]]+market_scanner\.py[[:space:]]+--loop[[:space:]]+--top[[:space:]]+20([[:space:]]|$)' \
    "Diamond Market Scanner"

check_process \
    'python3[[:space:]]+strategy_lab\.py[[:space:]]+--loop[[:space:]]+--interval-minutes[[:space:]]+360[[:space:]]+--no-print([[:space:]]|$)' \
    "Diamond Strategy Lab"

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
    scanner_healthcheck.sh
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
    if ! python3 - "$DIAG_STATS_FILE" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])

try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"        [FOUT] JSON lezen mislukt: {exc}")
    raise SystemExit(1)

print(f"        Diagnoserondes    : {int(data.get('total_rounds', 0) or 0)}")
print(f"        Laatste ronde     : {data.get('last_round_at') or '-'}")

for symbol, stats in sorted((data.get("symbols") or {}).items()):
    print(
        f"          - {symbol}: "
        f"controles={int(stats.get('checks', 0) or 0)}, "
        f"bijna={int(stats.get('near_signals', 0) or 0)}, "
        f"signalen={int(stats.get('technical_signals', 0) or 0)}, "
        f"laatste score={float(stats.get('last_score_pct', 0) or 0):.0f}%"
    )
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
    "$PROJECT_DIR" \
    "$LONG_BASELINE_FILE" \
    "$LONG_REPORT_FILE" \
    "$SHORT_BASELINE_FILE" \
    "$SHORT_REPORT_FILE" \
    "$SHORT_INTERIM_5_FILE" \
    "$SHORT_INTERIM_10_FILE" <<'PY'
import importlib.util
import sys
from pathlib import Path

(
    project_dir,
    long_baseline_file,
    long_report_file,
    short_baseline_file,
    short_report_file,
    short_interim_5_file,
    short_interim_10_file,
) = [
    Path(value)
    for value in sys.argv[1:]
]

agent_file = project_dir / "agent.py"

if not agent_file.exists():
    print(f"[FOUT]  agent.py ontbreekt: {agent_file}")
    raise SystemExit(1)

spec = importlib.util.spec_from_file_location(
    "diamond_healthcheck_agent",
    agent_file,
)

if spec is None or spec.loader is None:
    print("[FOUT]  agent.py kon niet worden geladen")
    raise SystemExit(1)

agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


def report_status(
    report_file,
    reached,
    target_total,
    display_name,
):
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


try:
    long_status = agent.get_test_target_status()
except Exception as exc:
    print(
        f"[FOUT]  Longteststatus opvragen mislukt: {exc}"
    )
    raise SystemExit(1)

print("LONGTEST")

if not long_status.get("enabled", False):
    print(
        "[FOUT]  Longtestbaseline is niet geldig of niet actief"
    )
    print(
        f"        Bestand           : {long_baseline_file}"
    )
    print(
        f"        Reden             : "
        f"{long_status.get('reason') or 'onbekend'}"
    )
    raise SystemExit(1)

long_start = int(
    long_status.get("start_trades", 0)
    or 0
)
long_target_total = int(
    long_status.get("target_total_trades", 0)
    or 0
)
long_current = int(
    long_status.get("current_trades", 0)
    or 0
)
long_new = int(
    long_status.get("new_trades", 0)
    or 0
)
long_remaining = int(
    long_status.get("remaining_trades", 0)
    or 0
)
long_target_new = max(
    0,
    long_target_total - long_start,
)
long_dry_run = bool(
    long_status.get("dry_run", False)
)
long_reached = bool(
    long_status.get("target_reached", False)
)

print("[OK]    Longtestbaseline actief")
print(f"        Bestand           : {long_baseline_file}")
print(f"        Start trades      : {long_start}")
print(f"        Huidige trades    : {long_current}")
print(
    f"        Nieuwe testtrades : "
    f"{long_new}/{long_target_new}"
)
print(f"        Nog nodig         : {long_remaining}")
print(f"        Doel totaal       : {long_target_total}")
print(
    f"        Dry-run           : "
    f"{'JA' if long_dry_run else 'NEE'}"
)
print(
    f"        Teststop actief   : "
    f"{'JA' if long_dry_run else 'NEE'}"
)
print(
    f"        Doel bereikt      : "
    f"{'JA' if long_reached else 'NEE'}"
)

report_status(
    long_report_file,
    long_reached,
    long_target_total,
    "Longtest",
)

print()
print("PAPER-SHORTTEST")

try:
    short_status = agent.get_short_test_target_status()
except Exception as exc:
    print(
        f"[FOUT]  Paper-shortstatus opvragen mislukt: {exc}"
    )
    raise SystemExit(1)

if not short_status.get("enabled", False):
    print(
        "[FOUT]  Paper-shorttest is niet geldig of niet actief"
    )
    print(
        f"        Bestand           : {short_baseline_file}"
    )
    print(
        f"        Reden             : "
        f"{short_status.get('reason') or 'onbekend'}"
    )
    raise SystemExit(1)

short_start = int(
    short_status.get("start_short_trades", 0)
    or 0
)
short_target_total = int(
    short_status.get(
        "target_total_short_trades",
        0,
    )
    or 0
)
short_current = int(
    short_status.get("current_short_trades", 0)
    or 0
)
short_new = int(
    short_status.get("new_short_trades", 0)
    or 0
)
short_remaining = int(
    short_status.get(
        "remaining_short_trades",
        0,
    )
    or 0
)
short_target_new = max(
    0,
    short_target_total - short_start,
)
short_reached = bool(
    short_status.get("target_reached", False)
)

print("[OK]    Paper-shortbaseline actief")
print(f"        Bestand           : {short_baseline_file}")
print(f"        Start shorts      : {short_start}")
print(f"        Huidige shorts    : {short_current}")
print(
    f"        Nieuwe shorts     : "
    f"{short_new}/{short_target_new}"
)
print(f"        Nog nodig         : {short_remaining}")
print(f"        Doel totaal       : {short_target_total}")
print("        Paper only        : JA")
print("        Maximaal open     : 1")
print("        Hefboom           : 1x")
print(
    f"        Doel bereikt      : "
    f"{'JA' if short_reached else 'NEE'}"
)

if short_interim_5_file.exists():
    print("[OK]    Tussenrapport 5/20 aanwezig")
    print(
        f"        Bestand           : "
        f"{short_interim_5_file}"
    )
else:
    print("[INFO]  Tussenrapport 5/20 nog niet aanwezig")

if short_interim_10_file.exists():
    print("[OK]    Tussenrapport 10/20 aanwezig")
    print(
        f"        Bestand           : "
        f"{short_interim_10_file}"
    )
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
    echo "[INFO]  Veilige paper-shortdiagnose wordt uitgevoerd"
    echo "        Alleen lezen: geen orders en geen bestanden gewijzigd"
    echo

    if (
        cd "$PROJECT_DIR"
        python3 short_diagnose.py
    ); then
        echo
        echo "[OK]    Paper-shortdiagnose succesvol afgerond"
    else
        echo
        echo "[FOUT]  Paper-shortdiagnose mislukt"
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

if report.get("version") != "1.0":
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

if report.get("version") != "1.0":
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
