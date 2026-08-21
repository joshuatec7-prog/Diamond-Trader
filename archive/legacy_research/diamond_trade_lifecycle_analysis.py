#!/usr/bin/env python3
"""
Diamond Trader Trade Lifecycle / Exit Diagnose v1.0

Doel
----
Analyseert UITSLUITEND de bestaande prospectieve SELECTIVE/STRONG shadow-trades uit:
    /var/data/diamond_scanner_selective_shadow_trades.csv

Toont:
- SELECTIVE totaal
- STRONG totaal
- gemiddelde/mediane duur winnaars en verliezers
- SELECTIVE per duur: <1H, 1-4H, 4-12H, 12H+
- SELECTIVE per exitreden
- dezelfde compacte exitreden-samenvatting voor STRONG als extra context

Veiligheid
----------
- geen orders;
- geen private API;
- geen netwerk;
- geen wijziging aan config/state/tradebron;
- schrijft alleen eigen analysetrapport:
  /var/data/diamond_trade_lifecycle_analysis.json
"""

from __future__ import annotations

import csv
import json
import math
import os
import statistics
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List


VERSION = "1.0"
MODE = "READ_ONLY_TRADE_LIFECYCLE_DIAGNOSE"

DATA_DIR = Path(os.getenv("DIAMOND_DATA_DIR", "/var/data"))
TRADES_FILE = DATA_DIR / "diamond_scanner_selective_shadow_trades.csv"
REPORT_FILE = DATA_DIR / "diamond_trade_lifecycle_analysis.json"

VARIANTS = ("SELECTIVE", "STRONG")

SAFETY = {
    "orders_possible": False,
    "private_api": False,
    "network_calls": False,
    "config_modified": False,
    "source_trades_modified": False,
    "automatic_live_changes": False,
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


def safe_pf(trades: Iterable[Dict[str, Any]]) -> float | None:
    pnl = [to_float(row.get("net_pnl_eur")) for row in trades]
    gross_profit = sum(x for x in pnl if x > 0)
    gross_loss = sum(x for x in pnl if x < 0)
    if gross_loss < 0:
        return gross_profit / abs(gross_loss)
    if gross_profit > 0:
        return math.inf
    return None


def avg(values: List[float]) -> float | None:
    return sum(values) / len(values) if values else None


def median(values: List[float]) -> float | None:
    return statistics.median(values) if values else None


def round_or_none(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    if math.isinf(value):
        return value
    return round(value, digits)


def load_trades(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(str(path))

    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "variant",
            "closed_at",
            "exit_reason",
            "net_pnl_eur",
            "duration_minutes",
        }
        fields = set(reader.fieldnames or [])
        missing = sorted(required - fields)
        if missing:
            raise RuntimeError(
                "CSV mist vereiste kolommen: " + ", ".join(missing)
            )

        for raw in reader:
            variant = str(raw.get("variant") or "").strip().upper()
            if variant not in VARIANTS:
                continue

            # Alleen daadwerkelijk gesloten trades.
            if not str(raw.get("closed_at") or "").strip():
                continue

            row = dict(raw)
            row["variant"] = variant
            row["net_pnl_eur"] = to_float(raw.get("net_pnl_eur"))
            row["duration_minutes"] = max(
                0.0,
                to_float(raw.get("duration_minutes")),
            )
            row["exit_reason"] = (
                str(raw.get("exit_reason") or "onbekend").strip()
                or "onbekend"
            )
            rows.append(row)

    return rows


def summarize(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    pnl = [to_float(row.get("net_pnl_eur")) for row in trades]
    wins = [row for row in trades if to_float(row.get("net_pnl_eur")) > 0]
    losses = [row for row in trades if to_float(row.get("net_pnl_eur")) < 0]
    neutral = [row for row in trades if to_float(row.get("net_pnl_eur")) == 0]

    win_durations = [
        to_float(row.get("duration_minutes"))
        for row in wins
    ]
    loss_durations = [
        to_float(row.get("duration_minutes"))
        for row in losses
    ]
    all_durations = [
        to_float(row.get("duration_minutes"))
        for row in trades
    ]

    pf = safe_pf(trades)

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "neutral": len(neutral),
        "net_pnl_eur": round(sum(pnl), 4),
        "profit_factor": round_or_none(pf, 4),
        "average_pnl_eur": round(
            sum(pnl) / len(trades), 4
        ) if trades else None,
        "average_duration_minutes": round_or_none(avg(all_durations), 2),
        "median_duration_minutes": round_or_none(median(all_durations), 2),
        "winner_average_duration_minutes": round_or_none(avg(win_durations), 2),
        "winner_median_duration_minutes": round_or_none(median(win_durations), 2),
        "loser_average_duration_minutes": round_or_none(avg(loss_durations), 2),
        "loser_median_duration_minutes": round_or_none(median(loss_durations), 2),
    }


def duration_bucket(minutes: float) -> str:
    if minutes < 60:
        return "<1H"
    if minutes < 240:
        return "1-4H"
    if minutes < 720:
        return "4-12H"
    return "12H+"


def grouped_summary(
    trades: List[Dict[str, Any]],
    key_func,
    *,
    order: List[str] | None = None,
) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in trades:
        groups[str(key_func(row))].append(row)

    keys = list(groups)
    if order is not None:
        known = [key for key in order if key in groups]
        rest = sorted(key for key in keys if key not in set(order))
        keys = known + rest
    else:
        keys = sorted(keys)

    output = []
    for key in keys:
        summary = summarize(groups[key])
        output.append({
            "group": key,
            **summary,
        })
    return output


def build_report(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_variant = {
        variant: [
            row for row in trades
            if row["variant"] == variant
        ]
        for variant in VARIANTS
    }

    variants = {
        variant: summarize(by_variant[variant])
        for variant in VARIANTS
    }

    selective = by_variant["SELECTIVE"]
    strong = by_variant["STRONG"]

    return {
        "version": VERSION,
        "mode": MODE,
        "generated_at": now_iso(),
        "source_file": str(TRADES_FILE),
        "source_closed_rows_used": len(trades),
        "variants": variants,
        "selective_duration_buckets": grouped_summary(
            selective,
            lambda row: duration_bucket(
                to_float(row.get("duration_minutes"))
            ),
            order=["<1H", "1-4H", "4-12H", "12H+"],
        ),
        "selective_exit_reasons": grouped_summary(
            selective,
            lambda row: str(row.get("exit_reason") or "onbekend"),
        ),
        "strong_exit_reasons": grouped_summary(
            strong,
            lambda row: str(row.get("exit_reason") or "onbekend"),
        ),
        "safety": SAFETY,
    }


def atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
                allow_nan=True,
            )
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
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if math.isinf(number):
        return "INF"
    return f"{number:.4f}"


def min_to_h(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{to_float(value) / 60.0:.2f}h"


def print_total(label: str, summary: Dict[str, Any]) -> None:
    print(
        f"{label:<10} "
        f"n={summary['trades']:>2} "
        f"W/L/BE={summary['wins']}/{summary['losses']}/{summary['neutral']} "
        f"PnL=€{summary['net_pnl_eur']:+.4f} "
        f"PF={pf_text(summary['profit_factor'])}"
    )
    print(
        f"  duur alles   avg={min_to_h(summary['average_duration_minutes'])} "
        f"mediaan={min_to_h(summary['median_duration_minutes'])}"
    )
    print(
        f"  winnaars     avg={min_to_h(summary['winner_average_duration_minutes'])} "
        f"mediaan={min_to_h(summary['winner_median_duration_minutes'])}"
    )
    print(
        f"  verliezers   avg={min_to_h(summary['loser_average_duration_minutes'])} "
        f"mediaan={min_to_h(summary['loser_median_duration_minutes'])}"
    )


def print_group(row: Dict[str, Any]) -> None:
    print(
        f"{row['group']:<22} "
        f"n={row['trades']:>2} "
        f"W/L/BE={row['wins']}/{row['losses']}/{row['neutral']} "
        f"PnL=€{row['net_pnl_eur']:+.4f} "
        f"PF={pf_text(row['profit_factor'])} "
        f"avgduur={min_to_h(row['average_duration_minutes'])}"
    )


def print_report(report: Dict[str, Any]) -> None:
    print("=" * 86)
    print(f" DIAMOND TRADE LIFECYCLE / EXIT DIAGNOSE v{VERSION}")
    print("=" * 86)
    print(f"Bron: {report['source_file']}")
    print(f"Gesloten SELECTIVE/STRONG rijen gebruikt: {report['source_closed_rows_used']}")

    print("\n=== TOTAAL ===")
    for variant in VARIANTS:
        print_total(variant, report["variants"][variant])

    print("\n=== SELECTIVE PER DUUR ===")
    rows = report["selective_duration_buckets"]
    if not rows:
        print("Geen gesloten SELECTIVE-trades.")
    else:
        for row in rows:
            print_group(row)

    print("\n=== SELECTIVE PER EXITREDEN ===")
    rows = report["selective_exit_reasons"]
    if not rows:
        print("Geen gesloten SELECTIVE-trades.")
    else:
        for row in rows:
            print_group(row)

    print("\n=== STRONG PER EXITREDEN ===")
    rows = report["strong_exit_reasons"]
    if not rows:
        print("Geen gesloten STRONG-trades.")
    else:
        for row in rows:
            print_group(row)

    print("\n=== VEILIGHEID ===")
    print("Orders              : NEE")
    print("Private API         : NEE")
    print("Netwerk             : NEE")
    print("Bron-CSV gewijzigd  : NEE")
    print("Config/state gewijzigd: NEE")
    print("Live wijziging      : NEE")


def main() -> int:
    try:
        trades = load_trades(TRADES_FILE)
    except FileNotFoundError:
        print("=" * 86)
        print(f" DIAMOND TRADE LIFECYCLE / EXIT DIAGNOSE v{VERSION}")
        print("=" * 86)
        print(f"STATUS: BRON ONTBREEKT")
        print(str(TRADES_FILE))
        print("Orders/private API/live wijziging: NEE")
        return 2
    except Exception as exc:
        print("=" * 86)
        print(f" DIAMOND TRADE LIFECYCLE / EXIT DIAGNOSE v{VERSION}")
        print("=" * 86)
        print(f"STATUS: BRONFOUT | {type(exc).__name__}: {exc}")
        print("Orders/private API/live wijziging: NEE")
        return 3

    report = build_report(trades)
    atomic_json(REPORT_FILE, report)
    print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
