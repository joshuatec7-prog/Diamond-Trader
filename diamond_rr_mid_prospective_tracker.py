#!/usr/bin/env python3
"""Diamond Trader RR MID Prospective Shadow Tracker v1.0.

Research-only. Reads already-closed CURRENT shadow trades and reports only new
LONG trend_breakout trades after the fixed baseline that satisfy:
- entry spread <= 0.10%
- 1.35 <= reward_risk < 1.50

No orders, private API, strategy/config/stake or LIVE changes.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

VERSION = "1.0"
TARGET = 20
SPREAD_MAX = 0.10
RR_MIN = 1.35
RR_MAX = 1.50
INTERVAL_SECONDS = 5 * 60

DATA = Path(os.getenv("DIAMOND_DATA_DIR", "/var/data"))
SOURCE = DATA / "diamond_scanner_selective_shadow_trades.csv"
STATE = DATA / "diamond_rr_mid_135_150_shadow_state.json"
REPORT = DATA / "diamond_rr_mid_135_150_shadow_report.json"

SAFETY = {
    "orders": False,
    "private_api": False,
    "network": False,
    "strategy_change": False,
    "filter_change": False,
    "config_change": False,
    "stake_change": False,
    "live_change": False,
    "source_modified": False,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def load_json(path: Path) -> Dict[str, Any]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def atomic_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def initialize_state() -> Dict[str, Any]:
    created = now_iso()
    state = {
        "version": VERSION,
        "created_at": created,
        "baseline_last_detected_at": created,
        "target_closed": TARGET,
        "rule": {
            "name": "RR_MID_135_150",
            "min_rr_inclusive": RR_MIN,
            "max_rr_exclusive": RR_MAX,
            "max_entry_spread_pct": SPREAD_MAX,
        },
        "status": "PROSPECTIVE_SHADOW_ONLY",
        "safety": SAFETY,
    }
    atomic_json(STATE, state)
    return state


def load_rows() -> List[Dict[str, Any]]:
    if not SOURCE.is_file():
        raise FileNotFoundError(SOURCE)

    with SOURCE.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "variant", "candidate_key", "detected_at", "closed_at", "symbol",
            "strategy", "side", "market_regime", "entry_spread_pct",
            "reward_risk", "net_pnl_eur", "total_fees_eur", "exit_reason",
        }
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise RuntimeError("CSV mist kolommen: " + ", ".join(sorted(missing)))

        rows: List[Dict[str, Any]] = []
        for raw in reader:
            if str(raw.get("variant") or "").strip().upper() != "CURRENT":
                continue
            if str(raw.get("side") or "").strip().upper() != "LONG":
                continue
            if str(raw.get("strategy") or "").strip() != "trend_breakout":
                continue
            if f(raw.get("entry_spread_pct"), 999.0) > SPREAD_MAX + 1e-12:
                continue
            rr = f(raw.get("reward_risk"), -1.0)
            if not (RR_MIN <= rr < RR_MAX):
                continue
            if not str(raw.get("closed_at") or "").strip():
                continue

            row = dict(raw)
            row["_detected_dt"] = parse_dt(raw.get("detected_at"))
            row["_closed_dt"] = parse_dt(raw.get("closed_at"))
            row["_pnl"] = f(raw.get("net_pnl_eur"))
            row["_fees"] = f(raw.get("total_fees_eur"))
            rows.append(row)
        return rows


def profit_factor(rows: Iterable[Dict[str, Any]]) -> Optional[float]:
    pnl = [f(row.get("_pnl")) for row in rows]
    gross_profit = sum(x for x in pnl if x > 0)
    gross_loss = abs(sum(x for x in pnl if x < 0))
    if gross_loss > 0:
        return gross_profit / gross_loss
    if gross_profit > 0:
        return math.inf
    return None


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    pnl = [f(row.get("_pnl")) for row in rows]
    wins = sum(x > 0 for x in pnl)
    losses = sum(x < 0 for x in pnl)
    neutral = len(rows) - wins - losses
    pf = profit_factor(rows)
    return {
        "closed": len(rows),
        "wins": wins,
        "losses": losses,
        "neutral": neutral,
        "win_rate": round(wins / len(rows), 4) if rows else None,
        "net_pnl_eur": round(sum(pnl), 4),
        "profit_factor": None if pf is None else ("INF" if math.isinf(pf) else round(pf, 4)),
        "average_trade_eur": round(sum(pnl) / len(rows), 4) if rows else None,
        "total_fees_eur": round(sum(f(row.get("_fees")) for row in rows), 4),
        "target_closed": TARGET,
        "target_reached": len(rows) >= TARGET,
    }


def build_report(rows: List[Dict[str, Any]], state: Dict[str, Any]) -> Dict[str, Any]:
    cutoff = (
        parse_dt(state.get("baseline_last_detected_at"))
        or parse_dt(state.get("baseline_cutoff"))
        or parse_dt(state.get("created_at"))
    )
    if cutoff is None:
        raise RuntimeError("ongeldige RR MID baseline")

    prospective = [
        row for row in rows
        if row.get("_detected_dt") is not None and row["_detected_dt"] > cutoff
    ]
    prospective.sort(key=lambda row: row.get("_closed_dt") or datetime.min.replace(tzinfo=timezone.utc))

    recent = [
        {
            "detected_at": row.get("detected_at"),
            "closed_at": row.get("closed_at"),
            "symbol": row.get("symbol"),
            "reward_risk": round(f(row.get("reward_risk")), 4),
            "spread_pct": round(f(row.get("entry_spread_pct")), 6),
            "exit_reason": row.get("exit_reason"),
            "net_pnl_eur": round(f(row.get("_pnl")), 4),
        }
        for row in prospective[-10:]
    ]

    return {
        "version": VERSION,
        "generated_at": now_iso(),
        "baseline_cutoff": cutoff.isoformat(),
        "rule": "CURRENT LONG trend_breakout + spread <= 0.10% + 1.35 <= RR < 1.50",
        **summarize(prospective),
        "recent_closed": recent,
        "status": "EINDREVIEW_MOGELIJK" if len(prospective) >= TARGET else "DOORLOPEN",
        "safety": SAFETY,
    }


def pf_text(value: Any) -> str:
    if value is None:
        return "n/a"
    if str(value).upper() == "INF":
        return "INF"
    return f"{float(value):.4f}"


def print_report(report: Dict[str, Any]) -> None:
    print("=" * 82)
    print(f" DIAMOND RR MID PROSPECTIVE SHADOW TRACKER v{VERSION}")
    print("=" * 82)
    print(f"Baseline : {report['baseline_cutoff']}")
    print(f"Regel    : {report['rule']}")
    print(
        f"Gesloten : {report['closed']}/{TARGET} | "
        f"W/L/N={report['wins']}/{report['losses']}/{report['neutral']} | "
        f"PnL=€{report['net_pnl_eur']:+.4f} | PF={pf_text(report['profit_factor'])}"
    )
    print(f"Status   : {report['status']}")
    print("LIVE     : ONGEWIJZIGD | Orders/API: NEE")


def run_once(print_output: bool = True) -> int:
    state = load_json(STATE)
    if not (state.get("baseline_last_detected_at") or state.get("baseline_cutoff")):
        state = initialize_state()
    report = build_report(load_rows(), state)
    atomic_json(REPORT, report)
    if print_output:
        print_report(report)
    return 0


def self_test() -> int:
    base = datetime(2026, 8, 25, 18, 45, 25, tzinfo=timezone.utc)
    rows = [
        {"_detected_dt": base, "_closed_dt": base, "_pnl": 5.0, "_fees": 0.65},
        {"_detected_dt": base, "_closed_dt": base, "_pnl": -2.0, "_fees": 0.65},
    ]
    result = summarize(rows)
    assert result["closed"] == 2
    assert result["wins"] == 1 and result["losses"] == 1
    assert result["net_pnl_eur"] == 3.0
    assert abs(float(result["profit_factor"]) - 2.5) < 1e-9
    print("DIAMOND_RR_MID_PROSPECTIVE_SELF_TEST_OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--no-print", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=INTERVAL_SECONDS)
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if not args.loop:
        return run_once(print_output=not args.no_print)

    interval = max(60, int(args.interval_seconds))
    while True:
        try:
            run_once(print_output=not args.no_print)
        except Exception as exc:
            print(f"{now_iso()} | RR_MID_TRACKER_FOUT | {type(exc).__name__}: {exc}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
