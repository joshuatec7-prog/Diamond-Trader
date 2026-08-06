#!/usr/bin/env bash

# Diamond Trader startscript v2.2
# Geheugenarme opzet:
# - Agent, Supervisor, Bot en Strategy Lab blijven permanent draaien.
# - Diagnose en Market Scanner worden sequentieel iedere 15 minuten uitgevoerd.
# - Diagnose en Scanner draaien nooit tegelijk.
# - Early Entry Collector v1.2 verzamelt alleen publieke marktdata.
# - De aparte Early Entry Runner herstart alleen de collector als die stopt.

set -Eeuo pipefail

PROJECT_DIR="/opt/render/project/src"
DATA_DIR="/var/data"
PERIODIC_LOG="$DATA_DIR/diamond_periodic_analysis_runner.log"
STRATEGY_LAB_LOG="$DATA_DIR/diamond_strategy_lab_runner.log"
EARLY_ENTRY_LOG="$DATA_DIR/diamond_early_entry/collector_v1_2_runner.log"

cd "$PROJECT_DIR"
mkdir -p "$DATA_DIR"
mkdir -p "$DATA_DIR/diamond_early_entry"

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

start_process \
    "Diamond Agent" \
    python3 agent.py

start_process \
    "Diamond Supervisor" \
    python3 supervisor_agent.py

start_process \
    "Diamond Bot" \
    python3 closed_candle_runner.py bot

echo "[START] Diamond Strategy Lab"
python3 strategy_lab.py --loop --interval-minutes 360 --no-print \
    >> "$STRATEGY_LAB_LOG" 2>&1 &
STRATEGY_LAB_PID=$!
PIDS+=("$STRATEGY_LAB_PID")
echo "        PID $STRATEGY_LAB_PID"
echo "        Interval: iedere 360 minuten"
echo "        Log: $STRATEGY_LAB_LOG"

echo "[START] Diamond Periodieke Analyse"
python3 periodic_analysis_runner.py \
    >> "$PERIODIC_LOG" 2>&1 &
PERIODIC_PID=$!
PIDS+=("$PERIODIC_PID")
echo "        PID $PERIODIC_PID"
echo "        Diagnose + Scanner: sequentieel iedere 15 minuten"
echo "        Log: $PERIODIC_LOG"

# EARLY_ENTRY_AUTOSTART_V1
# De runner blijft permanent actief en bewaakt alleen de collector.
# Daardoor staat de collector zelf NIET rechtstreeks in PIDS/wait -n.
echo "[START] Diamond Early Entry Collector"
python3 early_entry_collector_runner.py \
    >> "$EARLY_ENTRY_LOG" 2>&1 &
EARLY_ENTRY_RUNNER_PID=$!
PIDS+=("$EARLY_ENTRY_RUNNER_PID")
echo "        Runner PID $EARLY_ENTRY_RUNNER_PID"
echo "        Collector: early_entry_collector_v1_2.py"
echo "        Transport: publieke REST-only"
echo "        Log: $EARLY_ENTRY_LOG"

echo
echo "[OK] Alle Diamond Trader-hoofdprocessen zijn gestart."
echo "     Diagnose en Market Scanner draaien geheugenarm en periodiek."
echo "     Het startscript stopt de worker als een hoofdproces onverwacht stopt."
echo

# Render hoort de worker opnieuw te starten als een belangrijk proces uitvalt.
# wait -n wacht tot het eerste permanente achtergrondproces stopt.
set +e
wait -n "${PIDS[@]}"
WAIT_STATUS=$?
set -e

echo
echo "[FOUT] Een Diamond Trader-hoofdproces is gestopt."
echo "       Exitcode: $WAIT_STATUS"

exit "$WAIT_STATUS"
