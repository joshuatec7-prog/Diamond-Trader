#!/usr/bin/env python3
"""Extra current-spread guard for AUTO LIVE 5."""

import logging
import os
from typing import Any

LOG = logging.getLogger("diamond_auto_live_5_guard")
HARD_AUTO_SPREAD_PCT = 0.10


def _enabled() -> bool:
    return str(os.getenv("DIAMOND_AUTO_LIVE_5_ENABLED", "")).strip().lower() in {
        "1", "true", "yes", "ja", "on", "aan"
    }


def install_auto_live_5_guard() -> None:
    import diamond_bot

    Bot = diamond_bot.Bot
    if getattr(Bot, "_diamond_auto_live_5_spread_guard_installed", False):
        return

    original_try_buy = Bot.try_buy_symbol

    def guarded_try_buy(
        self: Any,
        symbol: str,
        precomputed_signal=None,
        precomputed_news_gate=None,
        precomputed_ticker=None,
        precomputed_spread_pct=None,
    ) -> None:
        signal = precomputed_signal or {}
        if _enabled() and bool(signal.get("auto_live_5")):
            try:
                ticker = precomputed_ticker or self.get_ticker(symbol)
                spread = (
                    float(precomputed_spread_pct)
                    if precomputed_spread_pct is not None
                    else float(self.estimate_spread_pct(ticker))
                )
            except Exception as exc:
                LOG.warning(
                    "AUTO LIVE 5 GEBLOKKEERD %s | actuele spreadcontrole fout: %s",
                    symbol,
                    type(exc).__name__,
                )
                return

            if spread > HARD_AUTO_SPREAD_PCT:
                LOG.warning(
                    "AUTO LIVE 5 GEBLOKKEERD %s | actuele spread %.4f%% > %.4f%%",
                    symbol,
                    spread,
                    HARD_AUTO_SPREAD_PCT,
                )
                return

            precomputed_ticker = ticker
            precomputed_spread_pct = spread

        return original_try_buy(
            self,
            symbol,
            precomputed_signal=precomputed_signal,
            precomputed_news_gate=precomputed_news_gate,
            precomputed_ticker=precomputed_ticker,
            precomputed_spread_pct=precomputed_spread_pct,
        )

    Bot.try_buy_symbol = guarded_try_buy
    Bot._diamond_auto_live_5_spread_guard_installed = True


def self_test() -> None:
    assert HARD_AUTO_SPREAD_PCT == 0.10
    print("DIAMOND_AUTO_LIVE_5_GUARD_SELF_TEST_OK")


if __name__ == "__main__":
    self_test()
