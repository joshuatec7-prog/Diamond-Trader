#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "[START] CryptoBot Clean-Room - LEAN READ ONLY"
export UNIVERSE_SIZE=20
echo "[START] Universe size: ${UNIVERSE_SIZE}"
echo "[START] Direction scanner v3: strict L2 + net R/R, every 15 minutes (read only)"
echo "[START] Funding v4.1: strict 72h history + L2 costs; cross labels blocked (read only)"
exec python3 supervisor.py

