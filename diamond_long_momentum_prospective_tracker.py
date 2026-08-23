#!/usr/bin/env python3
"""
Diamond Trader LONG Momentum Prospective Tracker v1.0

Doel:
- Vanaf een vaste baseline alleen NIEUWE afgesloten CURRENT shadow-trades volgen
  die LONG + momentum zijn met entry spread <= 0.10%.
- Geen strategie-, config-, stake- of LIVE-wijzigingen.
- Geen private API en geen orders.
- Bronnen zijn alleen bestaande lokale researchbestanden.

Bron:
  /var/data/diamond_scanner_selective_shadow_trades.csv

State:
  /var/data/diamond_long_momentum_prospective_state.json

Rapport:
  /var/data/diamond_long_momentum_prospective_report.json
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

DATA = Path(os.getenv("DIAMOND_DATA_DIR", "/var/data"))
SOURCE = DATA / "diamond_scanner_selective_shadow_trades.csv"
STATE = DATA / "diamond_long_momentum_prospective_state.json"
REPORT = DATA / "diamond_long_momentum_prospective_report.json"
INTERVAL_SECONDS = 15 * 60

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
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
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
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def atomic_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
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


def load_source(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "variant",
            "candidate_key",
            "detected_at",
            "closed_at",
            "symbol",
            "strategy",
            "side",
            "entry_spread_pct",
            "net_pnl_eur",
            "total_fees_eur",
            "exit_reason",
        }
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise RuntimeError(
                "CSV mist kolommen: " + ", ".join(sorted(missing))
            )

        rows: List[Dict[str, Any]] = []
        for raw in reader:
            if str(raw.get("variant") or "").strip().upper() != "CURRENT":
                continue
            if str(raw.get("side") or "").strip().upper() != "LONG":
                continue
            if str(raw.get("strategy") or "").strip() != "momentum":
                continue
            if f(raw.get("entry_spread_pct"), 999.0) > SPREAD_MAX + 1e-12:
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


def initialize_state() -> Dict[str, Any]:
    created = now_iso()
    state = {
        "version": VERSION,
        "created_at": created,
        "baseline_cutoff": created,
        "target_closed": TARGET,
        "spread_max_pct": SPREAD_MAX,
        "note": (
            "Alleen trades waarvan detected_at op/na baseline_cutoff ligt tellen mee. "
            "Hierdoor tellen ook voor baseline geopende posities later niet alsnog mee."
        ),
        "safety": SAFETY,
    }
    atomic_json(STATE, state)
    return state


def profit_factor(rows: Iterable[Dict[str, Any]]) -> Optional[float]:
    pnl = [f(row.get("_pnl")) for row in rows]
    gp = sum(x for x in pnl if x > 0)
    gl = abs(sum(x for x in pnl if x < 0))
    if gl > 0:
        return gp / gl
    if gp > 0:
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
        "profit_factor": (
            None if pf is None
            else "INF" if math.isinf(pf)
            else round(pf, 4)
        ),
        "average_trade_eur": round(sum(pnl) / len(rows), 4) if rows else None,
        "total_fees_eur": round(sum(f(row.get("_fees")) for row in rows), 4),
        "take_profit": sum(
            str(row.get("exit_reason") or "") == "take_profit" for row in rows
        ),
        "stop_loss": sum(
            str(row.get("exit_reason") or "") == "stop_loss" for row in rows
        ),
        "time_exit": sum(
            str(row.get("exit_reason") or "") == "time_exit" for row in rows
        ),
        "target_closed": TARGET,
        "target_reached": len(rows) >= TARGET,
    }


def build_report(rows: List[Dict[str, Any]], state: Dict[str, Any]) -> Dict[str, Any]:
    cutoff = parse_dt(state.get("baseline_cutoff")) or parse_dt(state.get("created_at"))
    if cutoff is None:
        raise RuntimeError("ongeldige baseline_cutoff")

    prospective = [
        row for row in rows
        if row.get("_detected_dt") is not None
        and row["_detected_dt"] >= cutoff
    ]

    prospective.sort(
        key=lambda row: row.get("_closed_dt")
        or datetime.min.replace(tzinfo=timezone.utc)
    )

    recent = []
    for row in prospective[-5:]:
        recent.append({
            "closed_at": row.get("closed_at"),
            "symbol": row.get("symbol"),
            "spread_pct": round(f(row.get("entry_spread_pct")), 6),
            "reward_risk": round(f(row.get("reward_risk")), 3),
            "exit_reason": row.get("exit_reason"),
            "net_pnl_eur": round(f(row.get("_pnl")), 4),
        })

    return {
        "version": VERSION,
        "generated_at": now_iso(),
        "baseline_cutoff": cutoff.isoformat(),
        "rule": "CURRENT LONG momentum + entry spread <= 0.10%",
        **summarize(prospective),
        "recent_closed": recent,
        "safety": SAFETY,
    }


def pf_text(value: Any) -> str:
    if value is None:
        return "n/a"
    if str(value).upper() == "INF":
        return "INF"
    try:
        return f"{float(value):.4f}"
    except Exception:
        return "n/a"


def print_report(report: Dict[str, Any]) -> None:
    print("=" * 92)
    print(f" DIAMOND LONG MOMENTUM PROSPECTIVE TRACKER v{VERSION}")
    print("=" * 92)
    print(f"Baseline     : {report['baseline_cutoff']}")
    print(f"Regel        : {report['rule']}")
    print(
        f"Gesloten     : {report['closed']}/{TARGET} | "
        f"W/L/N={report['wins']}/{report['losses']}/{report['neutral']} | "
        f"PnL=€{report['net_pnl_eur']:+.4f} | "
        f"PF={pf_text(report['profit_factor'])}"
    )
    if report["closed"]:
        print(
            f"Winrate      : {report['win_rate'] * 100:.1f}% | "
            f"Gem/trade=€{report['average_trade_eur']:+.4f} | "
            f"Fees=€{report['total_fees_eur']:.4f}"
        )
        print(
            f"TP/SL/TIME   : {report['take_profit']}/"
            f"{report['stop_loss']}/{report['time_exit']}"
        )
    print(
        "Status       : "
        + ("EINDREVIEW MOGELIJK" if report["target_reached"] else "DOORLOPEN")
    )
    print("LIVE/config  : ONGEWIJZIGD")
    print("Orders/API   : NEE")


def run_once(print_output: bool = True) -> int:
    state = load_json(STATE)
    if not state.get("baseline_cutoff"):
        state = initialize_state()

    rows = load_source(SOURCE)
    report = build_report(rows, state)
    atomic_json(REPORT, report)

    if print_output:
        print_report(report)
    return 0


def self_test() -> int:
    baseline = datetime(2026, 8, 23, tzinfo=timezone.utc)
    rows = [
        {
            "_detected_dt": baseline,
            "_closed_dt": baseline,
            "_pnl": 2.0,
            "_fees": 0.65,
            "exit_reason": "take_profit",
            "closed_at": baseline.isoformat(),
            "symbol": "AAA/EUR",
            "entry_spread_pct": "0.05",
            "reward_risk": "1.5",
        },
        {
            "_detected_dt": baseline,
            "_closed_dt": baseline,
            "_pnl": -1.0,
            "_fees": 0.65,
            "exit_reason": "stop_loss",
            "closed_at": baseline.isoformat(),
            "symbol": "BBB/EUR",
            "entry_spread_pct": "0.08",
            "reward_risk": "1.4",
        },
    ]
    report = {
        **summarize(rows),
        "baseline_cutoff": baseline.isoformat(),
        "rule": "test",
    }
    assert report["closed"] == 2
    assert report["wins"] == 1
    assert report["losses"] == 1
    assert abs(report["net_pnl_eur"] - 1.0) < 1e-9
    assert abs(float(report["profit_factor"]) - 2.0) < 1e-9
    print("DIAMOND_LONG_MOMENTUM_PROSPECTIVE_SELF_TEST_OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--no-print", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=INTERVAL_SECONDS,
    )
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
            print(
                f"{now_iso()} | LONG_MOMENTUM_TRACKER_FOUT | "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
