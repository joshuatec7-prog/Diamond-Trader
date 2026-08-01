#!/usr/bin/env python3
"""
Diamond Trader Shadow V2 Filter v1.0

Doel:
- beoordeelt uitsluitend NIEUWE virtuele schaduwtrades vanaf een vaste baseline;
- gebruikt de bestaande diamond_shadow_trades.csv als bron;
- selecteert alleen trend_breakout en range_breakout;
- sluit PUMP/EUR en SHIB/EUR uit;
- wijzigt nooit bot-, scanner-, long-, short- of transactiebestanden;
- plaatst nooit orders en maakt geen exchange-verbinding.

Gebruik:
    python3 shadow_v2_filter.py --self-test
    python3 shadow_v2_filter.py --init
    python3 shadow_v2_filter.py
    python3 shadow_v2_filter.py --status

Bestanden:
    /var/data/diamond_shadow_v2_baseline.json
    /var/data/diamond_shadow_v2_report.json
    /var/data/diamond_shadow_v2_trades.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

VERSION = "1.0"
MODE = "READ_ONLY_SHADOW_V2_FILTER"

DATA_DIR = Path(os.getenv("DIAMOND_DATA_DIR", "/var/data"))
SOURCE_FILE = DATA_DIR / "diamond_shadow_trades.csv"
BASELINE_FILE = DATA_DIR / "diamond_shadow_v2_baseline.json"
REPORT_FILE = DATA_DIR / "diamond_shadow_v2_report.json"
TRADES_FILE = DATA_DIR / "diamond_shadow_v2_trades.csv"

TARGET_TRADES = 20
ALLOWED_STRATEGIES = {"trend_breakout", "range_breakout"}
EXCLUDED_SYMBOLS = {"PUMP/EUR", "SHIB/EUR"}

SAFETY = {
    "orders_possible": False,
    "exchange_connection_used": False,
    "bot_state_modified": False,
    "scanner_state_modified": False,
    "source_shadow_file_modified": False,
    "settings_modified": False,
    "automatic_strategy_changes": False,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
    ) as tmp:
        json.dump(data, tmp, indent=2, ensure_ascii=False)
        tmp_name = tmp.name
    os.replace(tmp_name, path)


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} bevat geen JSON-object")
    return data


def read_source() -> List[Dict[str, str]]:
    if not SOURCE_FILE.exists():
        raise FileNotFoundError(f"Bronbestand ontbreekt: {SOURCE_FILE}")

    with SOURCE_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    required = {
        "symbol",
        "strategy",
        "net_pnl_eur",
        "total_fees_eur",
        "opened_at",
        "closed_at",
    }

    if not rows:
        return []

    missing = required - set(rows[0].keys())
    if missing:
        raise ValueError(
            "Bronbestand mist kolommen: " + ", ".join(sorted(missing))
        )

    return rows


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def init_baseline(force: bool = False) -> Dict[str, Any]:
    rows = read_source()

    if BASELINE_FILE.exists() and not force:
        existing = load_json(BASELINE_FILE)
        print("[INFO] Shadow V2-baseline bestaat al; niet overschreven.")
        print(f"       Startbronregels : {existing.get('source_row_count_at_start', '-')}")
        print(f"       Gestart op      : {existing.get('started_at', '-')}")
        return existing

    baseline = {
        "version": VERSION,
        "mode": MODE,
        "started_at": now_iso(),
        "source_file": str(SOURCE_FILE),
        "source_row_count_at_start": len(rows),
        "target_trades": TARGET_TRADES,
        "rules": {
            "allowed_strategies": sorted(ALLOWED_STRATEGIES),
            "excluded_symbols": sorted(EXCLUDED_SYMBOLS),
        },
        "safety": SAFETY,
    }

    save_json_atomic(BASELINE_FILE, baseline)

    print("[OK] Shadow V2-baseline aangemaakt")
    print(f"     Bestaande schaduwtrades uitgesloten : {len(rows)}")
    print(f"     Nieuwe testdoel                    : {TARGET_TRADES}")
    print(f"     Strategieën                        : {', '.join(sorted(ALLOWED_STRATEGIES))}")
    print(f"     Uitgesloten munten                 : {', '.join(sorted(EXCLUDED_SYMBOLS))}")
    return baseline


def select_rows(rows: List[Dict[str, str]], start_index: int):
    new_rows = rows[start_index:]
    accepted = []
    rejected = []

    for row in new_rows:
        symbol = str(row.get("symbol") or "").upper()
        strategy = str(row.get("strategy") or "")

        reasons = []
        if symbol in EXCLUDED_SYMBOLS:
            reasons.append("uitgesloten_munt")
        if strategy not in ALLOWED_STRATEGIES:
            reasons.append("uitgesloten_strategie")

        if reasons:
            rejected.append((row, reasons))
        else:
            accepted.append(row)

    return new_rows, accepted, rejected


def summarize(accepted: List[Dict[str, str]]) -> Dict[str, Any]:
    trades = len(accepted)
    pnls = [to_float(r.get("net_pnl_eur")) for r in accepted]
    fees = [to_float(r.get("total_fees_eur")) for r in accepted]

    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    neutral = trades - wins - losses

    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = sum(p for p in pnls if p < 0)
    net_pnl = sum(pnls)

    profit_factor = (
        gross_profit / abs(gross_loss)
        if gross_loss < 0
        else (float("inf") if gross_profit > 0 else 0.0)
    )

    by_strategy = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "net_pnl_eur": 0.0})
    by_symbol = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "net_pnl_eur": 0.0})

    for row, pnl in zip(accepted, pnls):
        strategy = str(row.get("strategy") or "-")
        symbol = str(row.get("symbol") or "-")

        for bucket, key in ((by_strategy, strategy), (by_symbol, symbol)):
            bucket[key]["trades"] += 1
            bucket[key]["wins"] += int(pnl > 0)
            bucket[key]["losses"] += int(pnl < 0)
            bucket[key]["net_pnl_eur"] += pnl

    def finish_groups(groups):
        result = {}
        for key, value in sorted(groups.items()):
            n = value["trades"]
            result[key] = {
                **value,
                "winrate_pct": round(value["wins"] / n * 100, 2) if n else 0.0,
                "net_pnl_eur": round(value["net_pnl_eur"], 6),
            }
        return result

    avg_win = gross_profit / wins if wins else 0.0
    avg_loss = gross_loss / losses if losses else 0.0

    return {
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "neutral": neutral,
        "winrate_pct": round(wins / trades * 100, 2) if trades else 0.0,
        "net_pnl_eur": round(net_pnl, 6),
        "gross_profit_eur": round(gross_profit, 6),
        "gross_loss_eur": round(gross_loss, 6),
        "profit_factor": round(profit_factor, 4) if math.isfinite(profit_factor) else "inf",
        "average_pnl_eur": round(net_pnl / trades, 6) if trades else 0.0,
        "average_win_eur": round(avg_win, 6),
        "average_loss_eur": round(avg_loss, 6),
        "total_fees_eur": round(sum(fees), 6),
        "by_strategy": finish_groups(by_strategy),
        "by_symbol": finish_groups(by_symbol),
    }


def write_trades(rows: List[Dict[str, str]]) -> None:
    TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not rows:
        with TRADES_FILE.open("w", encoding="utf-8", newline="") as f:
            f.write("")
        return

    fieldnames = list(rows[0].keys())
    with TRADES_FILE.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_report() -> Dict[str, Any]:
    if not BASELINE_FILE.exists():
        raise FileNotFoundError(
            f"Baseline ontbreekt. Start eerst met: python3 {Path(__file__).name} --init"
        )

    baseline = load_json(BASELINE_FILE)
    rows = read_source()

    start_index = int(baseline.get("source_row_count_at_start", 0) or 0)
    if start_index > len(rows):
        raise RuntimeError(
            "Bronbestand bevat minder regels dan bij de baseline; "
            "mogelijk is het bestand vervangen of geroteerd."
        )

    new_rows, accepted, rejected = select_rows(rows, start_index)
    summary = summarize(accepted)

    rejected_by_reason = defaultdict(int)
    for _, reasons in rejected:
        for reason in reasons:
            rejected_by_reason[reason] += 1

    report = {
        "version": VERSION,
        "mode": MODE,
        "generated_at": now_iso(),
        "started_at": baseline.get("started_at"),
        "target_trades": TARGET_TRADES,
        "progress": {
            "accepted_closed_trades": len(accepted),
            "target": TARGET_TRADES,
            "remaining": max(0, TARGET_TRADES - len(accepted)),
            "progress_pct": round(min(1.0, len(accepted) / TARGET_TRADES) * 100, 1),
            "target_reached": len(accepted) >= TARGET_TRADES,
        },
        "rules": {
            "allowed_strategies": sorted(ALLOWED_STRATEGIES),
            "excluded_symbols": sorted(EXCLUDED_SYMBOLS),
        },
        "source": {
            "file": str(SOURCE_FILE),
            "rows_at_start": start_index,
            "rows_now": len(rows),
            "new_closed_shadow_trades": len(new_rows),
            "accepted_v2": len(accepted),
            "rejected_v2": len(rejected),
            "rejected_by_reason": dict(sorted(rejected_by_reason.items())),
        },
        "summary": summary,
        "safety": SAFETY,
        "limitations": [
            "Shadow V2 filtert prospectief de gesloten trades van de bestaande scanner.",
            "Hij opent geen eigen virtuele posities en gebruikt geen exchange-verbinding.",
            "Een signaal dat de bestaande scanner niet opende door zijn eigen positielimiet "
            "kan daardoor niet achteraf als V2-trade worden toegevoegd.",
        ],
    }

    write_trades(accepted)
    save_json_atomic(REPORT_FILE, report)
    return report


def print_report(report: Dict[str, Any]) -> None:
    progress = report["progress"]
    source = report["source"]
    summary = report["summary"]

    print("=" * 68)
    print(" DIAMOND TRADER SHADOW V2")
    print("=" * 68)
    print(f"Modus                 : {report['mode']}")
    print(f"Gestart               : {report.get('started_at') or '-'}")
    print(f"Nieuwe brontrades     : {source['new_closed_shadow_trades']}")
    print(f"V2 geaccepteerd       : {source['accepted_v2']}")
    print(f"V2 afgewezen          : {source['rejected_v2']}")
    print(f"Voortgang             : {progress['accepted_closed_trades']}/{progress['target']} ({progress['progress_pct']:.1f}%)")
    print()
    print("RESULTAAT")
    print("-" * 68)
    print(f"Winst / verlies       : {summary['wins']} / {summary['losses']}")
    print(f"Winrate               : {summary['winrate_pct']:.2f}%")
    print(f"Nettoresultaat        : €{summary['net_pnl_eur']:+.4f}")
    print(f"Profit factor         : {summary['profit_factor']}")
    print(f"Kosten                : €{summary['total_fees_eur']:.4f}")
    print()
    print("REGELS")
    print("-" * 68)
    print(f"Toegestaan            : {', '.join(report['rules']['allowed_strategies'])}")
    print(f"Uitgesloten           : {', '.join(report['rules']['excluded_symbols'])}")
    print()
    print("VEILIGHEID")
    print("-" * 68)
    print("Orders mogelijk       : NEE")
    print("Exchange gebruikt     : NEE")
    print("Bot/scanner gewijzigd : NEE")
    print("=" * 68)


def self_test() -> None:
    sample = [
        {"symbol": "PUMP/EUR", "strategy": "trend_breakout", "net_pnl_eur": "3", "total_fees_eur": "0.5"},
        {"symbol": "ESP/EUR", "strategy": "momentum", "net_pnl_eur": "-2", "total_fees_eur": "0.5"},
        {"symbol": "ESP/EUR", "strategy": "trend_breakout", "net_pnl_eur": "4", "total_fees_eur": "0.5"},
        {"symbol": "VANRY/EUR", "strategy": "range_breakout", "net_pnl_eur": "-1", "total_fees_eur": "0.5"},
    ]

    new_rows, accepted, rejected = select_rows(sample, 0)
    assert len(new_rows) == 4
    assert len(accepted) == 2
    assert len(rejected) == 2

    summary = summarize(accepted)
    assert summary["trades"] == 2
    assert summary["wins"] == 1
    assert summary["losses"] == 1
    assert abs(summary["net_pnl_eur"] - 3.0) < 1e-9

    assert SAFETY["orders_possible"] is False
    assert SAFETY["exchange_connection_used"] is False

    print("SHADOW_V2_SELF_TEST_OK")


def main() -> None:
    parser = argparse.ArgumentParser(description="Diamond Trader Shadow V2 Filter")
    parser.add_argument("--init", action="store_true", help="Maak de prospectieve baseline.")
    parser.add_argument("--force-init", action="store_true", help="Reset de baseline bewust.")
    parser.add_argument("--status", action="store_true", help="Toon de actuele V2-status.")
    parser.add_argument("--self-test", action="store_true", help="Voer interne tests uit.")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return

    if args.init or args.force_init:
        init_baseline(force=args.force_init)
        return

    report = build_report()
    print_report(report)


if __name__ == "__main__":
    main()
