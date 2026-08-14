#!/usr/bin/env python3
# Diamond Trader Canary Log Analyzer v1.0
#
# Alleen-lezen analyse van de live canary execution-log.
# Plaatst geen orders en gebruikt geen private exchange-API.

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


VERSION = "1.0"
DEFAULT_INPUT = Path("/var/data/diamond_canary_execution.csv")
DEFAULT_JSON = Path("/var/data/diamond_canary_log_analysis.json")

STATUS_RANK = {
    "OK": 0,
    "WARNING": 1,
    "HIGH": 2,
    "STOP_CANDIDATE": 3,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        result = float(value)
        if not math.isfinite(result):
            return default
        return result
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


def classify_slippage(value: float) -> str:
    # Negatieve slippage = betere fill en dus OK.
    if value > 0.30:
        return "STOP_CANDIDATE"
    if value > 0.20:
        return "HIGH"
    if value > 0.10:
        return "WARNING"
    return "OK"


def normalize_status(value: Any, fallback_slippage: float = 0.0) -> str:
    status = str(value or "").strip().upper()
    if status in STATUS_RANK:
        return status
    return classify_slippage(fallback_slippage)


def worst_status(*statuses: str) -> str:
    clean = [
        str(status or "OK").strip().upper()
        for status in statuses
    ]
    if not clean:
        return "OK"
    return max(clean, key=lambda x: STATUS_RANK.get(x, 0))


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
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


def format_eur(value: float) -> str:
    sign = "+" if value > 0 else ""
    return f"€{sign}{value:.4f}"


def format_pct(value: float) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.4f}%"


def profit_factor(pnls: Iterable[float]) -> Optional[float]:
    gross_profit = sum(x for x in pnls if x > 0)
    gross_loss = abs(sum(x for x in pnls if x < 0))
    if gross_loss <= 0:
        if gross_profit > 0:
            return math.inf
        return None
    return gross_profit / gross_loss


def pf_text(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    if math.isinf(value):
        return "INF"
    return f"{value:.4f}"


def read_rows(path: Path) -> Tuple[List[Dict[str, str]], List[str]]:
    issues: List[str] = []
    if not path.exists():
        return [], issues

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                return [], ["CSV_HEADER_ONTBREEKT"]
            rows = [dict(row) for row in reader]
    except Exception as exc:
        return [], [f"CSV_LEESFOUT:{type(exc).__name__}"]

    required = {
        "event",
        "canary_trade_number",
        "market",
        "side",
        "actual_net_pnl_quote",
        "buy_slippage_pct",
        "sell_slippage_pct",
        "recovery_used",
        "dry_run",
    }
    missing = sorted(required - set(reader.fieldnames or []))
    if missing:
        issues.append("CSV_KOLOMMEN_ONTBREKEN:" + ",".join(missing))

    return rows, issues


def analyze(path: Path) -> Dict[str, Any]:
    rows, issues = read_rows(path)

    result: Dict[str, Any] = {
        "version": VERSION,
        "generated_at": now_iso(),
        "input_file": str(path),
        "input_exists": path.exists(),
        "row_count": len(rows),
        "issues": issues[:],
        "status": "READY" if not rows else "OK",
        "opened_trades": 0,
        "closed_trades": 0,
        "incomplete_trades": 0,
        "duplicate_close_trades": [],
        "trade_number_gaps": [],
        "wins": 0,
        "losses": 0,
        "breakeven": 0,
        "actual_net_pnl_quote": 0.0,
        "expected_net_pnl_quote": 0.0,
        "pnl_difference_quote": 0.0,
        "profit_factor": None,
        "total_fees_quote": 0.0,
        "avg_buy_slippage_pct": 0.0,
        "avg_sell_slippage_pct": 0.0,
        "max_adverse_slippage_pct": 0.0,
        "recovery_events": 0,
        "status_counts": {},
        "missing_order_id_events": 0,
        "dry_run_rows": 0,
        "trades": [],
    }

    if not rows:
        return result

    grouped: Dict[int, List[Dict[str, str]]] = defaultdict(list)
    unknown_trade_rows = 0
    for row in rows:
        trade_no = to_int(row.get("canary_trade_number"), 0)
        if trade_no <= 0:
            unknown_trade_rows += 1
            continue
        grouped[trade_no].append(row)

    if unknown_trade_rows:
        result["issues"].append(
            f"ONGELDIG_CANARY_TRADENUMMER:{unknown_trade_rows}"
        )

    trade_numbers = sorted(grouped)
    if trade_numbers:
        expected_numbers = set(
            range(min(trade_numbers), max(trade_numbers) + 1)
        )
        result["trade_number_gaps"] = sorted(
            expected_numbers - set(trade_numbers)
        )
        if result["trade_number_gaps"]:
            result["issues"].append(
                "TRADE_NUMMER_GATEN:"
                + ",".join(map(str, result["trade_number_gaps"]))
            )

    closed_trade_rows: List[Dict[str, str]] = []
    trades: List[Dict[str, Any]] = []

    for trade_no in trade_numbers:
        trade_rows = grouped[trade_no]
        opens = [
            row for row in trade_rows
            if str(row.get("event") or "").strip().upper() == "OPEN"
        ]
        closes = [
            row for row in trade_rows
            if str(row.get("event") or "").strip().upper() == "CLOSE"
        ]

        if opens:
            result["opened_trades"] += 1

        if len(closes) > 1:
            result["duplicate_close_trades"].append(trade_no)
            result["issues"].append(
                f"DUPLICATE_CLOSE_TRADE:{trade_no}"
            )

        close = closes[-1] if closes else None
        open_row = opens[-1] if opens else None

        if close is None:
            result["incomplete_trades"] += 1
            market = (
                (open_row or {}).get("market")
                or (trade_rows[-1] if trade_rows else {}).get("market")
                or "?"
            )
            trades.append(
                {
                    "trade_number": trade_no,
                    "market": market,
                    "state": "OPEN/INCOMPLETE",
                }
            )
            continue

        result["closed_trades"] += 1
        closed_trade_rows.append(close)

        buy_slip = to_float(
            close.get("buy_slippage_pct"),
            to_float((open_row or {}).get("buy_slippage_pct"), 0.0),
        )
        sell_slip = to_float(close.get("sell_slippage_pct"), 0.0)

        buy_status = normalize_status(
            close.get("buy_slippage_status")
            or (open_row or {}).get("buy_slippage_status"),
            buy_slip,
        )
        sell_status = normalize_status(
            close.get("sell_slippage_status"),
            sell_slip,
        )
        overall = normalize_status(
            close.get("overall_status"),
            max(buy_slip, sell_slip),
        )
        overall = worst_status(overall, buy_status, sell_status)

        actual_pnl = to_float(close.get("actual_net_pnl_quote"), 0.0)
        expected_pnl = to_float(close.get("expected_net_pnl_quote"), 0.0)
        pnl_diff = to_float(
            close.get("pnl_difference_quote"),
            actual_pnl - expected_pnl,
        )
        fees = to_float(
            close.get("total_fees_quote"),
            to_float(close.get("buy_fee_quote"), 0.0)
            + to_float(close.get("sell_fee_quote"), 0.0),
        )

        recovery_used = (
            to_bool(close.get("recovery_used"), False)
            or to_bool((open_row or {}).get("recovery_used"), False)
        )

        if recovery_used:
            result["recovery_events"] += 1

        if to_bool(close.get("dry_run"), False):
            result["dry_run_rows"] += 1

        buy_order_id = str(
            close.get("buy_order_id")
            or (open_row or {}).get("buy_order_id")
            or ""
        ).strip()
        sell_order_id = str(close.get("sell_order_id") or "").strip()

        missing_ids = int(not buy_order_id) + int(not sell_order_id)
        result["missing_order_id_events"] += missing_ids

        trades.append(
            {
                "trade_number": trade_no,
                "market": close.get("market") or "?",
                "state": "CLOSED",
                "actual_pnl": actual_pnl,
                "expected_pnl": expected_pnl,
                "pnl_difference": pnl_diff,
                "fees": fees,
                "buy_slippage_pct": buy_slip,
                "sell_slippage_pct": sell_slip,
                "buy_status": buy_status,
                "sell_status": sell_status,
                "overall_status": overall,
                "holding_time_min": to_float(
                    close.get("holding_time_min"), 0.0
                ),
                "recovery_used": recovery_used,
                "reason": close.get("reason") or "",
            }
        )

    closed = [t for t in trades if t.get("state") == "CLOSED"]
    pnls = [to_float(t.get("actual_pnl"), 0.0) for t in closed]

    result["wins"] = sum(1 for pnl in pnls if pnl > 1e-12)
    result["losses"] = sum(1 for pnl in pnls if pnl < -1e-12)
    result["breakeven"] = len(pnls) - result["wins"] - result["losses"]
    result["actual_net_pnl_quote"] = sum(pnls)
    result["expected_net_pnl_quote"] = sum(
        to_float(t.get("expected_pnl"), 0.0)
        for t in closed
    )
    result["pnl_difference_quote"] = sum(
        to_float(t.get("pnl_difference"), 0.0)
        for t in closed
    )
    result["total_fees_quote"] = sum(
        to_float(t.get("fees"), 0.0)
        for t in closed
    )
    result["profit_factor"] = profit_factor(pnls)

    if closed:
        result["avg_buy_slippage_pct"] = (
            sum(to_float(t.get("buy_slippage_pct"), 0.0) for t in closed)
            / len(closed)
        )
        result["avg_sell_slippage_pct"] = (
            sum(to_float(t.get("sell_slippage_pct"), 0.0) for t in closed)
            / len(closed)
        )
        result["max_adverse_slippage_pct"] = max(
            0.0,
            *[
                max(
                    to_float(t.get("buy_slippage_pct"), 0.0),
                    to_float(t.get("sell_slippage_pct"), 0.0),
                )
                for t in closed
            ],
        )

    counts = Counter(
        str(t.get("overall_status") or "OK")
        for t in closed
    )
    result["status_counts"] = dict(sorted(counts.items()))

    if result["dry_run_rows"]:
        result["issues"].append(
            f"DRY_RUN_RIJEN_IN_LIVE_LOG:{result['dry_run_rows']}"
        )

    if result["missing_order_id_events"]:
        result["issues"].append(
            f"ONTBREKENDE_ORDER_IDS:{result['missing_order_id_events']}"
        )

    result["trades"] = trades

    if result["duplicate_close_trades"] or result["dry_run_rows"]:
        result["status"] = "FAIL"
    elif result["issues"]:
        result["status"] = "WARNING"
    elif result["closed_trades"] > 0:
        worst = worst_status(
            *[
                str(t.get("overall_status") or "OK")
                for t in closed
            ]
        )
        result["status"] = worst
    else:
        result["status"] = "READY"

    return result


def print_report(result: Dict[str, Any], last_n: int = 5) -> None:
    print("=" * 68)
    print(f" DIAMOND CANARY LOG ANALYZER v{VERSION}")
    print("=" * 68)

    if not result["input_exists"]:
        print("[READY] Canary execution-log bestaat nog niet.")
        print("        Dit is normaal vóór de eerste echte canary-trade.")
        print("Closed live trades : 0")
        print("Orders/private API : NEE")
        return

    if result["row_count"] == 0:
        print("[READY] Canary execution-log is aanwezig maar nog leeg.")
        print("Closed live trades : 0")
        print("Orders/private API : NEE")
        return

    print(
        f"STATUS             : {result['status']}"
    )
    print(
        f"Trades             : closed={result['closed_trades']} "
        f"open/incomplete={result['incomplete_trades']}"
    )
    print(
        f"W/L/BE             : "
        f"{result['wins']}/{result['losses']}/{result['breakeven']}"
    )
    print(
        f"Actual netto PnL   : "
        f"{format_eur(result['actual_net_pnl_quote'])}"
    )
    print(
        f"Expected netto PnL : "
        f"{format_eur(result['expected_net_pnl_quote'])}"
    )
    print(
        f"Execution verschil : "
        f"{format_eur(result['pnl_difference_quote'])}"
    )
    print(
        f"Profit Factor      : "
        f"{pf_text(result['profit_factor'])}"
    )
    print(
        f"Totale fees        : "
        f"{format_eur(result['total_fees_quote'])}"
    )
    print(
        f"Gem. BUY slippage  : "
        f"{format_pct(result['avg_buy_slippage_pct'])}"
    )
    print(
        f"Gem. SELL slippage : "
        f"{format_pct(result['avg_sell_slippage_pct'])}"
    )
    print(
        f"Max slippage       : "
        f"{format_pct(result['max_adverse_slippage_pct'])}"
    )
    print(
        f"Recovery gebruikt  : {result['recovery_events']}"
    )

    counts = result.get("status_counts") or {}
    print(
        "Execution status   : "
        + " ".join(
            f"{name}={counts.get(name, 0)}"
            for name in (
                "OK",
                "WARNING",
                "HIGH",
                "STOP_CANDIDATE",
            )
        )
    )

    if result["issues"]:
        print("\n=== DATA / SAFETY MELDINGEN ===")
        for issue in result["issues"]:
            print(f"- {issue}")

    closed = [
        trade for trade in result["trades"]
        if trade.get("state") == "CLOSED"
    ]
    if closed:
        print(f"\n=== LAATSTE {min(last_n, len(closed))} GESLOTEN TRADES ===")
        for trade in closed[-last_n:]:
            print(
                f"#{trade['trade_number']:>2} "
                f"{trade['market']:<10} "
                f"PnL={format_eur(trade['actual_pnl'])} "
                f"exp={format_eur(trade['expected_pnl'])} "
                f"BUY={format_pct(trade['buy_slippage_pct'])} "
                f"SELL={format_pct(trade['sell_slippage_pct'])} "
                f"[{trade['overall_status']}]"
            )

    if result["incomplete_trades"]:
        open_trades = [
            trade for trade in result["trades"]
            if trade.get("state") != "CLOSED"
        ]
        print("\n=== OPEN / INCOMPLETE ===")
        for trade in open_trades:
            print(
                f"#{trade['trade_number']:>2} "
                f"{trade['market']} | {trade['state']}"
            )

    print("\nOrders/private API : NEE")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyseer Diamond Trader canary execution-log."
    )
    parser.add_argument(
        "--input",
        default=str(DEFAULT_INPUT),
        help=f"CSV-pad (standaard: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--json-output",
        default=str(DEFAULT_JSON),
        help=f"JSON-rapport (standaard: {DEFAULT_JSON})",
    )
    parser.add_argument(
        "--no-json",
        action="store_true",
        help="Schrijf geen JSON-rapport.",
    )
    parser.add_argument(
        "--last",
        type=int,
        default=5,
        help="Aantal laatste gesloten trades tonen.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    result = analyze(input_path)

    if not args.no_json:
        atomic_write_json(
            Path(args.json_output),
            result,
        )

    print_report(
        result,
        last_n=max(1, args.last),
    )

    # FAIL betekent dat de log zelf een veiligheids-/integriteitsprobleem bevat.
    return 2 if result["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
