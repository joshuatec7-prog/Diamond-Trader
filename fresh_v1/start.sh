#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "[START] CryptoBot Fresh v1 - PAPER ONLY"
exec python3 main.py --loop
