#!/usr/bin/env python3
"""Pushover notifications for Diamond Trader AUTO LIVE 5.

The module never creates or approves orders. It only observes already-confirmed
LIVE open/close events and sends notifications through Pushover.

Secrets are read exclusively from environment variables:
- PUSHOVER_USER_KEY
- PUSHOVER_API_TOKEN

AUTO BUY notifications use Pushover emergency priority and repeat every
60 seconds for up to 20 minutes until acknowledged. SELL notifications are
high priority but do not repeat.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from diamond_auto_live_5_patch import (
    AUTO_STATE_FILE,
    SOURCE,
    read_json,
    to_float,
    write_json_atomic,
)

LOG = logging.getLogger("diamond_pushover")

PUSHOVER_ENDPOINT = "https://api.pushover.net/1/messages.json"
PUSHOVER_USER_ENV = "PUSHOVER_USER_KEY"
PUSHOVER_TOKEN_ENV = "PUSHOVER_API_TOKEN"
OPEN_RETRY_SECONDS = 60
OPEN_EXPIRE_SECONDS = 1200


def _credentials() -> Tuple[str, str]:
    user = os.getenv(PUSHOVER_USER_ENV, "").strip()
    token = os.getenv(PUSHOVER_TOKEN_ENV, "").strip()
    return user, token


def configured() -> bool:
    user, token = _credentials()
    return bool(user and token)


def build_payload(
    title: str,
    message: str,
    *,
    priority: int = 0,
    sound: Optional[str] = None,
    retry: Optional[int] = None,
    expire: Optional[int] = None,
) -> Dict[str, str]:
    user, token = _credentials()
    payload: Dict[str, str] = {
        "token": token,
        "user": user,
        "title": str(title)[:250],
        "message": str(message)[:1024],
        "priority": str(int(priority)),
    }
    if sound:
        payload["sound"] = str(sound)
    if int(priority) == 2:
        retry_value = max(30, int(retry or OPEN_RETRY_SECONDS))
        expire_value = max(30, min(10800, int(expire or OPEN_EXPIRE_SECONDS)))
        payload["retry"] = str(retry_value)
        payload["expire"] = str(expire_value)
    return payload


def send_message(
    title: str,
    message: str,
    *,
    priority: int = 0,
    sound: Optional[str] = None,
    retry: Optional[int] = None,
    expire: Optional[int] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    if not configured():
        raise RuntimeError("PUSHOVER_NOT_CONFIGURED")

    payload = build_payload(
        title,
        message,
        priority=priority,
        sound=sound,
        retry=retry,
        expire=expire,
    )
    encoded = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        PUSHOVER_ENDPOINT,
        data=encoded,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"PUSHOVER_HTTP_{exc.code}:{body}") from exc
    except Exception as exc:
        raise RuntimeError(f"PUSHOVER_SEND_FAILED:{type(exc).__name__}") from exc

    try:
        result = json.loads(raw)
    except Exception as exc:
        raise RuntimeError("PUSHOVER_INVALID_RESPONSE") from exc

    if not isinstance(result, dict) or int(to_float(result.get("status"), 0)) != 1:
        errors = result.get("errors") if isinstance(result, dict) else None
        raise RuntimeError(f"PUSHOVER_REJECTED:{errors or 'unknown'}")

    return result


def _sequence(position: Dict[str, Any]) -> int:
    return int(to_float(position.get("canary_trade_number"), 0))


def _completed_sequences(state: Dict[str, Any]) -> list[int]:
    values = []
    for value in state.get("completed_sequences") or []:
        number = int(to_float(value, 0))
        if number > 0:
            values.append(number)
    return sorted(set(values))


def _alerted(state: Dict[str, Any], key: str) -> list[int]:
    values = []
    for value in state.get(key) or []:
        number = int(to_float(value, 0))
        if number > 0:
            values.append(number)
    return sorted(set(values))


def _open_auto_info(position: Dict[str, Any]) -> Tuple[bool, Dict[str, Any], int, int]:
    state = read_json(AUTO_STATE_FILE)
    if state.get("source") != SOURCE:
        return False, state, 0, 0

    sequence = _sequence(position)
    expected = int(to_float(state.get("active_expected_sequence"), 0))
    candidate_key = str(position.get("candidate_key") or "")
    active_key = str(state.get("active_candidate_key") or "")

    if sequence <= 0 or sequence != expected:
        return False, state, sequence, 0
    if active_key and candidate_key and active_key != candidate_key:
        return False, state, sequence, 0

    slot = int(to_float(state.get("completed_buys"), 0)) + 1
    target = max(1, int(to_float(state.get("target_buys"), 5)))
    slot = max(1, min(target, slot))
    return True, state, sequence, slot


def _close_auto_info(position: Dict[str, Any]) -> Tuple[bool, Dict[str, Any], int, int]:
    state = read_json(AUTO_STATE_FILE)
    if state.get("source") != SOURCE:
        return False, state, 0, 0

    sequence = _sequence(position)
    completed = _completed_sequences(state)
    if sequence <= 0 or sequence not in completed:
        return False, state, sequence, 0

    slot = completed.index(sequence) + 1
    return True, state, sequence, slot


def _mark_alerted(state: Dict[str, Any], key: str, sequence: int) -> None:
    values = _alerted(state, key)
    if sequence not in values:
        values.append(sequence)
    state[key] = sorted(set(values))
    write_json_atomic(AUTO_STATE_FILE, state)


def _fmt_price(value: Any) -> str:
    number = to_float(value, 0.0)
    if number <= 0:
        return "-"
    if number >= 100:
        return f"{number:.2f}"
    if number >= 1:
        return f"{number:.5f}"
    return f"{number:.8f}"


def notify_auto_open(symbol: str, position: Dict[str, Any]) -> bool:
    is_auto, state, sequence, slot = _open_auto_info(position)
    if not is_auto:
        return False
    if sequence in _alerted(state, "pushover_open_alerted_sequences"):
        return True

    target = max(1, int(to_float(state.get("target_buys"), 5)))
    quote_amount = to_float(position.get("quote_amount"), 0.0)
    fee = to_float(position.get("fees_buy_quote"), 0.0)
    title = f"DIAMOND AUTO BUY {slot}/{target}"
    message = "\n".join([
        f"{symbol} is automatisch GEKOCHT.",
        f"Inzet: €{quote_amount:.2f} | fee €{fee:.2f}",
        f"Entry: {_fmt_price(position.get('entry_price'))}",
        f"Stop: {_fmt_price(position.get('stop_loss'))}",
        f"Target: {_fmt_price(position.get('take_profit'))}",
        f"Strategie: {position.get('strategy') or '-'}",
        f"Regime: {position.get('market_regime') or '-'}",
        "Order is bevestigd. Geen handmatige actie nodig.",
        "Bevestig deze Pushover-melding om het alarm te stoppen.",
    ])

    send_message(
        title,
        message,
        priority=2,
        sound="siren",
        retry=OPEN_RETRY_SECONDS,
        expire=OPEN_EXPIRE_SECONDS,
    )
    _mark_alerted(state, "pushover_open_alerted_sequences", sequence)
    LOG.warning("PUSHOVER AUTO BUY verzonden | %s | slot=%d/%d | seq=%d", symbol, slot, target, sequence)
    return True


def notify_auto_close(
    symbol: str,
    position: Dict[str, Any],
    reason: str,
    actual_net_pnl_quote: float,
    holding_time_min: float,
) -> bool:
    is_auto, state, sequence, slot = _close_auto_info(position)
    if not is_auto:
        return False
    if sequence in _alerted(state, "pushover_close_alerted_sequences"):
        return True

    target = max(1, int(to_float(state.get("target_buys"), 5)))
    pnl = to_float(actual_net_pnl_quote, 0.0)
    sign = "+" if pnl > 0 else ""
    title = f"DIAMOND SELL {slot}/{target} | {sign}€{pnl:.2f}"
    message = "\n".join([
        f"{symbol} is VERKOCHT.",
        f"Netto resultaat: {sign}€{pnl:.2f}",
        f"Reden: {reason or '-'}",
        f"Houdtijd: {to_float(holding_time_min, 0.0):.1f} min",
        f"AUTO LIVE 5 voortgang: {int(to_float(state.get('completed_buys'), 0))}/{target}",
    ])

    send_message(
        title,
        message,
        priority=1,
        sound="cashregister" if pnl >= 0 else "falling",
    )
    _mark_alerted(state, "pushover_close_alerted_sequences", sequence)
    LOG.warning("PUSHOVER SELL verzonden | %s | slot=%d/%d | pnl=%+.2f", symbol, slot, target, pnl)
    return True


def install_pushover_hooks() -> None:
    import diamond_bot

    Bot = diamond_bot.Bot
    if getattr(Bot, "_diamond_pushover_hooks_installed", False):
        return

    original_open = Bot.canary_open_event
    original_close = Bot.canary_close_event

    def canary_open_event_with_push(self: Any, symbol: str, position: Dict[str, Any], recovered: bool = False) -> None:
        original_open(self, symbol, position, recovered=recovered)
        try:
            notify_auto_open(symbol, position)
        except Exception as exc:
            LOG.warning("PUSHOVER BUY waarschuwing mislukt | %s | %s", symbol, exc)

    def canary_close_event_with_push(
        self: Any,
        symbol: str,
        position: Dict[str, Any],
        reason: str,
        order: Dict[str, Any],
        filled_amount: float,
        exit_quote_actual: float,
        sell_fee_quote: float,
        actual_net_pnl_quote: float,
        holding_time_min: float,
        reference_bid: float,
        sell_spread_pct: float,
        recovered: bool,
    ) -> None:
        original_close(
            self,
            symbol,
            position,
            reason,
            order,
            filled_amount,
            exit_quote_actual,
            sell_fee_quote,
            actual_net_pnl_quote,
            holding_time_min,
            reference_bid,
            sell_spread_pct,
            recovered,
        )
        try:
            notify_auto_close(
                symbol,
                position,
                reason,
                actual_net_pnl_quote,
                holding_time_min,
            )
        except Exception as exc:
            LOG.warning("PUSHOVER SELL waarschuwing mislukt | %s | %s", symbol, exc)

    Bot.canary_open_event = canary_open_event_with_push
    Bot.canary_close_event = canary_close_event_with_push
    Bot._diamond_pushover_hooks_installed = True


def self_test() -> None:
    old_user = os.environ.get(PUSHOVER_USER_ENV)
    old_token = os.environ.get(PUSHOVER_TOKEN_ENV)
    try:
        os.environ[PUSHOVER_USER_ENV] = "u" * 30
        os.environ[PUSHOVER_TOKEN_ENV] = "a" * 30
        emergency = build_payload(
            "test",
            "body",
            priority=2,
            sound="siren",
            retry=60,
            expire=1200,
        )
        assert emergency["priority"] == "2"
        assert emergency["retry"] == "60"
        assert emergency["expire"] == "1200"
        assert emergency["sound"] == "siren"

        normal = build_payload("test", "body", priority=1, sound="falling")
        assert normal["priority"] == "1"
        assert "retry" not in normal
        assert "expire" not in normal

        state = {
            "source": SOURCE,
            "active_expected_sequence": 7,
            "active_candidate_key": "K",
            "completed_buys": 1,
            "target_buys": 5,
        }
        assert int(to_float(state["active_expected_sequence"], 0)) == 7
        print("DIAMOND_PUSHOVER_ALERTS_SELF_TEST_OK")
    finally:
        if old_user is None:
            os.environ.pop(PUSHOVER_USER_ENV, None)
        else:
            os.environ[PUSHOVER_USER_ENV] = old_user
        if old_token is None:
            os.environ.pop(PUSHOVER_TOKEN_ENV, None)
        else:
            os.environ[PUSHOVER_TOKEN_ENV] = old_token


def live_test() -> None:
    if not configured():
        raise SystemExit("PUSHOVER TEST: sleutels ontbreken in deze Render-omgeving")
    result = send_message(
        "DIAMOND TRADER TESTALARM",
        "Pushover-koppeling werkt. TEST - er is GEEN order geplaatst. Bevestig de melding om het alarm te stoppen.",
        priority=2,
        sound="siren",
        retry=60,
        expire=180,
    )
    print("PUSHOVER LIVE TEST: OK")
    if result.get("receipt"):
        print("Emergency receipt: ontvangen")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--check-env", action="store_true")
    parser.add_argument("--live-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return
    if args.check_env:
        user, token = _credentials()
        print(f"PUSHOVER_USER_KEY : {'AANWEZIG' if user else 'ONTBREEKT'}")
        print(f"PUSHOVER_API_TOKEN: {'AANWEZIG' if token else 'ONTBREEKT'}")
        return
    if args.live_test:
        live_test()
        return
    parser.print_help()


if __name__ == "__main__":
    main()
