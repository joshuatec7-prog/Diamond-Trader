#!/usr/bin/env python3
"""
Diamond Trader - Strategy Comparison Report v1.1

READ-ONLY vergelijkingsrapport voor:
- LONG CURRENT / WAIT_15M / WAIT_30M
- LONG min-profit €1.00 / €0.50 / €0.25
- Scanner CURRENT / SELECTIVE / STRONG
- Paper-SHORT V3 sinds de actuele baseline
- Early Entry "voortekenen" vóór officiële LONG-signalen

Veiligheid:
- geen exchange-calls
- geen API keys
- geen orders
- geen config-wijzigingen
- geen diamond_state wijzigingen
- schrijft alleen eigen rapport onder /var/data
"""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

VERSION = "1.1"
MODE = "READ_ONLY_STRATEGY_COMPARISON"

DATA = Path("/var/data")

LONG_ENTRY_TRADES = DATA / "diamond_long_entry_shadow_trades.csv"
MINPROFIT_TRADES = DATA / "diamond_long_min_profit_shadow_trades.csv"
SELECTIVE_TRADES = DATA / "diamond_scanner_selective_shadow_trades.csv"

SHORT_BASELINE = DATA / "diamond_short_test_baseline.json"
BOT_STATE = DATA / "diamond_state.json"
TRANSACTIONS = DATA / "diamond_transactions.csv"

EARLY_REPORT = DATA / "diamond_early_entry" / "early_entry_long_signal_analysis_v1_0.json"

REPORT_JSON = DATA / "diamond_strategy_comparison_report_v1_1.json"


def load_json(path: Path, default=None):
    if default is None:
        default = {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def rows(path: Path) -> List[Dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def fnum(v: Any) -> float:
    try:
        n = float(v)
        return n if math.isfinite(n) else 0.0
    except Exception:
        return 0.0


def summarize_by_variant(data: List[Dict[str, str]], variant_key="variant", pnl_key="net_pnl_eur"):
    out = {}
    groups = defaultdict(list)

    for r in data:
        variant = str(r.get(variant_key) or "?")
        groups[variant].append(r)

    for variant, rs in groups.items():
        pnls = [fnum(r.get(pnl_key)) for r in rs]
        wins = sum(1 for x in pnls if x > 0)
        losses = sum(1 for x in pnls if x <= 0)
        gross_profit = sum(x for x in pnls if x > 0)
        gross_loss = abs(sum(x for x in pnls if x < 0))
        pf = (gross_profit / gross_loss) if gross_loss > 0 else None

        out[variant] = {
            "closed": len(rs),
            "wins": wins,
            "losses": losses,
            "winrate_pct": (wins / len(rs) * 100.0) if rs else 0.0,
            "net_pnl_eur": sum(pnls),
            "average_pnl_eur": (sum(pnls) / len(rs)) if rs else 0.0,
            "profit_factor": pf,
        }

    return out


def paper_short_v3():
    baseline = load_json(SHORT_BASELINE, {})
    bot = load_json(BOT_STATE, {})

    start_short_trades = int(baseline.get("start_short_trades") or 0)
    target_new = int(baseline.get("target_new_trades") or 20)
    current_short_trades = int(bot.get("short_trades") or 0)
    new_count = max(0, current_short_trades - start_short_trades)

    tx = rows(TRANSACTIONS)
    closes = [
        r for r in tx
        if str(r.get("side") or r.get("action") or "").upper() == "SHORT_CLOSE"
    ]

    # Actuele diamond_transactions.csv gebruikt:
    # ts,market,side,...,net_pnl_quote,...,reason,dry_run
    # Selecteer primair op de V3-starttijd; dit is veiliger dan alleen de
    # laatste N regels nemen.
    started_at = str(baseline.get("started_at") or "")
    if started_at:
        try:
            from datetime import datetime
            start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            filtered = []
            for r in closes:
                ts = str(r.get("ts") or r.get("timestamp") or "")
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except Exception:
                    continue
                if dt >= start_dt:
                    filtered.append(r)
            closes = filtered
        except Exception:
            pass

    # Houd de baseline-telling leidend als extra beveiliging tegen oudere
    # of onverwachte transactieregels.
    test_closes = closes[:new_count] if new_count > 0 else []

    pnls = [
        fnum(
            r.get("net_pnl_quote")
            or r.get("net_pnl_eur")
            or r.get("pnl_quote")
            or r.get("pnl")
        )
        for r in test_closes
    ]
    wins = sum(1 for x in pnls if x > 0)

    return {
        "start_short_trades": start_short_trades,
        "current_short_trades": current_short_trades,
        "new_closed": new_count,
        "target_new": target_new,
        "remaining": max(0, target_new - new_count),
        "wins": wins,
        "losses": max(0, len(pnls) - wins),
        "winrate_pct": (wins / len(pnls) * 100.0) if pnls else 0.0,
        "net_pnl_eur": sum(pnls),
        "average_pnl_eur": (sum(pnls) / len(pnls)) if pnls else 0.0,
    }


def early_entry_precursors():
    rep = load_json(EARLY_REPORT, {})
    counts = rep.get("counts") or {}
    sigs = rep.get("signals") or []

    complete = [s for s in sigs if s.get("complete_snapshots")]

    winners = []
    losers = []

    def metric(s, name):
        return fnum((s.get("derived") or {}).get(name))

    for s in complete:
        pnl = s.get("current_net_pnl_eur")
        if pnl is None:
            continue
        (winners if fnum(pnl) > 0 else losers).append(s)

    metrics = [
        "price_m30_to_m0_pct",
        "price_m15_to_m0_pct",
        "price_m5_to_m0_pct",
        "book_imbalance_m30",
        "book_imbalance_m15",
        "book_imbalance_m5",
        "book_imbalance_m0",
        "trade_imbalance_m30",
        "trade_imbalance_m15",
        "trade_imbalance_m5",
        "trade_imbalance_m0",
        "spread_pct_m0",
    ]

    def averages(group):
        if not group:
            return {}
        result = {}
        for m in metrics:
            vals = [metric(s, m) for s in group]
            result[m] = sum(vals) / len(vals)
        return result

    return {
        "signals_total": int(counts.get("signals_total") or len(sigs)),
        "coverage_possible": int(counts.get("coverage_possible") or 0),
        "complete_snapshots": int(counts.get("complete_snapshots") or 0),
        "waiting_for_new_long_signal": len(complete) == 0,
        "tracked_precursors": [
            "prijsbeweging -30→0 minuten",
            "prijsbeweging -15→0 minuten",
            "prijsbeweging -5→0 minuten",
            "orderboek-imbalance op -30/-15/-5/0",
            "trade-imbalance op -30/-15/-5/0",
            "spread op signaalmoment",
            "1m/5m volume en koers in de onderliggende snapshots",
        ],
        "winner_average_precursors": averages(winners),
        "loser_average_precursors": averages(losers),
        "complete_signal_details": complete,
    }


def write_report():
    long_entry = summarize_by_variant(rows(LONG_ENTRY_TRADES))
    minprofit = summarize_by_variant(rows(MINPROFIT_TRADES))
    selective = summarize_by_variant(rows(SELECTIVE_TRADES))
    short_v3 = paper_short_v3()
    early = early_entry_precursors()

    report = {
        "version": VERSION,
        "mode": MODE,
        "safety": {
            "orders_possible": False,
            "private_exchange_calls": False,
            "api_keys_loaded": False,
            "config_write": False,
            "bot_state_write": False,
        },
        "long_entry": long_entry,
        "long_min_profit": minprofit,
        "scanner_selective": selective,
        "paper_short_v3": short_v3,
        "early_entry_precursors": early,
    }

    with REPORT_JSON.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
        f.write("\n")

    return report


def euro(v):
    return f"€{fnum(v):+.2f}"


def pf_text(v):
    if v is None:
        return "∞"
    return f"{fnum(v):.2f}"


def print_variant_section(title, variants, order):
    print(title)
    if not variants:
        print("geen gegevens")
        print()
        return

    for name in order:
        x = variants.get(name)
        if not x:
            continue
        print(
            f"{name:12} "
            f"{x['closed']:>2} gesloten | "
            f"{x['wins']}W/{x['losses']}L | "
            f"{x['winrate_pct']:.1f}% | "
            f"{euro(x['net_pnl_eur'])} | "
            f"PF {pf_text(x['profit_factor'])}"
        )
    print()


def main():
    report = write_report()

    print("============================================================")
    print(" DIAMOND TRADER - STRATEGY COMPARISON v1.1")
    print("============================================================")
    print()

    print_variant_section(
        "=== LONG ENTRY TIMING ===",
        report["long_entry"],
        ["CURRENT", "WAIT_15M", "WAIT_30M"],
    )

    print_variant_section(
        "=== LONG MIN PROFIT ===",
        report["long_min_profit"],
        ["CURRENT_100", "TEST_050", "TEST_025"],
    )

    print_variant_section(
        "=== SCANNER ===",
        report["scanner_selective"],
        ["CURRENT", "SELECTIVE", "STRONG"],
    )

    s = report["paper_short_v3"]
    print("=== PAPER-SHORT V3 ===")
    print(
        f"{s['new_closed']}/{s['target_new']} gesloten | "
        f"{s['wins']}W/{s['losses']}L | "
        f"{s['winrate_pct']:.1f}% | "
        f"{euro(s['net_pnl_eur'])} | "
        f"gem. {euro(s['average_pnl_eur'])}"
    )
    print(f"Nog te gaan: {s['remaining']}")
    print()

    e = report["early_entry_precursors"]
    print("=== EARLY ENTRY / VOORTEKENEN ===")
    print(f"LONG signalen totaal : {e['signals_total']}")
    print(f"Complete metingen     : {e['complete_snapshots']}")
    if e["waiting_for_new_long_signal"]:
        print("Status                : wacht op eerste nieuwe LONG met volledige Early Entry-data")
    else:
        print("Status                : analyse actief")
        print(f"Win-signalen gemeten  : {len([x for x in e['complete_signal_details'] if fnum(x.get('current_net_pnl_eur')) > 0])}")
        print(f"Loss-signalen gemeten : {len([x for x in e['complete_signal_details'] if x.get('current_net_pnl_eur') is not None and fnum(x.get('current_net_pnl_eur')) <= 0])}")
    print("Bekeken voortekenen   : prijs, orderboek, trades, spread en volume vóór het signaal")
    print()
    print(f"Rapport: {REPORT_JSON}")
    print()
    print("VEILIGHEID: READ-ONLY, GEEN ORDERS, GEEN STRATEGIEWIJZIGINGEN")


if __name__ == "__main__":
    main()
