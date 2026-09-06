#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "[START] CryptoBot Clean-Room v3.6 - PAPER / READ ONLY"
export UNIVERSE_SIZE=20
echo "[START] Universe size: ${UNIVERSE_SIZE}"
echo "[START] Scanner v3.5: v3.4 baseline + human 5m/L2/BTC challenger (paper only)"
echo "[START] Autonomous v3.6: full EUR universe + 8-part jury + automatic paper execution"
echo "[START] v3.6 capital: EUR 3000 | EUR 500 per position | max 5 | reserve EUR 200"
echo "[START] Existing coins excluded; live orders technically impossible"
echo "[START] Funding v4.1: strict 72h history + L2 costs; cross labels blocked (read only)"
exec python3 supervisor.py

