#!/usr/bin/env python3
"""
Diamond Trader SELECTIVE Outcome Anatomy v1.0

Read-only verdieping na Trade Lifecycle Diagnose.

Doel:
- onderzoeken WAAR SELECTIVE winst/verlies vandaan komt;
- groepen vergelijken op side, strategy, market_regime, R/R en signal_score;
- grootste winnaars/verliezers tonen;
- GEEN strategie-, exit-, stake- of livewijziging uitvoeren.

Bron:
  /var/data/diamond_scanner_selective_shadow_trades.csv

Rapport:
  /var/data/diamond_selective_outcome_anatomy.json
"""

from __future__ import annotations

import csv
import json
import math
import os
import statistics
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List

VERSION = "1.0"
DATA = Path(os.getenv("DIAMOND_DATA_DIR", "/var/data"))
SOURCE = DATA / "diamond_scanner_selective_shadow_trades.csv"
OUTPUT = DATA / "diamond_selective_outcome_anatomy.json"

MIN_GROUP_N = 3

SAFETY = {
    "orders": False,
    "private_api": False,
    "network": False,
    "config_change": False,
    "strategy_change": False,
    "exit_change": False,
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
            "net_pnl_eur", "duration_minutes", "exit_reason",
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
            row["net_pnl_eur"] = f(raw.get("net_pnl_eur"))
            row["duration_minutes"] = max(0.0, f(raw.get("duration_minutes")))
            row["signal_score"] = f(raw.get("signal_score"))
            row["reward_risk"] = f(raw.get("reward_risk"))
            row["side"] = str(raw.get("side") or "UNKNOWN").upper()
            row["strategy"] = str(raw.get("strategy") or "UNKNOWN")
            row["market_regime"] = str(raw.get("market_regime") or "UNKNOWN")
            row["symbol"] = str(raw.get("symbol") or "UNKNOWN")
            row["exit_reason"] = str(raw.get("exit_reason") or "UNKNOWN")
            rows.append(row)

    return rows


def pf(rows: Iterable[Dict[str, Any]]) -> float | None:
    pnl = [f(r.get("net_pnl_eur")) for r in rows]
    gp = sum(x for x in pnl if x > 0)
    gl = abs(sum(x for x in pnl if x < 0))
    if gl > 0:
        return gp / gl
    if gp > 0:
        return math.inf
    return None


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    pnl = [f(r.get("net_pnl_eur")) for r in rows]
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x < 0]
    durations = [f(r.get("duration_minutes")) for r in rows]

    average_win = sum(wins) / len(wins) if wins else None
    average_loss_abs = abs(sum(losses) / len(losses)) if losses else None

    payoff = None
    if average_win is not None and average_loss_abs not in (None, 0):
        payoff = average_win / average_loss_abs

    break_even_wr = None
    if average_win is not None and average_loss_abs not in (None, 0):
        break_even_wr = average_loss_abs / (average_win + average_loss_abs)

    return {
        "n": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(rows), 4) if rows else None,
        "net_pnl_eur": round(sum(pnl), 4),
        "profit_factor": (
            None if pf(rows) is None
            else math.inf if math.isinf(pf(rows))
            else round(pf(rows), 4)
        ),
        "average_trade_eur": round(sum(pnl) / len(rows), 4) if rows else None,
        "average_win_eur": round(average_win, 4) if average_win is not None else None,
        "average_loss_abs_eur": (
            round(average_loss_abs, 4)
            if average_loss_abs is not None else None
        ),
        "payoff_ratio": round(payoff, 4) if payoff is not None else None,
        "break_even_win_rate": (
            round(break_even_wr, 4)
            if break_even_wr is not None else None
        ),
        "average_duration_hours": (
            round(sum(durations) / len(durations) / 60.0, 2)
            if durations else None
        ),
        "median_duration_hours": (
            round(statistics.median(durations) / 60.0, 2)
            if durations else None
        ),
    }


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


def score_bucket(row: Dict[str, Any]) -> str:
    score = f(row.get("signal_score"))
    if score < 90:
        return "<90"
    if score < 95:
        return "90-94.9"
    return "95+"


def duration_bucket(row: Dict[str, Any]) -> str:
    minutes = f(row.get("duration_minutes"))
    if minutes < 60:
        return "<1H"
    if minutes < 240:
        return "1-4H"
    if minutes < 720:
        return "4-12H"
    return "12H+"


def group(
    rows: List[Dict[str, Any]],
    func: Callable[[Dict[str, Any]], str],
) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(func(row))].append(row)

    output = []
    for name, group_rows in groups.items():
        summary = summarize(group_rows)
        output.append({"group": name, **summary})

    output.sort(
        key=lambda r: (
            r["n"] >= MIN_GROUP_N,
            r["net_pnl_eur"],
            r["n"],
        ),
        reverse=True,
    )
    return output


def trade_card(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "symbol": row["symbol"],
        "side": row["side"],
        "strategy": row["strategy"],
        "market_regime": row["market_regime"],
        "signal_score": round(f(row["signal_score"]), 2),
        "reward_risk": round(f(row["reward_risk"]), 3),
        "duration_hours": round(f(row["duration_minutes"]) / 60.0, 2),
        "exit_reason": row["exit_reason"],
        "net_pnl_eur": round(f(row["net_pnl_eur"]), 4),
    }


def build(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    sorted_rows = sorted(
        rows,
        key=lambda r: f(r.get("net_pnl_eur")),
        reverse=True,
    )

    return {
        "version": VERSION,
        "source": str(SOURCE),
        "closed_selective_rows": len(rows),
        "overall": summarize(rows),
        "by_side": group(rows, lambda r: r["side"]),
        "by_strategy": group(rows, lambda r: r["strategy"]),
        "by_market_regime": group(rows, lambda r: r["market_regime"]),
        "by_reward_risk": group(rows, rr_bucket),
        "by_signal_score": group(rows, score_bucket),
        "by_duration": group(rows, duration_bucket),
        "largest_winners": [
            trade_card(r) for r in sorted_rows[:5]
            if f(r.get("net_pnl_eur")) > 0
        ],
        "largest_losses": [
            trade_card(r) for r in sorted_rows[-5:][::-1]
            if f(r.get("net_pnl_eur")) < 0
        ],
        "minimum_group_n_for_interpretation": MIN_GROUP_N,
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


def pct(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{100.0 * f(value):.1f}%"


def print_group(title: str, rows: List[Dict[str, Any]]) -> None:
    print(f"\n=== {title} ===")
    for row in rows:
        mark = "" if row["n"] >= MIN_GROUP_N else " [KLEIN n]"
        print(
            f"{row['group']:<22} "
            f"n={row['n']:>2} "
            f"W/L={row['wins']}/{row['losses']} "
            f"WR={pct(row['win_rate']):>6} "
            f"PnL=€{row['net_pnl_eur']:+.4f} "
            f"PF={pf_text(row['profit_factor'])}"
            f"{mark}"
        )


def main() -> int:
    try:
        rows = load_rows(SOURCE)
    except Exception as exc:
        print("=" * 86)
        print(f" DIAMOND SELECTIVE OUTCOME ANATOMY v{VERSION}")
        print("=" * 86)
        print(f"STATUS: BRONFOUT | {type(exc).__name__}: {exc}")
        print("Live/config/orders/private API: NEE")
        return 2

    report = build(rows)
    atomic_write(OUTPUT, report)

    o = report["overall"]
    print("=" * 86)
    print(f" DIAMOND SELECTIVE OUTCOME ANATOMY v{VERSION}")
    print("=" * 86)
    print(
        f"TOTAAL n={o['n']} W/L={o['wins']}/{o['losses']} "
        f"WR={pct(o['win_rate'])} "
        f"PnL=€{o['net_pnl_eur']:+.4f} "
        f"PF={pf_text(o['profit_factor'])}"
    )
    print(
        f"Gem winst=€{o['average_win_eur']:.4f} | "
        f"gem verlies=€-{o['average_loss_abs_eur']:.4f} | "
        f"payoff={o['payoff_ratio']:.3f}x | "
        f"break-even WR={pct(o['break_even_win_rate'])}"
    )

    print_group("PER SIDE", report["by_side"])
    print_group("PER STRATEGY", report["by_strategy"])
    print_group("PER MARKET REGIME", report["by_market_regime"])
    print_group("PER R/R", report["by_reward_risk"])
    print_group("PER SIGNAL SCORE", report["by_signal_score"])
    print_group("PER DUUR", report["by_duration"])

    print("\n=== 5 GROOTSTE WINNAARS ===")
    for row in report["largest_winners"]:
        print(
            f"{row['symbol']:<10} {row['side']:<5} "
            f"{row['strategy']:<20} "
            f"reg={row['market_regime']:<14} "
            f"RR={row['reward_risk']:.2f} "
            f"score={row['signal_score']:.1f} "
            f"duur={row['duration_hours']:.2f}h "
            f"PnL=€{row['net_pnl_eur']:+.4f}"
        )

    print("\n=== 5 GROOTSTE VERLIEZERS ===")
    for row in report["largest_losses"]:
        print(
            f"{row['symbol']:<10} {row['side']:<5} "
            f"{row['strategy']:<20} "
            f"reg={row['market_regime']:<14} "
            f"RR={row['reward_risk']:.2f} "
            f"score={row['signal_score']:.1f} "
            f"duur={row['duration_hours']:.2f}h "
            f"PnL=€{row['net_pnl_eur']:+.4f}"
        )

    print("\n=== VEILIGHEID ===")
    print("Strategie gewijzigd : NEE")
    print("Exit gewijzigd      : NEE")
    print("Stake gewijzigd     : NEE")
    print("Config/live         : NEE")
    print("Orders/private API  : NEE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
