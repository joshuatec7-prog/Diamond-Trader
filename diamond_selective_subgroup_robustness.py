#!/usr/bin/env python3
"""
Diamond Trader SELECTIVE Subgroup Robustness v1.0

Read-only vervolg op Outcome Anatomy.

Doel:
- controleren of winstgevende SELECTIVE-subgroepen overeind blijven
  wanneer hun beste trade wordt verwijderd;
- zwakke/fragiele groepen zichtbaar maken;
- interacties side x strategy, side x regime en side x R/R tonen;
- GEEN filters/strategie/live-instellingen wijzigen.

Bron:
  /var/data/diamond_scanner_selective_shadow_trades.csv

Rapport:
  /var/data/diamond_selective_subgroup_robustness.json
"""

from __future__ import annotations

import csv
import json
import math
import os
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List

VERSION = "1.0"
DATA = Path(os.getenv("DIAMOND_DATA_DIR", "/var/data"))
SOURCE = DATA / "diamond_scanner_selective_shadow_trades.csv"
OUTPUT = DATA / "diamond_selective_subgroup_robustness.json"

MIN_GROUP_N = 4

SAFETY = {
    "orders": False,
    "private_api": False,
    "network": False,
    "config_change": False,
    "strategy_change": False,
    "filter_change": False,
    "stake_change": False,
    "live_change": False,
}


def f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def load_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "variant", "closed_at", "symbol", "strategy", "side",
            "market_regime", "signal_score", "reward_risk",
            "net_pnl_eur", "duration_minutes",
        }
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise RuntimeError(
                "CSV mist kolommen: " + ", ".join(sorted(missing))
            )

        rows = []
        for raw in reader:
            if str(raw.get("variant") or "").strip().upper() != "SELECTIVE":
                continue
            if not str(raw.get("closed_at") or "").strip():
                continue
            row = dict(raw)
            row["side"] = str(raw.get("side") or "UNKNOWN").upper()
            row["strategy"] = str(raw.get("strategy") or "UNKNOWN")
            row["market_regime"] = str(raw.get("market_regime") or "UNKNOWN")
            row["symbol"] = str(raw.get("symbol") or "UNKNOWN")
            row["reward_risk"] = f(raw.get("reward_risk"))
            row["signal_score"] = f(raw.get("signal_score"))
            row["net_pnl_eur"] = f(raw.get("net_pnl_eur"))
            row["duration_minutes"] = f(raw.get("duration_minutes"))
            rows.append(row)

    return rows


def profit_factor(rows: Iterable[Dict[str, Any]]) -> float | None:
    pnl = [f(r.get("net_pnl_eur")) for r in rows]
    gp = sum(x for x in pnl if x > 0)
    gl = abs(sum(x for x in pnl if x < 0))
    if gl > 0:
        return gp / gl
    if gp > 0:
        return math.inf
    return None


def rr_bucket(row: Dict[str, Any]) -> str:
    rr = f(row.get("reward_risk"))
    if rr < 1.2:
        return "<1.20"
    if rr < 1.4:
        return "1.20-1.39"
    if rr < 1.6:
        return "1.40-1.59"
    if rr < 2.0:
        return "1.60-1.99"
    return "2.00+"


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    ordered = sorted(rows, key=lambda r: f(r.get("net_pnl_eur")), reverse=True)
    pnl = [f(r.get("net_pnl_eur")) for r in rows]
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x < 0]
    total = sum(pnl)
    pf = profit_factor(rows)

    best = ordered[0] if ordered else None
    without_best = ordered[1:] if len(ordered) > 1 else []
    wb_pnl = sum(f(r.get("net_pnl_eur")) for r in without_best)
    wb_pf = profit_factor(without_best)

    gross_profit = sum(wins)
    best_share = None
    if best and gross_profit > 0 and f(best.get("net_pnl_eur")) > 0:
        best_share = f(best.get("net_pnl_eur")) / gross_profit

    if len(rows) < MIN_GROUP_N:
        robustness = "SMALL_N"
    elif total <= 0:
        robustness = "WEAK"
    elif wb_pnl <= 0:
        robustness = "FRAGILE_BEST_TRADE"
    elif wb_pf is not None and (math.isinf(wb_pf) or wb_pf >= 1.20):
        robustness = "ROBUST"
    else:
        robustness = "POSITIVE_BUT_THIN"

    return {
        "n": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(rows), 4) if rows else None,
        "pnl_eur": round(total, 4),
        "profit_factor": (
            None if pf is None else math.inf if math.isinf(pf) else round(pf, 4)
        ),
        "best_trade_symbol": best.get("symbol") if best else None,
        "best_trade_pnl_eur": round(f(best.get("net_pnl_eur")), 4) if best else None,
        "best_trade_share_of_gross_profit": (
            round(best_share, 4) if best_share is not None else None
        ),
        "without_best_n": len(without_best),
        "without_best_pnl_eur": round(wb_pnl, 4),
        "without_best_profit_factor": (
            None
            if wb_pf is None
            else math.inf if math.isinf(wb_pf)
            else round(wb_pf, 4)
        ),
        "robustness": robustness,
    }


def grouped(
    rows: List[Dict[str, Any]],
    labeler: Callable[[Dict[str, Any]], str],
) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[labeler(row)].append(row)

    output = []
    for label, subset in groups.items():
        output.append({"group": label, **summarize(subset)})

    rank = {
        "ROBUST": 5,
        "POSITIVE_BUT_THIN": 4,
        "FRAGILE_BEST_TRADE": 3,
        "WEAK": 2,
        "SMALL_N": 1,
    }
    output.sort(
        key=lambda r: (
            rank.get(r["robustness"], 0),
            r["without_best_pnl_eur"],
            r["pnl_eur"],
        ),
        reverse=True,
    )
    return output


def build(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "version": VERSION,
        "source": str(SOURCE),
        "closed_selective_rows": len(rows),
        "overall": summarize(rows),
        "by_side": grouped(rows, lambda r: r["side"]),
        "by_strategy": grouped(rows, lambda r: r["strategy"]),
        "by_regime": grouped(rows, lambda r: r["market_regime"]),
        "by_rr": grouped(rows, rr_bucket),
        "side_x_strategy": grouped(
            rows,
            lambda r: f"{r['side']} | {r['strategy']}",
        ),
        "side_x_regime": grouped(
            rows,
            lambda r: f"{r['side']} | {r['market_regime']}",
        ),
        "side_x_rr": grouped(
            rows,
            lambda r: f"{r['side']} | {rr_bucket(r)}",
        ),
        "minimum_group_n": MIN_GROUP_N,
        "safety": SAFETY,
    }


def atomic_write(path: Path, payload: Dict[str, Any]) -> None:
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
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


def pf_text(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        x = float(value)
    except Exception:
        return "n/a"
    return "INF" if math.isinf(x) else f"{x:.4f}"


def print_section(title: str, rows: List[Dict[str, Any]]) -> None:
    print(f"\n=== {title} ===")
    for row in rows:
        print(
            f"{row['group']:<34} "
            f"n={row['n']:>2} "
            f"W/L={row['wins']}/{row['losses']} "
            f"PnL=€{row['pnl_eur']:+.4f} "
            f"PF={pf_text(row['profit_factor'])} | "
            f"zonder beste=€{row['without_best_pnl_eur']:+.4f} "
            f"PF={pf_text(row['without_best_profit_factor'])} "
            f"[{row['robustness']}]"
        )


def main() -> int:
    try:
        rows = load_rows(SOURCE)
    except Exception as exc:
        print("=" * 96)
        print(f" DIAMOND SELECTIVE SUBGROUP ROBUSTNESS v{VERSION}")
        print("=" * 96)
        print(f"STATUS: BRONFOUT | {type(exc).__name__}: {exc}")
        print("Live/config/orders/private API: NEE")
        return 2

    report = build(rows)
    atomic_write(OUTPUT, report)

    o = report["overall"]
    print("=" * 96)
    print(f" DIAMOND SELECTIVE SUBGROUP ROBUSTNESS v{VERSION}")
    print("=" * 96)
    print(
        f"TOTAAL n={o['n']} W/L={o['wins']}/{o['losses']} "
        f"PnL=€{o['pnl_eur']:+.4f} PF={pf_text(o['profit_factor'])}"
    )
    print(
        f"Zonder beste ({o['best_trade_symbol']} €{o['best_trade_pnl_eur']:+.4f}): "
        f"PnL=€{o['without_best_pnl_eur']:+.4f} "
        f"PF={pf_text(o['without_best_profit_factor'])} "
        f"[{o['robustness']}]"
    )

    print_section("PER SIDE", report["by_side"])
    print_section("PER STRATEGY", report["by_strategy"])
    print_section("PER REGIME", report["by_regime"])
    print_section("PER R/R", report["by_rr"])
    print_section("SIDE x STRATEGY", report["side_x_strategy"])
    print_section("SIDE x REGIME", report["side_x_regime"])
    print_section("SIDE x R/R", report["side_x_rr"])

    print("\n=== VEILIGHEID ===")
    print("Filters gewijzigd   : NEE")
    print("Strategie gewijzigd : NEE")
    print("Stake/config/live   : NEE")
    print("Orders/private API  : NEE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
