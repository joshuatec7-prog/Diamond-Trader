#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "[START] CryptoBot Clean-Room v1 - PAPER ONLY"
exec python3 main.py
