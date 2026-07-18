#!/bin/bash

# Diamond Bot Healthcheck
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

NOW_EPOCH=$(date +%s)

echo
echo "============================================================"
echo " DIAMOND BOT CONTROLE"
echo " $(date)"
echo "============================================================"
echo

check_process() {
    local script_name="$1"
    local display_name="$2"

    if pgrep -af "python3.*${script_name}" >/dev/null 2>&1; then
        echo "[OK]    $display_name draait"
        pgrep -af "python3.*${script_name}" | sed 's/^/        /'
    else
        echo "[FOUT]  $display_name draait NIET"
    fi
}

check_file() {
    local file_path="$1"
    local display_name="$2"

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
        echo "[INFO]  $display_name nog niet aanwezig"
        echo "        Bestand: $file_path"
    fi
}

show_json_summary() {
    local file_path="$1"
    local summary_type="$2"

    if [ ! -f "$file_path" ]; then
        return
    fi

    python3 - "$file_path" "$summary_type" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
summary_type = sys.argv[2]

try:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
except Exception as exc:
    print(f"        [FOUT] JSON lezen mislukt: {exc}")
    raise SystemExit(0)

if summary_type == "bot":
    positions = data.get("positions") or {}
    shorts = data.get("short_positions") or {}

    print(f"        Open spotposities : {len(positions)}")
    print(f"        Open shorts       : {len(shorts)}")
    print(f"        Spot trades       : {data.get('trades', 0)}")
    print(f"        Spot winsttrades  : {data.get('wins', 0)}")
    print(f"        Spot PnL          : {float(data.get('pnl_quote', 0) or 0):+.2f} EUR")
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
                f"(€{quote_amount:.2f}) | "
                f"door bot={opened_by_bot}"
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

        print(
            f"          - {symbol}: controles={checks}, "
            f"bijna={near}, signalen={signals}, "
            f"laatste score={float(stats.get('last_score_pct', 0) or 0):.0f}%"
        )

elif summary_type == "supervisor":
    print(f"        Gegenereerd op    : {data.get('generated_at') or '-'}")
    print(f"        Modus             : {data.get('mode') or '-'}")
    print(f"        Diagnoserondes    : {data.get('total_diagnose_rounds', 0)}")
    print(f"        Open posities     : {data.get('open_positions', 0)}")
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
    print(f"        Laatste analyse   : {data.get('last_analysis_ts', 0)}")

    sent_reports = data.get("sent_reports") or data.get("sent_daily_reports") or []
    weekly = data.get("sent_weekly_reports") or []

    print(f"        Statusmails       : {len(sent_reports)}")
    print(f"        Weekrapporten     : {len(weekly)}")
PY
}

echo "1. PROCESSEN"
echo "------------------------------------------------------------"

check_process "agent.py" "Diamond Agent"
check_process "diagnose.py" "Diamond Diagnose"
check_process "supervisor_agent.py" "Diamond Supervisor"
check_process "diamond_bot.py" "Diamond Bot"

echo
echo "2. PROJECTBESTANDEN"
echo "------------------------------------------------------------"

for file_name in \
    agent.py \
    config.yaml \
    diagnose.py \
    supervisor_agent.py \
    diamond_bot.py \
    requirements.txt \
    start.sh
do
    if [ -f "$PROJECT_DIR/$file_name" ]; then
        echo "[OK]    $file_name"
    else
        echo "[FOUT]  $file_name ontbreekt"
    fi
done

echo
echo "3. BOT-STATE"
echo "------------------------------------------------------------"

check_file "$STATE_FILE" "Bot-state"
show_json_summary "$STATE_FILE" "bot"

echo
echo "4. VEILIGHEIDSCONTROLE"
echo "------------------------------------------------------------"

check_file "$CONTROL_FILE" "Controlebestand"
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
    echo "        Dit is normaal zolang er nog geen dry-run trade is geweest."
fi

echo
echo "9. SCHIJFRUIMTE"
echo "------------------------------------------------------------"

df -h "$DATA_DIR" 2>/dev/null || df -h

echo
echo "10. EINDCONTROLE"
echo "------------------------------------------------------------"

errors=0

for process_script in \
    agent.py \
    diagnose.py \
    supervisor_agent.py \
    diamond_bot.py
do
    if ! pgrep -af "python3.*${process_script}" >/dev/null 2>&1; then
        errors=$((errors + 1))
    fi
done

if [ ! -f "$CONTROL_FILE" ]; then
    errors=$((errors + 1))
fi

if [ ! -f "$STATE_FILE" ]; then
    errors=$((errors + 1))
fi

if [ "$errors" -eq 0 ]; then
    echo "[OK]    Alle belangrijke onderdelen lijken actief."
else
    echo "[FOUT]  Er zijn $errors belangrijke problemen gevonden."
fi

echo
echo "============================================================"
echo " CONTROLE AFGEROND"
echo "============================================================"
echo
