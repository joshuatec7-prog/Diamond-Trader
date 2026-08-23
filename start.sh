#!/usr/bin/env bash

# Diamond Trader startscript v2.8
# - Repo-versie van `chat` wordt na iedere deploy automatisch actief.
# - LIVE-kandidaatmailer draait adviserend en plaatst nooit orders.
# - LONG momentum prospective tracker draait research-only.
# - SHORT momentum prospective tracker draait research-only.
# - Geen strategie-, stake- of livewijzigingen.

set -Eeuo pipefail

PROJECT_DIR="/opt/render/project/src"
DATA_DIR="/var/data"
PERIODIC_LOG="$DATA_DIR/diamond_periodic_analysis_runner.log"
STRATEGY_LAB_LOG="$DATA_DIR/diamond_strategy_lab_runner.log"
EARLY_ENTRY_LOG="$DATA_DIR/diamond_early_entry/collector_v1_3_1_runner.log"
LIVE_CANDIDATE_LOG="$DATA_DIR/diamond_live_candidate_mailer.log"
LONG_MOMENTUM_LOG="$DATA_DIR/diamond_long_momentum_prospective.log"
SHORT_MOMENTUM_LOG="$DATA_DIR/diamond_short_momentum_prospective.log"

cd "$PROJECT_DIR"
mkdir -p "$DATA_DIR" "$DATA_DIR/diamond_early_entry"

setup_shell_helpers() {
    if [ -f "$PROJECT_DIR/chat" ]; then
        chmod +x "$PROJECT_DIR/chat"
        ln -sf "$PROJECT_DIR/chat" "$DATA_DIR/chat"
    elif [ -f "$DATA_DIR/chat" ]; then
        chmod +x "$DATA_DIR/chat" 2>/dev/null || true
    else
        echo "[INFO] Geen chat-helper gevonden."
        return
    fi

    mkdir -p "$HOME/bin"

    cat > "$HOME/bin/chat" <<'SH'
#!/usr/bin/env bash
set -u

CORE="/var/data/chat"

if [ ! -x "$CORE" ]; then
    echo "FOUT: $CORE ontbreekt of is niet uitvoerbaar."
    exit 1
fi

if grep -q 'PLAK VANAF HIER' "$CORE" 2>/dev/null; then
    exec "$CORE" "$@"
fi

printf '\033[1;31m============================================================\033[0m\n'
printf '\033[1;31mPLAK VANAF HIER\033[0m\n'
printf '\033[1;31m============================================================\033[0m\n'

"$CORE" "$@"
status=$?

printf '\033[1;31m============================================================\033[0m\n'
printf '\033[1;31mEINDE - TOT HIER PLAKKEN\033[0m\n'
printf '\033[1;31m============================================================\033[0m\n'

exit "$status"
SH

    chmod +x "$HOME/bin/chat"

    local path_line='export PATH="$HOME/bin:$PATH"'
    for profile in "$HOME/.bashrc" "$HOME/.profile"; do
        touch "$profile"
        grep -qxF "$path_line" "$profile" 2>/dev/null || \
            echo "$path_line" >> "$profile"
    done

    export PATH="$HOME/bin:$PATH"
    hash -r 2>/dev/null || true
    echo "[OK] Render Shell commando beschikbaar: chat"
}

setup_shell_helpers

PIDS=()
OPTIONAL_PIDS=()

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

    for pid in "${PIDS[@]:-}" "${OPTIONAL_PIDS[@]:-}"; do
        [ -n "$pid" ] || continue
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
        fi
    done

    sleep 2

    for pid in "${PIDS[@]:-}" "${OPTIONAL_PIDS[@]:-}"; do
        [ -n "$pid" ] || continue
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

start_process "Diamond Agent" python3 agent.py
start_process "Diamond Supervisor" python3 supervisor_agent.py
start_process "Diamond Bot" python3 closed_candle_runner.py bot

echo "[START] Diamond LIVE Candidate Mailer"
python3 diamond_live_candidate_mailer.py \
    >> "$LIVE_CANDIDATE_LOG" 2>&1 &
LIVE_CANDIDATE_PID=$!
OPTIONAL_PIDS+=("$LIVE_CANDIDATE_PID")
echo "        PID $LIVE_CANDIDATE_PID"

echo "[START] Diamond LONG Momentum Prospective Tracker"
python3 diamond_long_momentum_prospective_tracker.py \
    --loop --interval-seconds 900 --no-print \
    >> "$LONG_MOMENTUM_LOG" 2>&1 &
LONG_MOMENTUM_PID=$!
OPTIONAL_PIDS+=("$LONG_MOMENTUM_PID")
echo "        PID $LONG_MOMENTUM_PID"

echo "[START] Diamond SHORT Momentum Prospective Tracker"
python3 diamond_short_momentum_prospective_tracker.py \
    --loop --interval-seconds 900 --no-print \
    >> "$SHORT_MOMENTUM_LOG" 2>&1 &
SHORT_MOMENTUM_PID=$!
OPTIONAL_PIDS+=("$SHORT_MOMENTUM_PID")
echo "        PID $SHORT_MOMENTUM_PID"

echo "[START] Diamond Strategy Lab"
python3 strategy_lab.py --loop --interval-minutes 360 --no-print \
    >> "$STRATEGY_LAB_LOG" 2>&1 &
STRATEGY_LAB_PID=$!
PIDS+=("$STRATEGY_LAB_PID")
echo "        PID $STRATEGY_LAB_PID"

echo "[START] Diamond Periodieke Analyse"
python3 periodic_analysis_runner.py \
    >> "$PERIODIC_LOG" 2>&1 &
PERIODIC_PID=$!
PIDS+=("$PERIODIC_PID")
echo "        PID $PERIODIC_PID"

echo "[START] Diamond Early Entry Collector"
python3 early_entry_collector_runner_v1_1.py \
    >> "$EARLY_ENTRY_LOG" 2>&1 &
EARLY_ENTRY_RUNNER_PID=$!
PIDS+=("$EARLY_ENTRY_RUNNER_PID")
echo "        Runner PID $EARLY_ENTRY_RUNNER_PID"

set +e

BTC_COLLECTOR="$PROJECT_DIR/btc_event_confirmation_collector.py"
BTC_STATE="$DATA_DIR/diamond_btc_event_confirmation/collector_state.json"

if [ -f "$BTC_COLLECTOR" ] && [ -f "$BTC_STATE" ]; then
    BTC_STATUS="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("status",""))' "$BTC_STATE" 2>/dev/null || true)"
    if [ "$BTC_STATUS" = "RUNNING" ] && ! pgrep -f 'btc_event_confirmation_collector\.py' >/dev/null 2>&1; then
        nohup python3 "$BTC_COLLECTOR" >> "$DATA_DIR/btc_event_confirmation.log" 2>&1 </dev/null &
    fi
fi

wait -n "${PIDS[@]}"
WAIT_STATUS=$?
set -e

echo
echo "[FOUT] Een Diamond Trader-hoofdproces is gestopt."
echo "       Exitcode: $WAIT_STATUS"

exit "$WAIT_STATUS"
