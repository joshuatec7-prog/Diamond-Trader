#!/usr/bin/env python3
"""Hard-bounded AUTO LIVE 5 patch for Diamond Trader.

This module does not place orders itself. It temporarily creates the same
fail-closed one-shot LIVE approval that Diamond Bot already requires, and only
while one exact, fresh SELECTIVE LONG trend-breakout contract is being handled
inside the existing bot process.

Safety properties:
- hard maximum of five confirmed automatic BUYs per persistent run;
- maximum €130 stake (or lower if config is lower);
- only LONG trend_breakout contracts;
- existing crash guard, liquidity gate, spread, balance, recovery and exchange
  checks remain in diamond_bot.py and the execution adapter;
- approval is valid for at most 90 seconds and is revoked immediately after the
  candidate attempt;
- existing positions/pending/recovery block a new automatic approval;
- first activation baselines time: signals detected before activation are not
  automatically bought;
- state lives on /var/data and survives deploys/restarts.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

LOG = logging.getLogger("diamond_auto_live_5")

AUTO_STATE_FILE = Path("/var/data/diamond_auto_live_5_state.json")
CANARY_EXECUTION_FILE = Path("/var/data/diamond_canary_execution.csv")
SOURCE = "AUTO_LIVE_5"
HARD_TARGET_BUYS = 5
HARD_MAX_STAKE_EUR = 130.0
HARD_MAX_SIGNAL_AGE_MINUTES = 20.0
HARD_MAX_APPROVAL_SECONDS = 90


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "ja", "aan", "on", "waar"}:
        return True
    if text in {"0", "false", "no", "nee", "uit", "off", "onwaar"}:
        return False
    return default


def cfg_get(cfg: Dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = cfg
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def parse_time(value: Any) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
    ) as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        tmp_name = handle.name
    os.replace(tmp_name, path)


def read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def auto_enabled(cfg: Dict[str, Any]) -> bool:
    env_value = os.getenv("DIAMOND_AUTO_LIVE_5_ENABLED")
    if env_value is not None:
        return to_bool(env_value, False)
    return to_bool(cfg_get(cfg, "execution.auto_live_5_enabled", False), False)


def target_buys(cfg: Dict[str, Any]) -> int:
    requested = int(to_float(cfg_get(cfg, "execution.auto_live_5_target_buys", 5), 5))
    return max(1, min(HARD_TARGET_BUYS, requested))


def signal_age_limit_minutes(cfg: Dict[str, Any]) -> float:
    requested = to_float(
        cfg_get(cfg, "execution.auto_live_5_max_signal_age_minutes", 20.0),
        20.0,
    )
    return max(1.0, min(HARD_MAX_SIGNAL_AGE_MINUTES, requested))


def approval_seconds(cfg: Dict[str, Any]) -> int:
    requested = int(to_float(cfg_get(cfg, "execution.auto_live_5_approval_seconds", 90), 90))
    return max(30, min(HARD_MAX_APPROVAL_SECONDS, requested))


def bounded_stake(cfg: Dict[str, Any]) -> float:
    fixed = max(0.0, to_float(cfg_get(cfg, "risk.fixed_stake_quote", 130.0), 130.0))
    live_hard = max(0.0, to_float(cfg_get(cfg, "risk.live_hard_max_stake_quote", 130.0), 130.0))
    return min(HARD_MAX_STAKE_EUR, fixed, live_hard)


def new_state(sequence: int, cfg: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    return {
        "version": 1,
        "status": "RUNNING",
        "source": SOURCE,
        "activated_at": now.isoformat(),
        "start_sequence": int(sequence),
        "target_buys": target_buys(cfg),
        "completed_buys": 0,
        "completed_sequences": [],
        "failed_sequences": [],
        "active_candidate_key": None,
        "active_symbol": None,
        "active_expected_sequence": None,
        "active_started_at": None,
        "last_completed_at": None,
        "last_reason": "activated",
    }


def ensure_state(bot: Any, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    state = read_json(AUTO_STATE_FILE)
    if not state:
        sequence = int(to_float((bot.state or {}).get("canary_trade_sequence"), 0))
        state = new_state(sequence, bot.cfg, now)
        write_json_atomic(AUTO_STATE_FILE, state)
        LOG.warning(
            "AUTO LIVE 5 GEACTIVEERD | start_sequence=%d | doel=%d | stake<=€%.2f",
            sequence,
            state["target_buys"],
            bounded_stake(bot.cfg),
        )
        return state

    state.setdefault("version", 1)
    state.setdefault("source", SOURCE)
    state.setdefault("status", "RUNNING")
    state.setdefault("target_buys", target_buys(bot.cfg))
    state["target_buys"] = min(HARD_TARGET_BUYS, max(1, int(to_float(state["target_buys"], 5))))
    state.setdefault("completed_buys", 0)
    state.setdefault("completed_sequences", [])
    state.setdefault("failed_sequences", [])
    state.setdefault("active_candidate_key", None)
    state.setdefault("active_symbol", None)
    state.setdefault("active_expected_sequence", None)
    state.setdefault("active_started_at", None)
    return state


def canary_open_exists(sequence: int, path: Path = CANARY_EXECUTION_FILE) -> bool:
    if sequence <= 0 or not path.exists():
        return False
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if str(row.get("event") or "").strip().upper() != "OPEN":
                    continue
                if int(to_float(row.get("canary_trade_number"), 0)) != sequence:
                    continue
                if str(row.get("dry_run") or "").strip().lower() in {"true", "1", "yes"}:
                    continue
                return True
    except Exception as exc:
        LOG.warning("AUTO LIVE 5 canary-log kon niet worden gelezen: %s", exc)
    return False


def _position_confirms(bot: Any, sequence: int, candidate_key: str) -> bool:
    positions = (bot.state or {}).get("positions") or {}
    if not isinstance(positions, dict):
        return False
    for position in positions.values():
        if not isinstance(position, dict):
            continue
        seq = int(to_float(position.get("canary_trade_number"), 0))
        key = str(position.get("candidate_key") or "")
        if seq == sequence and (not candidate_key or key == candidate_key):
            return True
    return False


def reconcile_state(bot: Any, state: Dict[str, Any]) -> Dict[str, Any]:
    expected = int(to_float(state.get("active_expected_sequence"), 0))
    if expected <= 0:
        if int(to_float(state.get("completed_buys"), 0)) >= int(to_float(state.get("target_buys"), 5)):
            state["status"] = "COMPLETE"
        return state

    candidate_key = str(state.get("active_candidate_key") or "")
    confirmed = _position_confirms(bot, expected, candidate_key) or canary_open_exists(expected)

    if confirmed:
        completed_sequences = [
            int(to_float(x, 0))
            for x in (state.get("completed_sequences") or [])
            if int(to_float(x, 0)) > 0
        ]
        if expected not in completed_sequences:
            completed_sequences.append(expected)
        completed_sequences = sorted(set(completed_sequences))
        state["completed_sequences"] = completed_sequences
        state["completed_buys"] = len(completed_sequences)
        state["last_completed_at"] = datetime.now(timezone.utc).isoformat()
        state["last_reason"] = f"confirmed_open_sequence_{expected}"
        state["active_candidate_key"] = None
        state["active_symbol"] = None
        state["active_expected_sequence"] = None
        state["active_started_at"] = None
        if state["completed_buys"] >= int(to_float(state.get("target_buys"), 5)):
            state["status"] = "COMPLETE"
        write_json_atomic(AUTO_STATE_FILE, state)
        return state

    pending = (bot.state or {}).get("pending_orders") or {}
    recovery = to_bool((bot.state or {}).get("recovery_required"), False)
    current_sequence = int(to_float((bot.state or {}).get("canary_trade_sequence"), 0))

    if pending or recovery:
        state["last_reason"] = "waiting_for_pending_or_recovery"
        write_json_atomic(AUTO_STATE_FILE, state)
        return state

    if current_sequence >= expected:
        failed = [
            int(to_float(x, 0))
            for x in (state.get("failed_sequences") or [])
            if int(to_float(x, 0)) > 0
        ]
        if expected not in failed:
            failed.append(expected)
        state["failed_sequences"] = sorted(set(failed))
        state["last_reason"] = f"no_confirmed_open_sequence_{expected}"
    else:
        state["last_reason"] = str(
            state.get("last_block_reason")
            or "candidate_blocked_before_order_prepare"
        )

    state["active_candidate_key"] = None
    state["active_symbol"] = None
    state["active_expected_sequence"] = None
    state["active_started_at"] = None
    write_json_atomic(AUTO_STATE_FILE, state)
    return state


def revoke_approval(bot: Any, reason: str) -> None:
    path = Path(getattr(bot, "live_approval_file", "/var/data/diamond_live_approval.json"))
    existing = read_json(path)
    revoked = {
        "status": "REVOKED",
        "approved": False,
        "mode": "LIVE",
        "allow_new_entries": False,
        "source": SOURCE,
        "revoked_at": datetime.now(timezone.utc).isoformat(),
        "reason": str(reason),
    }
    if existing.get("source") == SOURCE or auto_enabled(bot.cfg):
        write_json_atomic(path, revoked)


def validate_contract(
    cfg: Dict[str, Any],
    state: Dict[str, Any],
    contract: Dict[str, Any],
    now: Optional[datetime] = None,
) -> Tuple[bool, str, Optional[datetime]]:
    now = now or datetime.now(timezone.utc)

    if str(contract.get("side") or "").upper() != "LONG":
        return False, "long_only", None
    if str(contract.get("strategy") or "") != "trend_breakout":
        return False, "trend_breakout_only", None

    candidate_key = str(contract.get("candidate_key") or "").strip()
    symbol = str(contract.get("symbol") or "").strip().upper()
    if not candidate_key or not symbol:
        return False, "candidate_identity_missing", None

    regime = str(contract.get("market_regime") or "").strip().upper()
    if not regime or regime in {"BEARISH", "BEARISH_WEAK"}:
        return False, "regime_blocks_long", None

    detected = parse_time(contract.get("detected_at"))
    if detected is None:
        return False, "detected_at_invalid", None

    activated = parse_time(state.get("activated_at"))
    if activated is not None and detected < activated:
        return False, "predates_auto_activation", detected

    age_minutes = (now - detected).total_seconds() / 60.0
    if age_minutes < -1.0:
        return False, "future_signal", detected
    if age_minutes > signal_age_limit_minutes(cfg):
        return False, "signal_expired", detected

    if bounded_stake(cfg) <= 0:
        return False, "invalid_stake", detected

    if int(to_float(state.get("completed_buys"), 0)) >= int(to_float(state.get("target_buys"), 5)):
        return False, "target_reached", detected

    return True, "ok", detected


def build_approval(
    bot: Any,
    state: Dict[str, Any],
    contract: Dict[str, Any],
    now: Optional[datetime] = None,
) -> Tuple[Optional[Dict[str, Any]], str]:
    now = now or datetime.now(timezone.utc)
    ok, reason, detected = validate_contract(bot.cfg, state, contract, now)
    if not ok or detected is None:
        return None, reason

    if getattr(bot, "open_positions_count")() > 0:
        return None, "open_position_exists"
    if getattr(bot, "entries_blocked_by_recovery")():
        return None, "pending_or_recovery"

    current_sequence = int(to_float((bot.state or {}).get("canary_trade_sequence"), 0))
    required = max(
        5,
        int(to_float(cfg_get(bot.cfg, "execution.require_canary_graduation_trades", 5), 5)),
    )
    if current_sequence < required:
        return None, "canary_graduation_incomplete"

    expected_sequence = current_sequence + 1
    slot = int(to_float(state.get("completed_buys"), 0)) + 1
    expiry = min(
        detected + timedelta(minutes=signal_age_limit_minutes(bot.cfg)),
        now + timedelta(seconds=approval_seconds(bot.cfg)),
    )
    if (expiry - now).total_seconds() < 15:
        return None, "insufficient_safe_time"

    state["active_candidate_key"] = str(contract.get("candidate_key") or "")
    state["active_symbol"] = str(contract.get("symbol") or "").upper()
    state["active_expected_sequence"] = expected_sequence
    state["active_started_at"] = now.isoformat()
    state["last_reason"] = f"approval_prepared_slot_{slot}"
    write_json_atomic(AUTO_STATE_FILE, state)

    approval = {
        "status": "APPROVED",
        "approved": True,
        "mode": "LIVE",
        "allow_new_entries": True,
        "source": SOURCE,
        "approved_at": now.isoformat(),
        "expires_at": expiry.isoformat(),
        "max_stake_quote": bounded_stake(bot.cfg),
        "graduated_from_canary": True,
        "entry_sequence_start": current_sequence,
        "max_live_entries": 1,
        "auto_live_5_slot": slot,
        "auto_live_5_target": int(to_float(state.get("target_buys"), 5)),
        "candidate_key": str(contract.get("candidate_key") or ""),
        "symbol": str(contract.get("symbol") or "").upper(),
        "detected_at": str(contract.get("detected_at") or ""),
        "reason": f"automatic bounded LIVE entry {slot}/{int(to_float(state.get('target_buys'), 5))}",
    }
    return approval, "approved"


def _install_patch(diamond_bot_module: Any) -> None:
    Bot = diamond_bot_module.Bot
    if getattr(Bot, "_diamond_auto_live_5_installed", False):
        return

    original_execute = Bot.execute_selective_contracts

    def execute_selective_contracts_auto(self: Any) -> int:
        if not auto_enabled(self.cfg) or self.dry_run:
            return original_execute(self)

        state = ensure_state(self)
        state = reconcile_state(self, state)
        revoke_approval(self, "auto_live_5_waiting_for_candidate")

        contracts = self.selective_execution_candidates()
        if not contracts:
            return 0

        handled = 0
        for contract in contracts:
            handled += 1
            state = ensure_state(self)
            state = reconcile_state(self, state)

            target = int(to_float(state.get("target_buys"), 5))
            completed = int(to_float(state.get("completed_buys"), 0))
            if state.get("status") == "COMPLETE" or completed >= target:
                state["status"] = "COMPLETE"
                state["last_reason"] = "hard_target_reached"
                write_json_atomic(AUTO_STATE_FILE, state)
                revoke_approval(self, "auto_live_5_complete")
                LOG.warning("AUTO LIVE 5 KLAAR | %d/%d BUYs | nieuwe auto-BUYs geblokkeerd", completed, target)
                continue

            if state.get("active_expected_sequence"):
                LOG.warning(
                    "AUTO LIVE 5 WACHT | actieve sequence=%s | pending/recovery controle",
                    state.get("active_expected_sequence"),
                )
                continue

            approval, reason = build_approval(self, state, contract)
            if approval is None:
                LOG.info(
                    "AUTO LIVE 5 OVERSLAAN | %s | %s | %s",
                    contract.get("symbol"),
                    contract.get("candidate_key"),
                    reason,
                )
                continue

            approval_path = Path(
                getattr(self, "live_approval_file", "/var/data/diamond_live_approval.json")
            )
            write_json_atomic(approval_path, approval)

            slot = int(to_float(approval.get("auto_live_5_slot"), 0))
            LOG.warning(
                "AUTO LIVE 5 APPROVAL | slot=%d/%d | %s | key=%s | expiry=%s | stake=€%.2f",
                slot,
                int(to_float(approval.get("auto_live_5_target"), 5)),
                approval.get("symbol"),
                approval.get("candidate_key"),
                approval.get("expires_at"),
                to_float(approval.get("max_stake_quote"), 0.0),
            )

            signal = self.selective_contract_to_long_signal(contract)
            signal["auto_live_5"] = True
            signal["auto_live_5_slot"] = slot

            state = ensure_state(self)
            state.pop("last_block_reason", None)
            state.pop("last_block_symbol", None)
            state.pop("last_block_at", None)
            write_json_atomic(AUTO_STATE_FILE, state)

            try:
                self.try_buy_symbol(
                    str(contract.get("symbol") or ""),
                    precomputed_signal=signal,
                    precomputed_news_gate={
                        "allow": True,
                        "reason": "SELECTIVE_CONTRACT_AUTO_LIVE_5",
                    },
                )
            finally:
                state = ensure_state(self)
                state = reconcile_state(self, state)
                revoke_approval(self, "auto_live_5_candidate_attempt_finished")

                completed = int(to_float(state.get("completed_buys"), 0))
                target = int(to_float(state.get("target_buys"), 5))
                if completed >= target:
                    state["status"] = "COMPLETE"
                    state["last_reason"] = "hard_target_reached"
                    write_json_atomic(AUTO_STATE_FILE, state)
                    LOG.warning("AUTO LIVE 5 KLAAR | %d/%d bevestigde BUYs", completed, target)

            if getattr(self, "open_positions_count")() > 0:
                break

        return handled

    Bot.execute_selective_contracts = execute_selective_contracts_auto
    Bot._diamond_auto_live_5_installed = True


def install_auto_live_5_patch() -> None:
    import diamond_bot

    _install_patch(diamond_bot)


def self_test() -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.cfg = {
                "risk": {
                    "fixed_stake_quote": 130,
                    "live_hard_max_stake_quote": 130,
                },
                "execution": {
                    "auto_live_5_enabled": True,
                    "auto_live_5_target_buys": 99,
                    "auto_live_5_max_signal_age_minutes": 20,
                    "auto_live_5_approval_seconds": 90,
                    "require_canary_graduation_trades": 5,
                },
            }
            self.state = {
                "positions": {},
                "pending_orders": {},
                "recovery_required": False,
                "canary_trade_sequence": 6,
            }

        def open_positions_count(self) -> int:
            return len(self.state["positions"])

        def entries_blocked_by_recovery(self) -> bool:
            return bool(self.state["pending_orders"] or self.state["recovery_required"])

    now = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
    bot = FakeBot()
    state = new_state(6, bot.cfg, now - timedelta(minutes=3))
    assert state["target_buys"] == 5
    assert bounded_stake(bot.cfg) == 130.0

    contract = {
        "candidate_key": "XRP/EUR|trend_breakout|LONG|2026-08-23T11:45:00+00:00",
        "detected_at": (now - timedelta(minutes=2)).isoformat(),
        "symbol": "XRP/EUR",
        "side": "LONG",
        "strategy": "trend_breakout",
        "market_regime": "BULLISH",
    }
    ok, reason, _ = validate_contract(bot.cfg, state, contract, now)
    assert ok, reason

    stale = dict(contract)
    stale["detected_at"] = (now - timedelta(minutes=21)).isoformat()
    state_stale = dict(state)
    state_stale["activated_at"] = (now - timedelta(minutes=30)).isoformat()
    ok, reason, _ = validate_contract(bot.cfg, state_stale, stale, now)
    assert not ok and reason == "signal_expired"

    old = dict(contract)
    old["detected_at"] = (now - timedelta(minutes=2)).isoformat()
    state_old = dict(state)
    state_old["activated_at"] = (now - timedelta(minutes=1)).isoformat()
    ok, reason, _ = validate_contract(bot.cfg, state_old, old, now)
    assert not ok and reason == "predates_auto_activation"

    full = dict(state)
    full["completed_buys"] = 5
    ok, reason, _ = validate_contract(bot.cfg, full, contract, now)
    assert not ok and reason == "target_reached"

    bot.cfg["risk"]["fixed_stake_quote"] = 500
    bot.cfg["risk"]["live_hard_max_stake_quote"] = 500
    assert bounded_stake(bot.cfg) == 130.0

    print("DIAMOND_AUTO_LIVE_5_PATCH_SELF_TEST_OK")


if __name__ == "__main__":
    self_test()
