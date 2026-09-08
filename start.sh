#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "[START] CryptoBot Clean-Room v3.6 + v3.7 + v3.8 discovery - PAPER / READ ONLY"
export UNIVERSE_SIZE=20
echo "[START] Universe size: ${UNIVERSE_SIZE}"
echo "[START] Scanner v3.5: v3.4 baseline + human 5m/L2/BTC challenger (paper only)"
echo "[START] Autonomous v3.6: full EUR universe + 8-part jury + automatic paper execution"
echo "[START] v3.6 capital: EUR 3000 | EUR 500 per position | max 5 | reserve EUR 200"
echo "[START] v3.7 phase 1: separate database + hard gates + multi-snapshot L2 | observe only"
echo "[START] v3.7 execution: OFF | at least 24h observation | no automatic activation"
echo "[START] v3.8: all active EUR markets | human discovery funnel | observe only"
echo "[START] Existing coins excluded; live orders technically impossible"
echo "[START] Funding v4.1: strict 72h history + L2 costs; cross labels blocked (read only)"
exec python3 supervisor.py

