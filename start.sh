#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "[START] CryptoBot Clean-Room - LEAN READ ONLY"
export UNIVERSE_SIZE=20
echo "[START] Universe size: ${UNIVERSE_SIZE}"
echo "[START] Direction scanner v2: EUR + USDC, every 15 minutes (read only)"
echo "[START] Funding v4: executable L2 + 72h gate; cross labels blocked (read only)"
exec python3 supervisor.py

