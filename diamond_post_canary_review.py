#!/usr/bin/env python3
# Diamond Trader Post-Canary Review Automation v1.0
#
# Compacte, alleen-lezen review na echte canary trade 1 en 5.
# Gebruikt bestaande analyzer/safety/fee-output; verandert niets live.
#
# BELANGRIJK:
# - PASS_CANDIDATE is alleen een advies voor HANDMATIGE review.
# - Nooit automatisch opschalen.
# - Nooit automatisch live zetten.
# - Geen orders/private API.

from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple


VERSION = "1.0"
ROOT = Path(__file__).resolve().parent
DATA = Path("/var/data")

ANALYZER = ROOT / "diamond_canary_log_analyzer.py"
SAFETY_MONITOR = ROOT / "diamond_live_safety_monitor.py"
FEE_VALIDATOR = ROOT / "diamond_fee_cost_validator.py"

ANALYSIS_PATH = DATA / "diamond_canary_log_analysis.json"
SAFETY_PATH = DATA / "diamond_live_safety_status.json"
FEE_PATH = DATA / "diamond_fee_cost_validation.json"
OUTPUT_PATH = DATA / "diamond_post_canary_review.json"

TRADE1_MILESTONE = 1
PHASE1_CLOSED = 5

# Bestaande veiligheidsdrempels uit de live safety monitor.
DD_WARNING_EUR = 17.0
DD_PAUSE_EUR = 23.0
LOSS_STREAK_WARNING = 4
LOSS_STREAK_PAUSE = 5

SLIP_WARNING = 0.10
SLIP_HIGH = 0.20
SLIP_STOP = 0.30


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def boolish(value: Any, default: bool = False) -> bool:
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


def pf_value(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
        return number
    except (TypeError, ValueError):
        return None


def pf_text(value: Any) -> str:
    number = pf_value(value)
    if number is None:
        return "n/a"
    if math.isinf(number):
        return "INF"
    return f"{number:.4f}"


def fmt_eur(value: Any) -> str:
    number = to_float(value)
    sign = "+" if number > 0 else ""
    return f"€{sign}{number:.4f}"


def fmt_pct(value: Any) -> str:
    number = to_float(value)
    sign = "+" if number > 0 else ""
    return f"{sign}{number:.4f}%"


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


def run_helper(path: Path) -> Tuple[int, str]:
    if not path.exists():
        return 127, "ONTBREEKT"
    try:
        result = subprocess.run(
            ["python3", str(path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        text = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        return result.returncode, text
    except Exception as exc:
        return 126, f"{type(exc).__name__}:{exc}"


def refresh_sources() -> Dict[str, str]:
    """
    Bestaande read-only helpers veilig verversen.
    Een ontbrekende helper veroorzaakt geen live-effect; de status wordt
    alleen in het rapport getoond.
    """
    result = {}
    for name, path in (
        ("analyzer", ANALYZER),
        ("safety", SAFETY_MONITOR),
        ("fee", FEE_VALIDATOR),
    ):
        rc, _ = run_helper(path)
        result[name] = "PASS" if rc == 0 else ("ONTBREEKT" if rc == 127 else "WARN")
    return result


def classify_execution_difference(
    actual: float,
    expected: float,
    difference: float,
) -> str:
    """
    Informatieve classificatie, geen harde live gate.
    Alleen forse relatieve verslechtering wordt een waarschuwing.
    """
    if abs(expected) < 0.01:
        return "NEUTRAL"

    # Negatieve difference = slechter uitgevoerd dan verwacht.
    if difference >= 0:
        return "OK"

    relative = abs(difference) / max(abs(expected), 0.01)
    if relative > 0.50 and abs(difference) >= 1.00:
        return "WARNING"
    return "OK"


def review(
    analysis: Dict[str, Any],
    safety: Dict[str, Any],
    fee: Dict[str, Any],
    helper_status: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    helper_status = helper_status or {}

    closed = to_int(analysis.get("closed_trades"), 0)
    wins = to_int(analysis.get("wins"), 0)
    losses = to_int(analysis.get("losses"), 0)
    breakeven = to_int(analysis.get("breakeven"), 0)

    actual = to_float(analysis.get("actual_net_pnl_quote"), 0.0)
    expected = to_float(analysis.get("expected_net_pnl_quote"), 0.0)
    difference = to_float(analysis.get("pnl_difference_quote"), actual - expected)

    pf = pf_value(analysis.get("profit_factor"))
    fees = to_float(analysis.get("total_fees_quote"), 0.0)
    buy_slip = to_float(analysis.get("avg_buy_slippage_pct"), 0.0)
    sell_slip = to_float(analysis.get("avg_sell_slippage_pct"), 0.0)
    max_slip = to_float(analysis.get("max_adverse_slippage_pct"), 0.0)
    recovery_events = to_int(analysis.get("recovery_events"), 0)

    analyzer_status = str(
        analysis.get("status")
        or ("READY" if closed == 0 else "OK")
    ).upper()

    safety_status = str(
        safety.get("status")
        or ("READY" if closed == 0 else "ONBEKEND")
    ).upper()
    risk_status = str(
        safety.get("drawdown_status")
        or ("NORMAL" if closed == 0 else "ONBEKEND")
    ).upper()
    slippage_status = str(
        safety.get("slippage_status")
        or ("OK" if closed == 0 else "ONBEKEND")
    ).upper()
    max_dd = to_float(safety.get("max_drawdown_eur"), 0.0)
    current_streak = to_int(safety.get("current_loss_streak"), 0)
    max_streak = to_int(safety.get("max_loss_streak"), 0)
    pending_orders = to_int(safety.get("pending_orders"), 0)
    recovery_required = boolish(safety.get("recovery_required"), False)
    processes_ok = boolish(safety.get("processes_ok"), True)
    safety_flags = list(safety.get("safety_flags") or [])

    fee_status = str(
        fee.get("status")
        or ("READY" if closed == 0 else "ONBEKEND")
    ).upper()

    execution_diff_status = classify_execution_difference(
        actual,
        expected,
        difference,
    )

    blockers: List[str] = []
    warnings: List[str] = []

    if analyzer_status in {"FAIL", "STOP_CANDIDATE"}:
        blockers.append(f"analyzer={analyzer_status}")

    if safety_status in {"PAUSE_CANDIDATE", "STOP_CANDIDATE", "FAIL"}:
        blockers.append(f"live_safety={safety_status}")

    if risk_status == "PAUSE_CANDIDATE":
        blockers.append("drawdown/loss_streak=PAUSE_CANDIDATE")

    if slippage_status == "STOP_CANDIDATE" or max_slip > SLIP_STOP:
        blockers.append("slippage=STOP_CANDIDATE")

    if fee_status == "FAIL":
        blockers.append("fee_validator=FAIL")

    if pending_orders > 0:
        blockers.append(f"pending_orders={pending_orders}")

    if recovery_required:
        blockers.append("recovery_required=JA")

    if not processes_ok:
        blockers.append("hoofdprocessen=NIET_OK")

    if safety_status in {"WARNING", "HIGH"}:
        warnings.append(f"live_safety={safety_status}")

    if risk_status == "WARNING" or max_dd >= DD_WARNING_EUR or current_streak >= LOSS_STREAK_WARNING:
        warnings.append("drawdown/loss_streak=WARNING")

    if slippage_status in {"WARNING", "HIGH"}:
        warnings.append(f"slippage={slippage_status}")
    elif max_slip > SLIP_WARNING:
        warnings.append("slippage=WARNING")

    if fee_status in {"WARNING", "HIGH"}:
        warnings.append(f"fee_validator={fee_status}")

    if execution_diff_status == "WARNING":
        warnings.append("execution_verschil=WARNING")

    if recovery_events > 0:
        warnings.append(f"recovery_events={recovery_events}")

    # Milestone / oordeel.
    if closed == 0:
        milestone = "WAIT_TRADE_1"
        verdict = "WAIT_FIRST_CANARY"
        manual_action = "Wacht op eerste gesloten echte canary-trade."

    elif blockers:
        milestone = (
            "TRADE_5_REVIEW"
            if closed >= PHASE1_CLOSED
            else "TRADE_1_PLUS_MONITORING"
        )
        verdict = "PAUSE_CANDIDATE"
        manual_action = (
            "Handmatig beoordelen en niet opschalen totdat blockers zijn opgelost."
        )

    elif closed < PHASE1_CLOSED:
        milestone = (
            "TRADE_1_REVIEW"
            if closed == TRADE1_MILESTONE
            else "PHASE1_RUNNING"
        )
        verdict = "CONTINUE_PHASE1_WITH_WARNING" if warnings else "CONTINUE_PHASE1"
        manual_action = (
            f"Fase 1 ongewijzigd voortzetten tot exact {PHASE1_CLOSED} gesloten trades; "
            "niet opschalen."
        )

    else:
        milestone = "TRADE_5_REVIEW"

        # Na 5 trades: alleen een kandidaat-oordeel voor menselijke beslissing.
        positive_edge = actual > 0.0 and (pf is None or math.isinf(pf) or pf > 1.0)

        if warnings:
            verdict = "EXTEND_CANDIDATE"
            manual_action = (
                "Fase 1 niet automatisch opschalen; handmatig beoordelen of langer "
                "op dezelfde inzet testen nodig is."
            )
        elif positive_edge:
            verdict = "PASS_CANDIDATE"
            manual_action = (
                "Resultaat is kandidaat voor handmatige PASS. Opschaling blijft "
                "geblokkeerd tot punt 9 en afzonderlijke handmatige goedkeuring."
            )
        else:
            verdict = "EXTEND_CANDIDATE"
            manual_action = (
                "Geen duidelijke positieve live-edge na 5 trades; niet opschalen en "
                "handmatig beslissen over langer testen."
            )

    # Trade 1 oordeel is een sanity-check, geen winstgate.
    trade1_check = "WAIT"
    if closed >= 1:
        trade1_check = "PASS" if not blockers else "PAUSE_CANDIDATE"

    trade5_check = "WAIT"
    if closed >= 5:
        if blockers:
            trade5_check = "PAUSE_CANDIDATE"
        elif verdict == "PASS_CANDIDATE":
            trade5_check = "PASS_CANDIDATE"
        else:
            trade5_check = "REVIEW"

    return {
        "version": VERSION,
        "generated_at": now_iso(),
        "status": verdict,
        "milestone": milestone,
        "closed_trades": closed,
        "target_phase1": PHASE1_CLOSED,
        "trade1_review": trade1_check,
        "trade5_review": trade5_check,
        "metrics": {
            "wins": wins,
            "losses": losses,
            "breakeven": breakeven,
            "actual_net_pnl_quote": actual,
            "expected_net_pnl_quote": expected,
            "pnl_difference_quote": difference,
            "profit_factor": pf,
            "total_fees_quote": fees,
            "avg_buy_slippage_pct": buy_slip,
            "avg_sell_slippage_pct": sell_slip,
            "max_adverse_slippage_pct": max_slip,
            "max_drawdown_eur": max_dd,
            "current_loss_streak": current_streak,
            "max_loss_streak": max_streak,
            "recovery_events": recovery_events,
        },
        "source_status": {
            "analyzer": analyzer_status,
            "live_safety": safety_status,
            "drawdown": risk_status,
            "slippage": slippage_status,
            "fee_validator": fee_status,
            "execution_difference": execution_diff_status,
            "helper_refresh": helper_status,
        },
        "blockers": blockers,
        "warnings": warnings,
        "safety_flags": safety_flags[:5],
        "manual_action": manual_action,
        "automatic_scaling": False,
        "automatic_live_change": False,
        "orders_used": False,
        "private_api_used": False,
    }


def print_report(result: Dict[str, Any]) -> None:
    m = result["metrics"]
    s = result["source_status"]

    print("=" * 78)
    print(f" DIAMOND POST-CANARY REVIEW v{VERSION}")
    print("=" * 78)
    print(f"Closed canary       : {result['closed_trades']}/{result['target_phase1']}")
    print(f"Mijlpaal            : {result['milestone']}")
    print(f"OORDEEL             : {result['status']}")
    print(f"Trade 1 review      : {result['trade1_review']}")
    print(f"Trade 5 review      : {result['trade5_review']}")

    print("\n=== RESULTAAT ===")
    print(
        f"W/L/BE              : "
        f"{m['wins']}/{m['losses']}/{m['breakeven']}"
    )
    print(f"Actual netto PnL    : {fmt_eur(m['actual_net_pnl_quote'])}")
    print(f"Expected netto PnL  : {fmt_eur(m['expected_net_pnl_quote'])}")
    print(f"Execution verschil  : {fmt_eur(m['pnl_difference_quote'])}")
    print(f"Profit Factor       : {pf_text(m['profit_factor'])}")
    print(f"Totale fees         : {fmt_eur(m['total_fees_quote'])}")

    print("\n=== EXECUTION / SAFETY ===")
    print(f"Gem BUY slippage    : {fmt_pct(m['avg_buy_slippage_pct'])}")
    print(f"Gem SELL slippage   : {fmt_pct(m['avg_sell_slippage_pct'])}")
    print(f"Max slippage        : {fmt_pct(m['max_adverse_slippage_pct'])} [{s['slippage']}]")
    print(f"Max drawdown        : {fmt_eur(m['max_drawdown_eur'])} [{s['drawdown']}]")
    print(f"Loss streak         : {m['current_loss_streak']} huidig / {m['max_loss_streak']} max")
    print(f"Live safety         : {s['live_safety']}")
    print(f"Fee validator       : {s['fee_validator']}")
    print(f"Analyzer            : {s['analyzer']}")

    if result["blockers"]:
        print("\n=== BLOCKERS ===")
        for item in result["blockers"]:
            print(f"- {item}")

    if result["warnings"]:
        print("\n=== WAARSCHUWINGEN ===")
        for item in result["warnings"]:
            print(f"- {item}")

    print("\n=== HANDMATIGE ACTIE ===")
    print(result["manual_action"])

    print("\nAutomatisch opschalen : NEE")
    print("Automatische livegang : NEE")
    print("Orders/private API    : NEE")


def main() -> int:
    helper_status = refresh_sources()

    analysis = load_json(ANALYSIS_PATH)
    safety = load_json(SAFETY_PATH)
    fee = load_json(FEE_PATH)

    result = review(
        analysis,
        safety,
        fee,
        helper_status=helper_status,
    )
    atomic_json(OUTPUT_PATH, result)
    print_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
