#!/usr/bin/env bash
set -Eeuo pipefail

PIDS=()

cleanup() {
    exit_code=$?

    trap - EXIT INT TERM

    echo "Diamond Trader stopt alle processen..."

    if [ "${#PIDS[@]}" -gt 0 ]; then
        kill -TERM "${PIDS[@]}" 2>/dev/null || true
        wait "${PIDS[@]}" 2>/dev/null || true
    fi

    exit "$exit_code"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

python3 agent.py &
PIDS+=("$!")

python3 diagnose.py &
PIDS+=("$!")

python3 supervisor_agent.py &
PIDS+=("$!")

python3 diamond_bot.py &
PIDS+=("$!")

echo "Diamond Trader gestart | processen: ${PIDS[*]}"

# Zodra één proces stopt, stopt de volledige service.
# Render kan daarna alles schoon opnieuw starten.
set +e
wait -n "${PIDS[@]}"
STATUS=$?
set -e

echo "FOUT: een Diamond Trader-proces is gestopt | status=$STATUS"
exit 1
