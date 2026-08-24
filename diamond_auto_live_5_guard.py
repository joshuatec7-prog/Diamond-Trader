#!/usr/bin/env python3
"""AUTO LIVE 5 preflight guard, entry-herprijzing en blokkadediagnostiek."""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from diamond_auto_live_5_patch import (
    AUTO_STATE_FILE,
    read_json,
    write_json_atomic,
)
from diamond_liquidity_gate import evaluate_buy_liquidity
from diamond_selective_execution_adapter import mark_execution_contract_seen

LOG = logging.getLogger("diamond_auto_live_5_guard")
HARD_AUTO_SPREAD_PCT = 0.10

# Een oud trend-breakoutsignaal mag niet worden nagejaagd wanneer meer dan de
# helft van de oorspronkelijke beweging naar TP al voorbij is. Omgekeerd wordt
# ook niet ingestapt wanneer meer dan de helft van de oorspronkelijke
# stopafstand al neerwaarts is afgelegd.
HARD_MAX_PLAN_DRIFT_FRACTION = 0.50


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


def _build_live_entry_plan(
    signal: Dict[str, Any],
    current_ask: float,
    stake: float,
    fee_pct: float,
    min_profit_quote: float,
) -> Tuple[Optional[Dict[str, Any]], str, Dict[str, float]]:
    """Herprijs SL/TP rond de actuele instap, maar jaag een oud signaal niet na."""
    entry = float(signal.get("close") or 0.0)
    stop = float(signal.get("stop_loss") or 0.0)
    target = float(signal.get("take_profit") or 0.0)
    ask = float(current_ask or 0.0)

    metrics: Dict[str, float] = {
        "signal_entry": entry,
        "signal_stop": stop,
        "signal_target": target,
        "reference_ask": ask,
    }

    if not (stop > 0 and entry > 0 and target > 0 and stop < entry < target):
        return None, "blocked_entry_plan_invalid", metrics
    if ask <= 0:
        return None, "blocked_entry_ask_invalid", metrics

    reward_distance = target - entry
    risk_distance = entry - stop
    metrics["reward_distance"] = reward_distance
    metrics["risk_distance"] = risk_distance

    if ask >= target:
        return None, "blocked_entry_target_already_reached", metrics
    if ask <= stop:
        return None, "blocked_entry_original_stop_already_reached", metrics

    upward_drift = max(0.0, ask - entry)
    downward_drift = max(0.0, entry - ask)
    upward_fraction = upward_drift / reward_distance
    downward_fraction = downward_drift / risk_distance
    metrics["upward_drift_fraction"] = upward_fraction
    metrics["downward_drift_fraction"] = downward_fraction

    if upward_fraction > HARD_MAX_PLAN_DRIFT_FRACTION + 1e-12:
        return (
            None,
            f"blocked_entry_chase_{upward_fraction:.3f}_of_reward",
            metrics,
        )

    if downward_fraction > HARD_MAX_PLAN_DRIFT_FRACTION + 1e-12:
        return (
            None,
            f"blocked_entry_reversal_{downward_fraction:.3f}_of_risk",
            metrics,
        )

    # Het nieuwe plan bewaart exact dezelfde absolute risico- en
    # reward-afstand als het gevalideerde signaal, maar vanaf de actuele ask.
    rebased_stop = ask - risk_distance
    rebased_target = ask + reward_distance
    if rebased_stop <= 0 or rebased_stop >= ask or rebased_target <= ask:
        return None, "blocked_rebased_entry_plan_invalid", metrics

    fee_rate = max(0.0, float(fee_pct or 0.0)) / 100.0
    amount = max(0.0, float(stake or 0.0)) / ask
    entry_quote = max(0.0, float(stake or 0.0))
    entry_fee = entry_quote * fee_rate
    exit_quote = amount * rebased_target
    exit_fee = exit_quote * fee_rate
    expected_net = exit_quote - exit_fee - entry_quote - entry_fee
    metrics["expected_net_at_rebased_target"] = expected_net

    if expected_net + 1e-9 < max(0.0, float(min_profit_quote or 0.0)):
        return (
            None,
            f"blocked_rebased_tp_net_{expected_net:.3f}_lt_{min_profit_quote:.3f}",
            metrics,
        )

    adjusted = dict(signal)
    adjusted["original_signal_entry"] = entry
    adjusted["original_stop_loss"] = stop
    adjusted["original_take_profit"] = target
    adjusted["entry_plan_reference_ask"] = ask
    adjusted["entry_plan_reward_distance"] = reward_distance
    adjusted["entry_plan_risk_distance"] = risk_distance
    adjusted["entry_plan_upward_drift_fraction"] = upward_fraction
    adjusted["entry_plan_downward_drift_fraction"] = downward_fraction
    adjusted["entry_plan_expected_net_at_tp"] = expected_net
    adjusted["auto_live_repriced"] = True
    adjusted["close"] = ask
    adjusted["stop_loss"] = rebased_stop
    adjusted["take_profit"] = rebased_target

    metrics["rebased_stop"] = rebased_stop
    metrics["rebased_target"] = rebased_target
    return adjusted, "ok", metrics


def _finalize_position_plan(
    diamond_bot: Any,
    self: Any,
    symbol: str,
    execution_signal: Dict[str, Any],
    metrics: Dict[str, float],
) -> bool:
    """Leg SL/TP definitief rond de werkelijk bevestigde BUY-fill."""
    position = (self.state.get("positions") or {}).get(symbol)
    if not isinstance(position, dict):
        return False

    fill = diamond_bot.to_float(position.get("entry_price"), 0.0)
    reward_distance = diamond_bot.to_float(
        execution_signal.get("entry_plan_reward_distance"),
        0.0,
    )
    risk_distance = diamond_bot.to_float(
        execution_signal.get("entry_plan_risk_distance"),
        0.0,
    )
    if min(fill, reward_distance, risk_distance) <= 0:
        return False

    final_stop = fill - risk_distance
    final_target = fill + reward_distance
    if final_stop <= 0 or final_stop >= fill or final_target <= fill:
        return False

    position["stop_loss"] = final_stop
    position["take_profit"] = final_target
    position["highest_price"] = max(
        fill,
        diamond_bot.to_float(position.get("highest_price"), fill),
    )
    position["signal_entry_price"] = diamond_bot.to_float(
        execution_signal.get("original_signal_entry"),
        0.0,
    )
    position["entry_plan_reference_ask"] = diamond_bot.to_float(
        execution_signal.get("entry_plan_reference_ask"),
        fill,
    )
    position["entry_plan_reward_distance"] = reward_distance
    position["entry_plan_risk_distance"] = risk_distance
    position["entry_plan_upward_drift_fraction"] = diamond_bot.to_float(
        execution_signal.get("entry_plan_upward_drift_fraction"),
        0.0,
    )
    position["entry_plan_downward_drift_fraction"] = diamond_bot.to_float(
        execution_signal.get("entry_plan_downward_drift_fraction"),
        0.0,
    )
    position["entry_plan_rebased_to_fill"] = True
    diamond_bot.save_state(self.state_file, self.state)

    LOG.warning(
        "AUTO LIVE 5 EXITPLAN OP FILL | %s | signal=%.8f | ask=%.8f | "
        "fill=%.8f | stop=%.8f | tp=%.8f",
        symbol,
        metrics.get("signal_entry", 0.0),
        metrics.get("reference_ask", 0.0),
        fill,
        final_stop,
        final_target,
    )
    return True


def _preflight(
    diamond_bot: Any,
    self: Any,
    symbol: str,
    signal: dict,
    precomputed_news_gate: Optional[dict],
    precomputed_ticker: Optional[dict],
    precomputed_spread_pct: Optional[float],
) -> Tuple[
    Optional[str],
    Optional[dict],
    Optional[float],
    Dict[str, Any],
    Dict[str, float],
]:
    """Spiegel de bestaande BUY-gates en herprijs het entryplan vlak voor BUY.

    De echte checks in diamond_bot.py blijven leidend en worden daarna opnieuw
    uitgevoerd. Deze preflight versoepelt dus niets.
    """
    if not self.spot_enabled():
        return "blocked_spot_disabled", None, None, signal, {}

    if self.entries_blocked_by_recovery():
        return "blocked_pending_or_recovery", None, None, signal, {}

    if symbol in (self.state.get("positions") or {}):
        return "blocked_position_exists", None, None, signal, {}

    if (
        not self.allow_long_and_short_same_symbol()
        and symbol in (self.state.get("short_positions") or {})
    ):
        return "blocked_short_position_exists", None, None, signal, {}

    if self.symbol_in_cooldown(symbol):
        return "blocked_symbol_cooldown", None, None, signal, {}

    max_open = int(
        diamond_bot.to_float(
            diamond_bot.get_cfg(self.cfg, "max_open_positions", 5),
            5,
        )
    )
    if self.open_positions_count() >= max_open:
        return "blocked_max_open_positions", None, None, signal, {}
    if self.total_positions_count() >= self.max_total_positions():
        return "blocked_max_total_positions", None, None, signal, {}

    if self.skip_symbol_due_to_existing_balance(symbol):
        return "blocked_existing_balance", None, None, signal, {}

    if not signal:
        return "blocked_missing_signal", None, None, signal, {}

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
            signal,
            {},
        )

    if spread > HARD_AUTO_SPREAD_PCT:
        return (
            f"blocked_current_spread_{spread:.4f}_gt_{HARD_AUTO_SPREAD_PCT:.4f}",
            ticker,
            spread,
            signal,
            {},
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
            signal,
            {},
        )

    news_gate = precomputed_news_gate or self.news.buy_gate(symbol)
    if not news_gate.get("allow", False):
        return (
            f"blocked_news_{news_gate.get('reason') or 'unknown'}",
            ticker,
            spread,
            signal,
            {},
        )

    stake = min(
        diamond_bot.to_float(
            diamond_bot.get_cfg(self.cfg, "fixed_stake_quote", 40),
            40.0,
        ),
        self.buy_budget_available(),
    )
    if stake <= 0:
        return "blocked_no_buy_budget", ticker, spread, signal, {}

    ask = diamond_bot.to_float(ticker.get("ask"), 0.0)
    fee_pct = diamond_bot.to_float(
        diamond_bot.get_cfg(self.cfg, "taker_fee_pct", 0.25),
        0.25,
    )
    min_profit = max(
        0.0,
        diamond_bot.to_float(
            diamond_bot.get_cfg(self.cfg, "min_profit_eur", 0.50),
            0.50,
        ),
    )
    execution_signal, plan_reason, plan_metrics = _build_live_entry_plan(
        signal,
        ask,
        stake,
        fee_pct,
        min_profit,
    )
    if execution_signal is None:
        return plan_reason, ticker, spread, signal, plan_metrics

    gate = self.canary_new_entry_gate(stake)
    if not gate.get("allow", False):
        return (
            f"blocked_live_gate_{gate.get('reason') or 'unknown'}",
            ticker,
            spread,
            execution_signal,
            plan_metrics,
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
                execution_signal,
                plan_metrics,
            )

        if not liquidity.get("allow", False):
            return (
                "blocked_liquidity_"
                f"{liquidity.get('reason') or 'unknown'}_"
                f"impact_{float(liquidity.get('estimated_price_impact_pct') or 0.0):.4f}_"
                f"depth_{float(liquidity.get('depth_multiple') or 0.0):.2f}",
                ticker,
                spread,
                execution_signal,
                plan_metrics,
            )

    return None, ticker, spread, execution_signal, plan_metrics


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

        reason, ticker, spread, execution_signal, plan_metrics = _preflight(
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

        LOG.warning(
            "AUTO LIVE 5 ENTRYPLAN | %s | signal=%.8f | ask=%.8f | "
            "stop=%.8f | tp=%.8f | drift_up=%.1f%% reward | "
            "verwacht_net_tp=€%.2f",
            symbol,
            plan_metrics.get("signal_entry", 0.0),
            plan_metrics.get("reference_ask", 0.0),
            plan_metrics.get("rebased_stop", 0.0),
            plan_metrics.get("rebased_target", 0.0),
            plan_metrics.get("upward_drift_fraction", 0.0) * 100.0,
            plan_metrics.get("expected_net_at_rebased_target", 0.0),
        )

        _clear_block()

        result = original_try_buy(
            self,
            symbol,
            precomputed_signal=execution_signal,
            precomputed_news_gate=precomputed_news_gate,
            precomputed_ticker=ticker,
            precomputed_spread_pct=spread,
        )

        position = (self.state.get("positions") or {}).get(symbol)
        if isinstance(position, dict):
            _finalize_position_plan(
                diamond_bot,
                self,
                symbol,
                execution_signal,
                plan_metrics,
            )
            candidate_key = str(signal.get("candidate_key") or "").strip()
            if candidate_key:
                try:
                    mark_execution_contract_seen(
                        candidate_key,
                        reason="confirmed_live_buy",
                    )
                except Exception as exc:
                    LOG.warning(
                        "AUTO LIVE 5 kon uitgevoerde kandidaat niet als seen markeren | %s",
                        type(exc).__name__,
                    )
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
    assert HARD_MAX_PLAN_DRIFT_FRACTION == 0.50

    good = {
        "close": 100.0,
        "stop_loss": 98.0,
        "take_profit": 104.0,
    }
    adjusted, reason, metrics = _build_live_entry_plan(
        good,
        current_ask=100.5,
        stake=130.0,
        fee_pct=0.25,
        min_profit_quote=0.50,
    )
    assert reason == "ok"
    assert adjusted is not None
    assert abs(adjusted["stop_loss"] - 98.5) < 1e-9
    assert abs(adjusted["take_profit"] - 104.5) < 1e-9
    assert metrics["upward_drift_fraction"] == 0.125

    # Regressie van VIRTUAL 24-08: de late €0,70349 entry had nog maar
    # ongeveer 14% van de oorspronkelijke beweging naar TP over en moet dus
    # vóór ordervoorbereiding worden geblokkeerd.
    virtual = {
        "close": 0.68084,
        "stop_loss": 0.6666692414103276,
        "take_profit": 0.7072099769442902,
    }
    adjusted, reason, metrics = _build_live_entry_plan(
        virtual,
        current_ask=0.70349,
        stake=130.0,
        fee_pct=0.25,
        min_profit_quote=0.50,
    )
    assert adjusted is None
    assert reason.startswith("blocked_entry_chase_")
    assert metrics["upward_drift_fraction"] > 0.80

    reversal = {
        "close": 100.0,
        "stop_loss": 98.0,
        "take_profit": 104.0,
    }
    adjusted, reason, _ = _build_live_entry_plan(
        reversal,
        current_ask=98.8,
        stake=130.0,
        fee_pct=0.25,
        min_profit_quote=0.50,
    )
    assert adjusted is None
    assert reason.startswith("blocked_entry_reversal_")

    print("DIAMOND_AUTO_LIVE_5_GUARD_SELF_TEST_OK")


if __name__ == "__main__":
    self_test()
