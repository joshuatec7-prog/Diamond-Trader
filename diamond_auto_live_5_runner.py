#!/usr/bin/env python3
"""Production launcher for the bounded AUTO LIVE 5 experiment."""

import os

# Explicitly enable only this bounded runner. After five confirmed automatic
# BUYs the persistent AUTO LIVE state hard-stops further automatic entries.
os.environ["DIAMOND_AUTO_LIVE_5_ENABLED"] = "1"

from diamond_auto_live_5_patch import install_auto_live_5_patch

install_auto_live_5_patch()

from closed_candle_runner import run_bot


if __name__ == "__main__":
    run_bot()
