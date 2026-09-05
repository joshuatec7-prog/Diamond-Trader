#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "[START] CryptoBot Clean-Room - LEAN READ ONLY"
export UNIVERSE_SIZE=20
echo "[START] Universe size: ${UNIVERSE_SIZE}"
echo "[START] Scanner v3.5: v3.4 baseline + human 5m/L2/BTC challenger (paper only)"
echo "[START] Funding v4.1: strict 72h history + L2 costs; cross labels blocked (read only)"
exec python3 supervisor.py

