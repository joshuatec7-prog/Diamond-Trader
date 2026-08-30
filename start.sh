#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "[START] CryptoBot Clean-Room v1 - PAPER ONLY"
export UNIVERSE_SIZE=20
echo "[START] Universe size: ${UNIVERSE_SIZE}"
echo "[START] Strategy A: mean reversion"
echo "[START] Strategy B: trend momentum"
echo "[START] Strategy C: pullback continuation"
echo "[START] Strategy D: adaptive trend follower"
echo "[START] Research v4: weekly BTC/ETH shadow benchmark (read only)"
echo "[START] Funding v3.1: 72h gate + 30/90d stress history (read only)"
exec python3 supervisor.py

