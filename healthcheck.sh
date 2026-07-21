#!/usr/bin/env bash

# Diamond Trader Healthcheck
# Alleen lezen: dit script verandert niets aan de bot of instellingen.

set -u

DATA_DIR="/var/data"
PROJECT_DIR="/opt/render/project/src"

STATE_FILE="$DATA_DIR/diamond_state.json"
CONTROL_FILE="$DATA_DIR/diamond_control.json"
AGENT_STATE_FILE="$DATA_DIR/diamond_agent_state.json"
DIAG_STATS_FILE="$DATA_DIR/diamond_diagnose_stats.json"
SUPERVISOR_FILE="$DATA_DIR/diamond_supervisor_state.json"
TRADES_FILE="$DATA_DIR/diamond_transactions.csv"
TEST_BASELINE_FILE="$DATA_DIR/diamond_test_baseline.json"
TEST_REPORT_FILE="$DATA_DIR/diamond_test_report.json"

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

check_file() {
    local file_path="$1"
    local display_name="$2"
    local required="${3:-false}"

    if [ -f "$file_path" ]; then
        local modified_epoch
        local age_seconds
        local age_minutes
        local size_bytes

        modified_epoch=$(stat -c %Y "$file_path" 2>/dev/null || echo 0)
        age_seconds=$((NOW_EPOCH - modified_epoch))
        age_minutes=$((age_seconds / 60))
        size_bytes=$(stat -c %s "$file_path" 2>/dev/null || echo 0)

        echo "[OK]    $display_name aanwezig"
        echo "        Bestand: $file_path"
        echo "        Grootte: $size_bytes bytes"
        echo "        Laatst gewijzigd: $age_minutes minuten geleden"
    else
        if [ "$required" = "true" ]; then
            echo "[FOUT]  $display_name ontbreekt"
            ERRORS=$((ERRORS + 1))
        else
            echo "[INFO]  $display_name nog niet aanwezig"
        fi

        echo "        Bestand: $file_path"
    fi
}

show_json_summary() {
    local file_path="$1"
    local summary_type="$2"

    if [ ! -f "$file_path" ]; then
        return
    fi

    if ! python3 - "$file_path" "$summary_type" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
summary_type = sys.argv[2]

try:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
except Exception as exc:
    print(f"        [FOUT] JSON lezen mislukt: {exc}")
    raise SystemExit(1)


def format_timestamp(value):
    if value in (None, "", 0):
        return "-"

    try:
        return datetime.fromtimestamp(
            float(value),
            tz=timezone.utc,
        ).isoformat()
    except (TypeError, ValueError, OSError):
        return str(value)


if summary_type == "bot":
    positions = data.get("positions") or {}
    shorts = data.get("short_positions") or {}

    print(f"        Open spotposities : {len(positions)}")
    print(f"        Open shorts       : {len(shorts)}")
    print(f"        Spot trades       : {data.get('trades', 0)}")
    print(f"        Spot winsttrades  : {data.get('wins', 0)}")
    print(
        f"        Spot PnL          : "
        f"{float(data.get('pnl_quote', 0) or 0):+.2f} EUR"
    )
    print(
        f"        Dry-run saldo     : "
        f"{float(data.get('simulated_free_quote', 0) or 0):.2f} EUR"
    )

    if positions:
        print("        Posities:")

        for symbol, position in positions.items():
            amount = float(position.get("amount", 0) or 0)
            quote_amount = float(position.get("quote_amount", 0) or 0)
            opened_by_bot = bool(position.get("opened_by_bot", False))

            print(
                f"          - {symbol}: {amount:.8f} "
                f"(€{quote_amount:.2f}) | door bot={opened_by_bot}"
            )

elif summary_type == "control":
    print(f"        Gepauzeerd        : {bool(data.get('paused', False))}")
    print(f"        Reden             : {data.get('pause_reason') or '-'}")
    print(f"        Gepauzeerd sinds  : {data.get('paused_at') or '-'}")

elif summary_type == "diagnose":
    print(f"        Diagnoserondes    : {data.get('total_rounds', 0)}")
    print(f"        Laatste ronde     : {data.get('last_round_at') or '-'}")

    symbols = data.get("symbols") or {}

    for symbol, stats in sorted(symbols.items()):
        checks = int(stats.get("checks", 0) or 0)
        near = int(stats.get("near_signals", 0) or 0)
        signals = int(stats.get("technical_signals", 0) or 0)
        score = float(stats.get("last_score_pct", 0) or 0)

        print(
            f"          - {symbol}: controles={checks}, "
            f"bijna={near}, signalen={signals}, "
            f"laatste score={score:.0f}%"
        )

elif summary_type == "supervisor":
    spot_open = int(data.get("open_spot_positions", 0) or 0)
    short_open = int(data.get("open_short_positions", 0) or 0)

    print(f"        Gegenereerd op    : {data.get('generated_at') or '-'}")
    print(f"        Modus             : {data.get('mode') or '-'}")
    print(
        f"        Diagnoserondes    : "
        f"{data.get('total_diagnose_rounds', 0)}"
    )
    print(f"        Open spotposities : {spot_open}")
    print(f"        Open shorts       : {short_open}")
    print(f"        Open totaal       : {spot_open + short_open}")
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

elif summary_type == "agent":
    last_analysis = data.get("last_analysis_ts", 0)

    print(
        f"        Laatste analyse   : "
        f"{format_timestamp(last_analysis)}"
    )

    sent_reports = (
        data.get("sent_reports")
        or data.get("sent_daily_reports")
        or []
    )
    weekly_reports = data.get("sent_weekly_reports") or []

    print(f"        Statusmails       : {len(sent_reports)}")
    print(f"        Weekrapporten     : {len(weekly_reports)}")
PY
    then
        ERRORS=$((ERRORS + 1))
    fi
}

show_test_progress() {
    if [ ! -f "$TEST_BASELINE_FILE" ]; then
        echo "[FOUT]  Testbaseline ontbreekt"
        echo "        Bestand: $TEST_BASELINE_FILE"
        ERRORS=$((ERRORS + 1))
        return
    fi

    if ! python3 - \
        "$PROJECT_DIR" \
        "$TEST_BASELINE_FILE" \
        "$TEST_REPORT_FILE" <<'PY'
import json
import sys
from pathlib import Path

project_dir = Path(sys.argv[1])
baseline_file = Path(sys.argv[2])
report_file = Path(sys.argv[3])

sys.path.insert(
    0,
    str(project_dir),
)

try:
    import agent
except Exception as exc:
    print(f"[FOUT]  agent.py importeren mislukt: {exc}")
    raise SystemExit(1)

required_functions = (
    "get_test_target_status",
    "check_test_target",
    "build_test_report",
)

missing_functions = [
    name
    for name in required_functions
    if not hasattr(agent, name)
]

if missing_functions:
    print(
        "[FOUT]  Automatische testfuncties ontbreken: "
        + ", ".join(missing_functions)
    )
    raise SystemExit(1)

try:
    status = agent.get_test_target_status()
except Exception as exc:
    print(f"[FOUT]  Teststatus opvragen mislukt: {exc}")
    raise SystemExit(1)

if not status.get("enabled", False):
    print("[FOUT]  Testbaseline is niet geldig of niet actief")
    print(
        f"        Reden             : "
        f"{status.get('reason') or 'onbekend'}"
    )
    raise SystemExit(1)

start_trades = int(
    status.get("start_trades", 0)
    or 0
)

target_total = int(
    status.get("target_total_trades", 0)
    or 0
)

current_trades = int(
    status.get("current_trades", 0)
    or 0
)

new_trades = int(
    status.get("new_trades", 0)
    or 0
)

remaining = int(
    status.get("remaining_trades", 0)
    or 0
)

target_new = max(
    0,
    target_total - start_trades,
)

dry_run = bool(
    status.get("dry_run", False)
)

target_reached = bool(
    status.get("target_reached", False)
)

teststop_active = (
    dry_run
    and target_total > start_trades
)

print("[OK]    Testbaseline actief")
print(f"        Bestand           : {baseline_file}")
print(f"        Start trades      : {start_trades}")
print(f"        Huidige trades    : {current_trades}")
print(
    f"        Nieuwe testtrades : "
    f"{new_trades}/{target_new}"
)
print(f"        Nog nodig         : {remaining}")
print(f"        Doel totaal       : {target_total}")
print(
    f"        Dry-run           : "
    f"{'JA' if dry_run else 'NEE'}"
)
print(
    f"        Teststop actief   : "
    f"{'JA' if teststop_active else 'NEE'}"
)
print(
    f"        Doel bereikt      : "
    f"{'JA' if target_reached else 'NEE'}"
)

if report_file.exists():
    try:
        with report_file.open(
            "r",
            encoding="utf-8",
        ) as file:
            report = json.load(file)

        print("[OK]    Testrapport aanwezig")
        print(f"        Bestand           : {report_file}")
        print(
            f"        Test compleet     : "
            f"{'JA' if report.get('test_complete') else 'NEE'}"
        )
        print(
            f"        Trades in rapport : "
            f"{report.get('included_new_trades', 0)}"
        )
        print(
            f"        Gegenereerd op    : "
            f"{report.get('generated_at') or '-'}"
        )

    except Exception as exc:
        print(
            f"[WAARSCHUWING] Testrapport lezen mislukt: {exc}"
        )
else:
    if target_reached:
        print(
            "[WAARSCHUWING] Doel bereikt, maar eindrapport "
            "nog niet aanwezig"
        )
        print(
            "        De agent maakt dit normaal binnen één minuut."
        )
    else:
        print(
            "[INFO]  Eindrapport wordt gemaakt zodra "
            f"trade {target_total} is bereikt"
        )

if not teststop_active:
    print(
        "[FOUT]  Automatische teststop is niet actief; "
        "dry-run moet aanstaan"
    )
    raise SystemExit(1)
PY
    then
        ERRORS=$((ERRORS + 1))
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
    requirements.txt \
    start.sh \
    healthcheck.sh
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

check_file "$STATE_FILE" "Bot-state" "true"
show_json_summary "$STATE_FILE" "bot"

echo
echo "4. VEILIGHEIDSCONTROLE"
echo "------------------------------------------------------------"

check_file "$CONTROL_FILE" "Controlebestand" "true"
show_json_summary "$CONTROL_FILE" "control"

echo
echo "5. AGENT"
echo "------------------------------------------------------------"

check_file "$AGENT_STATE_FILE" "Agent-state"
show_json_summary "$AGENT_STATE_FILE" "agent"

echo
echo "6. DIAGNOSE"
echo "------------------------------------------------------------"

check_file "$DIAG_STATS_FILE" "Diagnosestatistieken"
show_json_summary "$DIAG_STATS_FILE" "diagnose"

echo
echo "7. SUPERVISOR"
echo "------------------------------------------------------------"

check_file "$SUPERVISOR_FILE" "Supervisorrapport"
show_json_summary "$SUPERVISOR_FILE" "supervisor"

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

show_test_progress

echo
echo "10. SCHIJFRUIMTE"
echo "------------------------------------------------------------"

df -h "$DATA_DIR" 2>/dev/null || df -h

echo
echo "11. EINDCONTROLE"
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
