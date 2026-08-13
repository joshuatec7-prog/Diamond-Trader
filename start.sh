#!/usr/bin/env bash

# Diamond Trader startscript v2.3
# Geheugenarme opzet:
# - Agent, Supervisor, Bot en Strategy Lab blijven permanent draaien.
# - Diagnose en Market Scanner worden sequentieel iedere 15 minuten uitgevoerd.
# - Diagnose en Scanner draaien nooit tegelijk.
# - Early Entry Collector v1.3.1 verzamelt alleen publieke marktdata.
# - De aparte Early Entry Runner v1.1 herstart alleen de collector als die stopt.
# - Herstelt na iedere deploy automatisch het korte Render Shell-commando: chat.
# - Ververst de compacte fasehelper voor PAPER / CANARY / LIVE status.

set -Eeuo pipefail

PROJECT_DIR="/opt/render/project/src"
DATA_DIR="/var/data"
PERIODIC_LOG="$DATA_DIR/diamond_periodic_analysis_runner.log"
STRATEGY_LAB_LOG="$DATA_DIR/diamond_strategy_lab_runner.log"
EARLY_ENTRY_LOG="$DATA_DIR/diamond_early_entry/collector_v1_3_1_runner.log"

cd "$PROJECT_DIR"
mkdir -p "$DATA_DIR"
mkdir -p "$DATA_DIR/diamond_early_entry"

setup_shell_helpers() {
    if [ -f "$DATA_DIR/chat" ]; then
        chmod +x "$DATA_DIR/chat" 2>/dev/null || true

        mkdir -p "$HOME/bin"
        ln -sf "$DATA_DIR/chat" "$HOME/bin/chat"

        local path_line='export PATH="$HOME/bin:$PATH"'

        for profile in "$HOME/.bashrc" "$HOME/.profile"; do
            touch "$profile"
            grep -qxF "$path_line" "$profile" 2>/dev/null || \
                echo "$path_line" >> "$profile"
        done

        export PATH="$HOME/bin:$PATH"
        echo "[OK] Render Shell helper beschikbaar: chat"
    else
        echo "[INFO] /var/data/chat ontbreekt; shell-helper niet aangemaakt."
    fi

    cat > "$DATA_DIR/phase_readiness.py" <<'PY'
#!/usr/bin/env python3
import json
import subprocess
from pathlib import Path

DATA = Path("/var/data")
ROOT = Path("/opt/render/project/src")
status_file = DATA / "diamond_release_phase_status.json"

if not status_file.exists():
    subprocess.run(
        ["python3", "diamond_release_go_live_readiness.py"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )

try:
    data = json.loads(status_file.read_text(encoding="utf-8"))
except Exception:
    data = {}

print("=== FASE GEREEDHEID ===")
print("PAPER READY  :", "JA" if data.get("paper_ready") else "NEE")
print("CANARY READY :", "JA" if data.get("canary_ready") else "NEE")
print("LIVE ACTIVE  :", "JA" if data.get("live_active") else "NEE")
print(
    "Execution    :",
    f"{int(data.get('execution_closed', 0))}/20",
    f"| {data.get('execution_status', 'WAIT')}",
)
print(
    "Safety       :",
    f"{int(data.get('safety_passed', 0))}/"
    f"{int(data.get('safety_total', 7))}",
)
PY

    chmod +x "$DATA_DIR/phase_readiness.py"
}

setup_shell_helpers

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

echo "[START] Diamond Early Entry Collector"
python3 early_entry_collector_runner_v1_1.py \
    >> "$EARLY_ENTRY_LOG" 2>&1 &
EARLY_ENTRY_RUNNER_PID=$!
PIDS+=("$EARLY_ENTRY_RUNNER_PID")
echo "        Runner PID $EARLY_ENTRY_RUNNER_PID"
echo "        Runner: early_entry_collector_runner_v1_1.py"
echo "        Collector: early_entry_collector_v1_3_1.py"
echo "        Transport: publieke native REST"
echo "        Log: $EARLY_ENTRY_LOG"

echo
echo "[OK] Alle Diamond Trader-hoofdprocessen zijn gestart."
echo "     Diagnose en Market Scanner draaien geheugenarm en periodiek."
echo "     Het startscript stopt de worker als een hoofdproces onverwacht stopt."
echo

set +e

BTC_COLLECTOR="/opt/render/project/src/btc_event_confirmation_collector.py"
BTC_STATE="/var/data/diamond_btc_event_confirmation/collector_state.json"

if [ -f "$BTC_COLLECTOR" ] && [ -f "$BTC_STATE" ]; then
    BTC_STATUS="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("status",""))' "$BTC_STATE" 2>/dev/null || true)"

    if [ "$BTC_STATUS" = "RUNNING" ] && ! pgrep -f 'btc_event_confirmation_collector\.py' >/dev/null 2>&1; then
        nohup python3 "$BTC_COLLECTOR" >> /var/data/btc_event_confirmation.log 2>&1 </dev/null &
    fi
fi

wait -n "${PIDS[@]}"
WAIT_STATUS=$?
set -e

echo
echo "[FOUT] Een Diamond Trader-hoofdproces is gestopt."
echo "       Exitcode: $WAIT_STATUS"

exit "$WAIT_STATUS"
