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
exec python3 supervisor.py
