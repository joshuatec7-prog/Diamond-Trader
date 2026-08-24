#!/usr/bin/env python3
"""AUTO LIVE 5 preflight guard en exacte blokkadediagnostiek."""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

from diamond_auto_live_5_patch import (
    AUTO_STATE_FILE,
    read_json,
    write_json_atomic,
)
from diamond_liquidity_gate import evaluate_buy_liquidity

LOG = logging.getLogger("diamond_auto_live_5_guard")
HARD_AUTO_SPREAD_PCT = 0.10


def _enabled() -> bool:
    return str(os.getenv("DIAMOND_AUTO_LIVE_5_ENABLED", "")).strip().lower() in {
        "1", "true", "yes", "ja", "on", "aan"
    }


def _record_block(symbol: str, reason: str) -> None:
    """Bewaar de laatste concrete AUTO LIVE blokkadereden."""
    try:
        state = read_json(AUTO_STATE_FILE)
        state["last_block_reason"] = str(reason)
        state["last_block_symbol"] = str(symbol).upper()
        state["last_block_at"] = datetime.now(timezone.utc).isoformat()
        write_json_atomic(AUTO_STATE_FILE, state)
    except Exception as exc:
        LOG.warning(
            "AUTO LIVE 5 kon blokkadereden niet bewaren | %s",
            type(exc).__name__,
        )


def _clear_block() -> None:
    try:
        state = read_json(AUTO_STATE_FILE)
        state.pop("last_block_reason", None)
        state.pop("last_block_symbol", None)
        state.pop("last_block_at", None)
        write_json_atomic(AUTO_STATE_FILE, state)
    except Exception:
        pass


def _preflight(
    diamond_bot: Any,
    self: Any,
    symbol: str,
    signal: dict,
    precomputed_news_gate: Optional[dict],
    precomputed_ticker: Optional[dict],
    precomputed_spread_pct: Optional[float],
) -> Tuple[Optional[str], Optional[dict], Optional[float]]:
    """Spiegel de bestaande BUY-gates om de reden vóór ordervoorbereiding te kennen.

    De echte checks in diamond_bot.py blijven leidend en worden daarna opnieuw
    uitgevoerd. Deze preflight versoepelt dus niets.
    """
    if not self.spot_enabled():
        return "blocked_spot_disabled", None, None

    if self.entries_blocked_by_recovery():
        return "blocked_pending_or_recovery", None, None

    if symbol in (self.state.get("positions") or {}):
        return "blocked_position_exists", None, None

    if (
        not self.allow_long_and_short_same_symbol()
        and symbol in (self.state.get("short_positions") or {})
    ):
        return "blocked_short_position_exists", None, None

    if self.symbol_in_cooldown(symbol):
        return "blocked_symbol_cooldown", None, None

    max_open = int(
        diamond_bot.to_float(
            diamond_bot.get_cfg(self.cfg, "max_open_positions", 5),
            5,
        )
    )
    if self.open_positions_count() >= max_open:
        return "blocked_max_open_positions", None, None
    if self.total_positions_count() >= self.max_total_positions():
        return "blocked_max_total_positions", None, None

    if self.skip_symbol_due_to_existing_balance(symbol):
        return "blocked_existing_balance", None, None

    if not signal:
        return "blocked_missing_signal", None, None

    try:
        ticker = precomputed_ticker or self.get_ticker(symbol)
        spread = (
            float(precomputed_spread_pct)
            if precomputed_spread_pct is not None
            else float(self.estimate_spread_pct(ticker))
        )
    except Exception as exc:
        return (
            f"blocked_current_spread_check_error_{type(exc).__name__}",
            None,
            None,
        )

    if spread > HARD_AUTO_SPREAD_PCT:
        return (
            f"blocked_current_spread_{spread:.4f}_gt_{HARD_AUTO_SPREAD_PCT:.4f}",
            ticker,
            spread,
        )

    max_spread = diamond_bot.to_float(
        diamond_bot.get_cfg(self.cfg, "max_spread_pct", 0.25),
        0.25,
    )
    if spread > max_spread:
        return (
            f"blocked_config_spread_{spread:.4f}_gt_{max_spread:.4f}",
            ticker,
            spread,
        )

    news_gate = precomputed_news_gate or self.news.buy_gate(symbol)
    if not news_gate.get("allow", False):
        return (
            f"blocked_news_{news_gate.get('reason') or 'unknown'}",
            ticker,
            spread,
        )

    stake = min(
        diamond_bot.to_float(
            diamond_bot.get_cfg(self.cfg, "fixed_stake_quote", 40),
            40.0,
        ),
        self.buy_budget_available(),
    )
    if stake <= 0:
        return "blocked_no_buy_budget", ticker, spread

    gate = self.canary_new_entry_gate(stake)
    if not gate.get("allow", False):
        return (
            f"blocked_live_gate_{gate.get('reason') or 'unknown'}",
            ticker,
            spread,
        )

    liquidity_enabled = diamond_bot.to_bool(
        diamond_bot.get_cfg(
            self.cfg,
            "execution.liquidity_gate_enabled",
            True,
        ),
        True,
    )
    if liquidity_enabled:
        try:
            depth = max(
                5,
                min(
                    1000,
                    int(
                        diamond_bot.to_float(
                            diamond_bot.get_cfg(
                                self.cfg,
                                "execution.liquidity_orderbook_depth",
                                50,
                            ),
                            50,
                        )
                    ),
                ),
            )
            book = self.exchange.fetch_order_book(symbol, depth)
            liquidity = evaluate_buy_liquidity(
                book,
                stake,
                max_price_impact_pct=diamond_bot.to_float(
                    diamond_bot.get_cfg(
                        self.cfg,
                        "execution.liquidity_max_price_impact_pct",
                        0.15,
                    ),
                    0.15,
                ),
                depth_band_pct=diamond_bot.to_float(
                    diamond_bot.get_cfg(
                        self.cfg,
                        "execution.liquidity_depth_band_pct",
                        0.25,
                    ),
                    0.25,
                ),
                min_depth_multiple=diamond_bot.to_float(
                    diamond_bot.get_cfg(
                        self.cfg,
                        "execution.liquidity_min_depth_multiple",
                        2.0,
                    ),
                    2.0,
                ),
            )
        except Exception as exc:
            return (
                f"blocked_liquidity_check_error_{type(exc).__name__}",
                ticker,
                spread,
            )

        if not liquidity.get("allow", False):
            return (
                "blocked_liquidity_"
                f"{liquidity.get('reason') or 'unknown'}_"
                f"impact_{float(liquidity.get('estimated_price_impact_pct') or 0.0):.4f}_"
                f"depth_{float(liquidity.get('depth_multiple') or 0.0):.2f}",
                ticker,
                spread,
            )

    return None, ticker, spread


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
        is_auto = _enabled() and bool(signal.get("auto_live_5"))

        if not is_auto:
            return original_try_buy(
                self,
                symbol,
                precomputed_signal=precomputed_signal,
                precomputed_news_gate=precomputed_news_gate,
                precomputed_ticker=precomputed_ticker,
                precomputed_spread_pct=precomputed_spread_pct,
            )

        reason, ticker, spread = _preflight(
            diamond_bot,
            self,
            symbol,
            signal,
            precomputed_news_gate,
            precomputed_ticker,
            precomputed_spread_pct,
        )
        if reason:
            _record_block(symbol, reason)
            LOG.warning("AUTO LIVE 5 GEBLOKKEERD %s | %s", symbol, reason)
            return

        _clear_block()
        before_sequence = int(
            diamond_bot.to_float(
                (self.state or {}).get("canary_trade_sequence"),
                0,
            )
        )

        result = original_try_buy(
            self,
            symbol,
            precomputed_signal=precomputed_signal,
            precomputed_news_gate=precomputed_news_gate,
            precomputed_ticker=ticker,
            precomputed_spread_pct=spread,
        )

        after_sequence = int(
            diamond_bot.to_float(
                (self.state or {}).get("canary_trade_sequence"),
                0,
            )
        )
        if (
            after_sequence > before_sequence
            or symbol in (self.state.get("positions") or {})
        ):
            _clear_block()
            return result

        if self.entries_blocked_by_recovery():
            _record_block(symbol, "blocked_pending_or_recovery_after_attempt")
        else:
            _record_block(symbol, "blocked_inside_try_buy_after_preflight")
        return result

    Bot.try_buy_symbol = guarded_try_buy
    Bot._diamond_auto_live_5_spread_guard_installed = True


def self_test() -> None:
    assert HARD_AUTO_SPREAD_PCT == 0.10
    print("DIAMOND_AUTO_LIVE_5_GUARD_SELF_TEST_OK")


if __name__ == "__main__":
    self_test()
