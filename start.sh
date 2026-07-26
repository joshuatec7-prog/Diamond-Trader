#!/usr/bin/env bash

# Diamond Trader startscript
# Start en bewaak alle hoofdprocessen, inclusief de veilige TA-schaduwscanner.

set -Eeuo pipefail

PROJECT_DIR="/opt/render/project/src"
DATA_DIR="/var/data"
SCANNER_LOG="$DATA_DIR/diamond_market_scanner_runner.log"

cd "$PROJECT_DIR"
mkdir -p "$DATA_DIR"

PIDS=()

start_process() {
    local display_name="$1"
    shift

    echo "[START] $display_name"
    "$@" &
    local pid=$!

    PIDS+=("$pid")
    echo "        PID $pid"
}

cleanup() {
    local exit_code=$?

    trap - EXIT INT TERM

    echo
    echo "[STOP] Diamond Trader-processen afsluiten"

    for pid in "${PIDS[@]:-}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
        fi
    done

    # Geef processen kort de tijd om netjes af te sluiten.
    sleep 2

    for pid in "${PIDS[@]:-}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
        fi
    done

    wait 2>/dev/null || true
    exit "$exit_code"
}

trap cleanup EXIT INT TERM

echo "============================================================"
echo " DIAMOND TRADER START"
echo " $(date)"
echo "============================================================"

start_process     "Diamond Agent"     python3 agent.py

start_process     "Diamond Diagnose"     python3 closed_candle_runner.py diagnose

start_process     "Diamond Supervisor"     python3 supervisor_agent.py

start_process     "Diamond Bot"     python3 closed_candle_runner.py bot

echo "[START] Diamond Market Scanner"
python3 market_scanner.py --loop --top 20     >> "$SCANNER_LOG" 2>&1 &
SCANNER_PID=$!
PIDS+=("$SCANNER_PID")
echo "        PID $SCANNER_PID"
echo "        Log: $SCANNER_LOG"

echo
echo "[OK] Alle Diamond Trader-processen zijn gestart."
echo "     Het startscript stopt de worker als één proces onverwacht stopt."
echo

# Render hoort de worker opnieuw te starten als een belangrijk proces uitvalt.
# wait -n wacht tot het eerste achtergrondproces stopt.
set +e
wait -n "${PIDS[@]}"
WAIT_STATUS=$?
set -e

echo
echo "[FOUT] Een Diamond Trader-proces is gestopt."
echo "       Exitcode: $WAIT_STATUS"

exit "$WAIT_STATUS"
