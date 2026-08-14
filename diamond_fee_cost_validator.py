#!/usr/bin/env python3
# Diamond Trader Fee / Cost Validator v1.0
#
# Alleen-lezen validatie van canary fees en PnL-rekenwerk.
# Geen orders, geen private API en geen live/config wijziging.

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

try:
    import yaml
except Exception:
    yaml = None


VERSION = "1.0"
ROOT = Path(__file__).resolve().parent
DATA = Path("/var/data")

DEFAULT_INPUT = DATA / "diamond_canary_execution.csv"
DEFAULT_CONFIG = ROOT / "config.yaml"
DEFAULT_OUTPUT = DATA / "diamond_fee_cost_validation.json"

EUR_TOLERANCE = 0.02
FEE_WARN_PP = 0.03
FEE_HIGH_PP = 0.10

REQUIRED_COLUMNS = {
    "event",
    "canary_trade_number",
    "market",
    "reference_ask",
    "reference_bid",
    "base_amount",
    "entry_quote_actual",
    "exit_quote_actual",
    "buy_fee_quote",
    "sell_fee_quote",
    "total_fees_quote",
    "expected_net_pnl_quote",
    "actual_net_pnl_quote",
    "pnl_difference_quote",
    "dry_run",
}


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


def load_yaml(path: Path) -> Dict[str, Any]:
    if yaml is None or not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def read_rows(path: Path) -> Tuple[List[Dict[str, str]], List[str]]:
    if not path.exists():
        return [], []

    issues: List[str] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = set(reader.fieldnames or [])
            missing = sorted(REQUIRED_COLUMNS - fields)
            if missing:
                issues.append(
                    "CSV_KOLOMMEN_ONTBREKEN:" + ",".join(missing)
                )
            rows = [dict(row) for row in reader]
        return rows, issues
    except Exception as exc:
        return [], [f"CSV_LEESFOUT:{type(exc).__name__}"]


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


def expected_net(
    amount: float,
    reference_ask: float,
    reference_bid: float,
    taker_fee_pct: float,
) -> float:
    fee_rate = max(0.0, taker_fee_pct) / 100.0
    expected_entry = amount * reference_ask
    expected_exit = amount * reference_bid
    return (
        expected_exit
        - (expected_exit * fee_rate)
        - expected_entry
        - (expected_entry * fee_rate)
    )


def fee_rate_pct(fee_quote: float, quote_amount: float) -> float:
    if quote_amount <= 0:
        return 0.0
    return (fee_quote / quote_amount) * 100.0


def fee_rate_status(actual_pct: float, configured_pct: float) -> str:
    extra = actual_pct - configured_pct
    if extra > FEE_HIGH_PP:
        return "HIGH"
    if extra > FEE_WARN_PP:
        return "WARNING"
    return "OK"


def rank(status: str) -> int:
    return {
        "READY": 0,
        "PASS": 0,
        "OK": 0,
        "WARNING": 1,
        "HIGH": 2,
        "FAIL": 3,
    }.get(str(status).upper(), 0)


def worst(*statuses: str) -> str:
    clean = [str(x or "PASS").upper() for x in statuses]
    return max(clean, key=rank) if clean else "PASS"


def validate(path: Path, config_path: Path) -> Dict[str, Any]:
    rows, issues = read_rows(path)
    cfg = load_yaml(config_path)
    taker_fee_pct = to_float(
        cfg_value(cfg, "fees.taker_fee_pct", 0.25),
        0.25,
    )

    result: Dict[str, Any] = {
        "version": VERSION,
        "generated_at": now_iso(),
        "input_file": str(path),
        "input_exists": path.exists(),
        "config_file": str(config_path),
        "configured_taker_fee_pct": taker_fee_pct,
        "status": "READY",
        "closed_trades": 0,
        "issues": issues[:],
        "dry_run_close_rows": 0,
        "logged_total_fees_quote": 0.0,
        "recomputed_total_fees_quote": 0.0,
        "configured_model_fees_quote": 0.0,
        "fee_delta_vs_configured_model_quote": 0.0,
        "max_fee_math_diff_quote": 0.0,
        "max_actual_pnl_math_diff_quote": 0.0,
        "max_expected_pnl_math_diff_quote": 0.0,
        "max_pnl_difference_math_diff_quote": 0.0,
        "avg_effective_buy_fee_pct": 0.0,
        "avg_effective_sell_fee_pct": 0.0,
        "max_fee_above_config_pp": 0.0,
        "execution_cost_difference_quote": 0.0,
        "trades": [],
        "orders_used": False,
        "private_api_used": False,
        "automatic_live_change": False,
    }

    if issues:
        result["status"] = "FAIL"
        return result

    close_rows = [
        row for row in rows
        if str(row.get("event") or "").strip().upper() == "CLOSE"
    ]

    if not close_rows:
        return result

    result["closed_trades"] = len(close_rows)

    buy_rates: List[float] = []
    sell_rates: List[float] = []
    trade_statuses: List[str] = []

    for row in close_rows:
        trade_no = to_int(row.get("canary_trade_number"), 0)
        market = str(row.get("market") or "?")

        amount = to_float(row.get("base_amount"), 0.0)
        ref_ask = to_float(row.get("reference_ask"), 0.0)
        ref_bid = to_float(row.get("reference_bid"), 0.0)

        entry_actual = to_float(row.get("entry_quote_actual"), 0.0)
        exit_actual = to_float(row.get("exit_quote_actual"), 0.0)

        buy_fee = to_float(row.get("buy_fee_quote"), 0.0)
        sell_fee = to_float(row.get("sell_fee_quote"), 0.0)
        total_fee_logged = to_float(row.get("total_fees_quote"), 0.0)
        total_fee_calc = buy_fee + sell_fee

        expected_logged = to_float(row.get("expected_net_pnl_quote"), 0.0)
        actual_logged = to_float(row.get("actual_net_pnl_quote"), 0.0)
        pnl_diff_logged = to_float(row.get("pnl_difference_quote"), 0.0)

        expected_calc = expected_net(
            amount,
            ref_ask,
            ref_bid,
            taker_fee_pct,
        )
        actual_calc = (
            exit_actual
            - sell_fee
            - entry_actual
            - buy_fee
        )
        pnl_diff_calc = actual_logged - expected_logged

        fee_math_diff = abs(total_fee_logged - total_fee_calc)
        actual_pnl_math_diff = abs(actual_logged - actual_calc)
        expected_pnl_math_diff = abs(expected_logged - expected_calc)
        pnl_difference_math_diff = abs(
            pnl_diff_logged - pnl_diff_calc
        )

        buy_rate = fee_rate_pct(buy_fee, entry_actual)
        sell_rate = fee_rate_pct(sell_fee, exit_actual)
        buy_rates.append(buy_rate)
        sell_rates.append(sell_rate)

        buy_fee_status = fee_rate_status(
            buy_rate,
            taker_fee_pct,
        )
        sell_fee_status = fee_rate_status(
            sell_rate,
            taker_fee_pct,
        )

        math_fail = any(
            diff > EUR_TOLERANCE
            for diff in (
                fee_math_diff,
                actual_pnl_math_diff,
                expected_pnl_math_diff,
                pnl_difference_math_diff,
            )
        )

        dry_run = to_bool(row.get("dry_run"), False)
        if dry_run:
            result["dry_run_close_rows"] += 1

        if dry_run or math_fail:
            trade_status = "FAIL"
        else:
            trade_status = worst(
                buy_fee_status,
                sell_fee_status,
            )
            if trade_status == "OK":
                trade_status = "PASS"

        trade_statuses.append(trade_status)

        configured_buy_fee = entry_actual * (taker_fee_pct / 100.0)
        configured_sell_fee = exit_actual * (taker_fee_pct / 100.0)
        configured_model_fee = configured_buy_fee + configured_sell_fee

        result["logged_total_fees_quote"] += total_fee_logged
        result["recomputed_total_fees_quote"] += total_fee_calc
        result["configured_model_fees_quote"] += configured_model_fee
        result["execution_cost_difference_quote"] += (
            actual_logged - expected_logged
        )

        result["max_fee_math_diff_quote"] = max(
            result["max_fee_math_diff_quote"],
            fee_math_diff,
        )
        result["max_actual_pnl_math_diff_quote"] = max(
            result["max_actual_pnl_math_diff_quote"],
            actual_pnl_math_diff,
        )
        result["max_expected_pnl_math_diff_quote"] = max(
            result["max_expected_pnl_math_diff_quote"],
            expected_pnl_math_diff,
        )
        result["max_pnl_difference_math_diff_quote"] = max(
            result["max_pnl_difference_math_diff_quote"],
            pnl_difference_math_diff,
        )

        result["trades"].append(
            {
                "trade_number": trade_no,
                "market": market,
                "status": trade_status,
                "buy_fee_pct": buy_rate,
                "sell_fee_pct": sell_rate,
                "fee_math_diff_quote": fee_math_diff,
                "actual_pnl_math_diff_quote": actual_pnl_math_diff,
                "expected_pnl_math_diff_quote": expected_pnl_math_diff,
                "pnl_difference_math_diff_quote": pnl_difference_math_diff,
                "logged_total_fees_quote": total_fee_logged,
                "configured_model_fees_quote": configured_model_fee,
                "execution_cost_difference_quote": (
                    actual_logged - expected_logged
                ),
                "dry_run": dry_run,
            }
        )

    result["fee_delta_vs_configured_model_quote"] = (
        result["logged_total_fees_quote"]
        - result["configured_model_fees_quote"]
    )

    result["avg_effective_buy_fee_pct"] = (
        sum(buy_rates) / len(buy_rates)
        if buy_rates else 0.0
    )
    result["avg_effective_sell_fee_pct"] = (
        sum(sell_rates) / len(sell_rates)
        if sell_rates else 0.0
    )

    actual_rates = buy_rates + sell_rates
    result["max_fee_above_config_pp"] = max(
        0.0,
        *[
            rate - taker_fee_pct
            for rate in actual_rates
        ],
    )

    if result["dry_run_close_rows"]:
        result["issues"].append(
            f"DRY_RUN_CLOSE_RIJEN:{result['dry_run_close_rows']}"
        )

    overall = worst(*trade_statuses)
    result["status"] = overall if overall != "OK" else "PASS"
    return result


def eur(value: float) -> str:
    sign = "+" if value > 0 else ""
    return f"€{sign}{value:.4f}"


def pct(value: float) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.4f}%"


def print_report(result: Dict[str, Any]) -> None:
    print("=" * 78)
    print(f" DIAMOND FEE / COST VALIDATOR v{VERSION}")
    print("=" * 78)

    if not result["input_exists"]:
        print("STATUS              : READY")
        print("Canary log          : nog niet aanwezig")
        print("Closed live trades  : 0")
        print("Dit is normaal vóór de eerste echte canary-trade.")
        print("Orders/private API  : NEE")
        return

    if result["closed_trades"] == 0:
        print("STATUS              : READY")
        print("Closed live trades  : 0")
        print(
            f"Configured taker fee: "
            f"{result['configured_taker_fee_pct']:.4f}%"
        )
        print("Wacht op de eerste echte canary-close.")
        print("Orders/private API  : NEE")
        return

    print(f"STATUS              : {result['status']}")
    print(f"Closed live trades  : {result['closed_trades']}")
    print(
        f"Configured taker fee: "
        f"{result['configured_taker_fee_pct']:.4f}%"
    )

    print("\n=== FEES ===")
    print(
        f"Logged total fees   : "
        f"{eur(result['logged_total_fees_quote'])}"
    )
    print(
        f"Herberekende fees   : "
        f"{eur(result['recomputed_total_fees_quote'])}"
    )
    print(
        f"Config fee-model    : "
        f"{eur(result['configured_model_fees_quote'])}"
    )
    print(
        f"Fee delta vs config : "
        f"{eur(result['fee_delta_vs_configured_model_quote'])}"
    )
    print(
        f"Gem. BUY fee        : "
        f"{pct(result['avg_effective_buy_fee_pct'])}"
    )
    print(
        f"Gem. SELL fee       : "
        f"{pct(result['avg_effective_sell_fee_pct'])}"
    )
    print(
        f"Max boven config    : "
        f"{result['max_fee_above_config_pp']:+.4f} procentpunt"
    )

    print("\n=== REKENCONTROLE ===")
    print(
        f"Max fee-afwijking   : "
        f"{eur(result['max_fee_math_diff_quote'])}"
    )
    print(
        f"Max actual PnL diff : "
        f"{eur(result['max_actual_pnl_math_diff_quote'])}"
    )
    print(
        f"Max expected diff   : "
        f"{eur(result['max_expected_pnl_math_diff_quote'])}"
    )
    print(
        f"Max PnL-diff check  : "
        f"{eur(result['max_pnl_difference_math_diff_quote'])}"
    )
    print(
        f"Execution verschil  : "
        f"{eur(result['execution_cost_difference_quote'])}"
    )

    if result["issues"]:
        print("\n=== MELDINGEN ===")
        for issue in result["issues"]:
            print(f"- {issue}")

    print("\n=== LAATSTE TRADES ===")
    for trade in result["trades"][-5:]:
        print(
            f"#{trade['trade_number']:>2} "
            f"{trade['market']:<10} "
            f"BUYfee={pct(trade['buy_fee_pct'])} "
            f"SELLfee={pct(trade['sell_fee_pct'])} "
            f"costdiff={eur(trade['execution_cost_difference_quote'])} "
            f"[{trade['status']}]"
        )

    print("\nTolerantie rekenwerk : €0.02")
    print("Fee boven config     : >0.03pp WARN | >0.10pp HIGH")
    print("Orders/private API   : NEE")
    print("Automatische livegang: NEE")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only fee/cost validator voor Diamond Trader."
    )
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--no-write", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = validate(
        Path(args.input),
        Path(args.config),
    )

    if not args.no_write:
        atomic_json(Path(args.output), result)

    print_report(result)

    return 2 if result["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
