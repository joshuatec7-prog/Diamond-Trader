#!/usr/bin/env python3
# Diamond Trader Live Safety Monitor v1.0
#
# Read-only monitoring voor canary/live.
# Geen orders, geen private API, geen automatische stop en geen live/config wijziging.

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

try:
    import yaml
except Exception:
    yaml = None


VERSION = "1.0"
ROOT = Path(__file__).resolve().parent
DATA = Path("/var/data")

DEFAULT_ANALYSIS = DATA / "diamond_canary_log_analysis.json"
DEFAULT_STATE = DATA / "diamond_state.json"
DEFAULT_CONFIG = ROOT / "config.yaml"
DEFAULT_OUTPUT = DATA / "diamond_live_safety_status.json"

REQUIRED_PROCESSES = (
    "agent.py",
    "supervisor_agent.py",
    "closed_candle_runner.py",
    "periodic_analysis_runner.py",
)

DD_WARNING_EUR = 17.0
DD_PAUSE_EUR = 23.0
LOSS_STREAK_WARNING = 4
LOSS_STREAK_PAUSE = 5
RESERVE_MIN_EUR = 250.0
MAX_POSITIONS = 5


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "ja", "on", "aan"}:
        return True
    if text in {"0", "false", "no", "nee", "off", "uit"}:
        return False
    return default


def cfg_value(cfg: Dict[str, Any], dotted: str, default: Any = None) -> Any:
    cur: Any = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def load_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_yaml(path: Path) -> Dict[str, Any]:
    if yaml is None or not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass


def process_running(pattern: str) -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        return False


def drawdown_and_streak(trades: List[Dict[str, Any]]) -> tuple[float, int, int]:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    current_streak = 0
    max_streak = 0

    closed = [
        trade for trade in trades
        if str(trade.get("state") or "").upper() == "CLOSED"
    ]
    closed.sort(key=lambda t: to_int(t.get("trade_number"), 0))

    for trade in closed:
        pnl = to_float(trade.get("actual_pnl"), 0.0)
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

        if pnl < -1e-12:
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0

    return max_dd, current_streak, max_streak


def classify_drawdown(dd: float, current_streak: int) -> str:
    if dd > DD_PAUSE_EUR or current_streak >= LOSS_STREAK_PAUSE:
        return "PAUSE_CANDIDATE"
    if dd >= DD_WARNING_EUR or current_streak >= LOSS_STREAK_WARNING:
        return "WARNING"
    return "NORMAL"


def classify_slippage(value: float) -> str:
    if value > 0.30:
        return "STOP_CANDIDATE"
    if value > 0.20:
        return "HIGH"
    if value > 0.10:
        return "WARNING"
    return "OK"


def severity(status: str) -> int:
    return {
        "READY": 0,
        "NORMAL": 0,
        "OK": 0,
        "WARNING": 1,
        "HIGH": 2,
        "PAUSE_CANDIDATE": 3,
        "STOP_CANDIDATE": 3,
        "FAIL": 4,
    }.get(str(status).upper(), 0)


def worst(*statuses: str) -> str:
    clean = [str(s or "NORMAL").upper() for s in statuses]
    return max(clean, key=severity) if clean else "NORMAL"


def evaluate(
    analysis: Dict[str, Any],
    state: Dict[str, Any],
    cfg: Dict[str, Any],
    *,
    process_check: bool = True,
) -> Dict[str, Any]:
    closed = to_int(analysis.get("closed_trades"), 0)
    trades = list(analysis.get("trades") or [])

    max_dd, current_streak, max_streak = drawdown_and_streak(trades)
    risk_status = classify_drawdown(max_dd, current_streak)

    max_slippage = to_float(
        analysis.get("max_adverse_slippage_pct"),
        0.0,
    )
    slip_status = classify_slippage(max_slippage)

    pending = state.get("pending_orders") or {}
    pending = pending if isinstance(pending, dict) else {}
    recovery_required = to_bool(state.get("recovery_required"), False)

    positions = state.get("positions") or {}
    positions = positions if isinstance(positions, dict) else {}

    reserve = to_float(cfg_value(cfg, "risk.eur_reserve", 0.0), 0.0)
    max_open = to_int(cfg_value(cfg, "risk.max_open_positions", 999), 999)
    max_total = to_int(cfg_value(cfg, "trading.max_total_positions", 999), 999)
    dry_run = to_bool(cfg_value(cfg, "risk.dry_run", True), True)

    processes = {}
    if process_check:
        processes = {
            pattern: process_running(pattern)
            for pattern in REQUIRED_PROCESSES
        }
    processes_ok = all(processes.values()) if process_check else True

    safety_flags: List[str] = []
    if pending:
        safety_flags.append(f"pending_orders={len(pending)}")
    if recovery_required:
        safety_flags.append("recovery_required=JA")
    if len(positions) > MAX_POSITIONS:
        safety_flags.append(f"open_positions={len(positions)}>{MAX_POSITIONS}")
    if reserve < RESERVE_MIN_EUR:
        safety_flags.append(f"configured_reserve=€{reserve:.2f}<€{RESERVE_MIN_EUR:.0f}")
    if max_open > MAX_POSITIONS or max_total > MAX_POSITIONS:
        safety_flags.append(f"position_limit={max_open}/{max_total}>{MAX_POSITIONS}")
    if not processes_ok:
        safety_flags.append("hoofdproces_ontbreekt")

    integrity = str(
        analysis.get("status")
        or ("READY" if closed == 0 else "OK")
    ).upper()

    if pending or recovery_required or not processes_ok:
        safety_core = "PAUSE_CANDIDATE"
    elif len(positions) > MAX_POSITIONS or reserve < RESERVE_MIN_EUR:
        safety_core = "PAUSE_CANDIDATE"
    elif max_open > MAX_POSITIONS or max_total > MAX_POSITIONS:
        safety_core = "PAUSE_CANDIDATE"
    else:
        safety_core = "NORMAL"

    integrity_for_overall = (
        integrity
        if integrity in {"FAIL", "STOP_CANDIDATE", "HIGH", "WARNING"}
        else "NORMAL"
    )

    if closed == 0:
        overall = worst("READY", safety_core, slip_status, integrity_for_overall)
        if overall == "NORMAL":
            overall = "READY"
    else:
        overall = worst(risk_status, slip_status, safety_core, integrity_for_overall)

    return {
        "version": VERSION,
        "generated_at": now_iso(),
        "status": overall,
        "closed_live_trades": closed,
        "actual_net_pnl_quote": to_float(analysis.get("actual_net_pnl_quote"), 0.0),
        "profit_factor": analysis.get("profit_factor"),
        "max_drawdown_eur": round(max_dd, 8),
        "current_loss_streak": current_streak,
        "max_loss_streak": max_streak,
        "drawdown_status": risk_status,
        "max_adverse_slippage_pct": max_slippage,
        "slippage_status": slip_status,
        "configured_reserve_eur": reserve,
        "open_positions": len(positions),
        "max_open_positions_config": max_open,
        "max_total_positions_config": max_total,
        "pending_orders": len(pending),
        "recovery_required": recovery_required,
        "dry_run": dry_run,
        "processes_ok": processes_ok,
        "processes": processes,
        "analysis_integrity_status": integrity,
        "safety_flags": safety_flags,
        "automatic_stop": False,
        "automatic_live_change": False,
        "private_api_used": False,
    }


def pf_text(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if math.isinf(number):
        return "INF"
    return f"{number:.4f}"


def print_report(result: Dict[str, Any]) -> None:
    print("=" * 78)
    print(f" DIAMOND LIVE SAFETY MONITOR v{VERSION}")
    print("=" * 78)
    print(f"STATUS              : {result['status']}")
    print(f"Closed live trades  : {result['closed_live_trades']}")
    print(f"Live netto PnL      : €{result['actual_net_pnl_quote']:+.4f}")
    print(f"Profit Factor       : {pf_text(result['profit_factor'])}")

    print("\n=== DRAWDOWN / LOSS STREAK ===")
    print(f"Max drawdown        : €{result['max_drawdown_eur']:.4f}")
    print(f"Current loss streak : {result['current_loss_streak']}")
    print(f"Max loss streak     : {result['max_loss_streak']}")
    print(f"Risk status         : {result['drawdown_status']}")

    print("\n=== EXECUTION ===")
    print(f"Max slippage        : {result['max_adverse_slippage_pct']:+.4f}%")
    print(f"Slippage status     : {result['slippage_status']}")
    print(f"Analyzer integrity  : {result['analysis_integrity_status']}")

    print("\n=== SAFETY / STATE ===")
    print(f"Configured reserve  : €{result['configured_reserve_eur']:.2f}")
    print(f"Open positions      : {result['open_positions']}")
    print(
        "Position limits     : "
        f"{result['max_open_positions_config']}/"
        f"{result['max_total_positions_config']}"
    )
    print(f"Pending orders      : {result['pending_orders']}")
    print(f"Recovery nodig      : {'JA' if result['recovery_required'] else 'NEE'}")
    print(f"Dry-run             : {'JA' if result['dry_run'] else 'NEE'}")
    print(f"Hoofdprocessen      : {'OK' if result['processes_ok'] else 'NIET OK'}")

    if result["processes"]:
        for name, ok in result["processes"].items():
            print(f"  {'PASS' if ok else 'FAIL'} {name}")

    if result["safety_flags"]:
        print("\n=== SAFETY FLAGS ===")
        for flag in result["safety_flags"]:
            print(f"- {flag}")

    print("\n=== DREMPELS ===")
    print("NORMAL          : DD < €17 en loss streak < 4")
    print("WARNING         : DD >= €17 of loss streak >= 4")
    print("PAUSE_CANDIDATE : DD > €23 of loss streak >= 5")
    print("Slippage        : <=0.10 OK | >0.10 WARN | >0.20 HIGH | >0.30 STOP")

    print("\nAutomatische stop     : NEE")
    print("Automatische livegang : NEE")
    print("Orders/private API    : NEE")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--analysis", default=str(DEFAULT_ANALYSIS))
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--skip-processes", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    analysis = load_json(Path(args.analysis))
    state = load_json(Path(args.state))
    cfg = load_yaml(Path(args.config))

    result = evaluate(
        analysis,
        state,
        cfg,
        process_check=not args.skip_processes,
    )

    if not args.no_write:
        atomic_json(Path(args.output), result)

    print_report(result)

    return 2 if result["status"] in {
        "FAIL",
        "PAUSE_CANDIDATE",
        "STOP_CANDIDATE",
    } else 0


if __name__ == "__main__":
    raise SystemExit(main())
