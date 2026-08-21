#!/usr/bin/env python3
"""
Diamond Trader - Time / Session Analysis v1.0

Doel
----
Read-only analyse van bestaande gesloten trades op:
- UTC-uur van de entry
- weekdag
- weekdag versus weekend
- vaste UTC-tijdblokken van 6 uur

Bronnen
-------
1. /var/data/diamond_transactions.csv
   - echte/dry-run LONG en paper-SHORT transacties
   - entrytijd wordt bij CLOSE-regels afgeleid uit hold_minutes
2. /var/data/diamond_long_entry_shadow_trades.csv
   - LONG Entry Timing Shadow: CURRENT / WAIT_15M / WAIT_30M
3. /var/data/diamond_scanner_selective_shadow_trades.csv
   - Scanner CURRENT / SELECTIVE / STRONG

Veiligheid
----------
- geen exchange-calls
- geen API keys
- geen orders
- geen config-wijzigingen
- geen bot-state wijzigingen
- schrijft uitsluitend eigen rapport:
  /var/data/diamond_time_session_analysis_v1_0.json

Opmerking
---------
Dit rapport verandert niets aan strategie of filters. Kleine steekproeven worden
expliciet gemarkeerd en mogen niet als bewijs voor live-aanpassingen worden gezien.
"""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

VERSION = "1.0"
MODE = "READ_ONLY_TIME_SESSION_ANALYSIS"

DATA_DIR = Path("/var/data")
TRANSACTIONS = DATA_DIR / "diamond_transactions.csv"
LONG_SHADOW = DATA_DIR / "diamond_long_entry_shadow_trades.csv"
SCANNER_SHADOW = DATA_DIR / "diamond_scanner_selective_shadow_trades.csv"
REPORT_JSON = DATA_DIR / "diamond_time_session_analysis_v1_0.json"

MIN_SAMPLE_INTERESTING = 3
MIN_SAMPLE_STRONG = 5

WEEKDAY_NAMES = ["ma", "di", "wo", "do", "vr", "za", "zo"]


def safe_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
        if not math.isfinite(number):
            return None
        return number
    except Exception:
        return None


def parse_dt(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except Exception:
        return []


def get_first(row: Dict[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def time_block(hour: int) -> str:
    if 0 <= hour <= 5:
        return "00-05 UTC"
    if 6 <= hour <= 11:
        return "06-11 UTC"
    if 12 <= hour <= 17:
        return "12-17 UTC"
    return "18-23 UTC"


def sample_label(count: int) -> str:
    if count >= MIN_SAMPLE_STRONG:
        return "bruikbaar"
    if count >= MIN_SAMPLE_INTERESTING:
        return "voorlopig"
    return "te klein"


def summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    pnls = [float(x["pnl_eur"]) for x in rows]
    count = len(pnls)
    wins = sum(1 for x in pnls if x > 0)
    losses = sum(1 for x in pnls if x <= 0)
    gross_profit = sum(x for x in pnls if x > 0)
    gross_loss = abs(sum(x for x in pnls if x < 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else None

    return {
        "trades": count,
        "wins": wins,
        "losses": losses,
        "winrate_pct": (wins / count * 100.0) if count else 0.0,
        "net_pnl_eur": sum(pnls),
        "average_pnl_eur": (sum(pnls) / count) if count else 0.0,
        "profit_factor": pf,
        "sample_quality": sample_label(count),
    }


def make_trade(
    source: str,
    variant: str,
    symbol: str,
    entry_at: datetime,
    pnl_eur: float,
    detail: str = "",
) -> Dict[str, Any]:
    return {
        "source": source,
        "variant": variant,
        "symbol": symbol,
        "entry_at": entry_at,
        "pnl_eur": pnl_eur,
        "detail": detail,
    }


def load_bot_trades() -> List[Dict[str, Any]]:
    """
    diamond_transactions.csv actuele layout:
    ts,market,side,price,amount,quote_value,fee_quote,spread_pct,
    net_pnl_quote,hold_minutes,reason,dry_run

    Voor SELL/SHORT_CLOSE leiden we de entrytijd af:
    entry_at = close_ts - hold_minutes.
    """
    result: List[Dict[str, Any]] = []

    for row in read_csv(TRANSACTIONS):
        side = str(get_first(row, ("side", "action")) or "").upper()
        if side not in ("SELL", "SHORT_CLOSE"):
            continue

        close_at = parse_dt(get_first(row, ("ts", "timestamp", "closed_at")))
        pnl = safe_float(
            get_first(row, ("net_pnl_quote", "net_pnl_eur", "pnl_quote", "pnl"))
        )
        hold = safe_float(get_first(row, ("hold_minutes", "holding_minutes")))
        symbol = str(get_first(row, ("market", "symbol")) or "?")

        if close_at is None or pnl is None:
            continue

        entry_at = close_at
        if hold is not None and hold >= 0:
            entry_at = close_at - timedelta(minutes=hold)

        if side == "SELL":
            source = "BOT_LONG"
            variant = "LONG"
        else:
            source = "PAPER_SHORT"
            variant = "SHORT"

        result.append(
            make_trade(
                source=source,
                variant=variant,
                symbol=symbol,
                entry_at=entry_at,
                pnl_eur=pnl,
                detail=str(get_first(row, ("reason", "exit_reason")) or ""),
            )
        )

    return result


def load_long_shadow() -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []

    for row in read_csv(LONG_SHADOW):
        entry_at = parse_dt(
            get_first(
                row,
                (
                    "entry_at",
                    "opened_at",
                    "signal_closed_at",
                    "signal_candle",
                    "closed_at",
                ),
            )
        )
        pnl = safe_float(
            get_first(row, ("net_pnl_eur", "net_pnl_quote", "pnl_eur", "pnl"))
        )
        if entry_at is None or pnl is None:
            continue

        result.append(
            make_trade(
                source="LONG_SHADOW",
                variant=str(row.get("variant") or "?"),
                symbol=str(row.get("symbol") or "?"),
                entry_at=entry_at,
                pnl_eur=pnl,
                detail=str(row.get("exit_reason") or ""),
            )
        )

    return result


def load_scanner_shadow() -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []

    for row in read_csv(SCANNER_SHADOW):
        entry_at = parse_dt(
            get_first(
                row,
                (
                    "entry_at",
                    "opened_at",
                    "signal_at",
                    "signal_candle",
                    "signal_timestamp",
                    "closed_at",
                    "exit_at",
                ),
            )
        )
        pnl = safe_float(
            get_first(row, ("net_pnl_eur", "net_pnl_quote", "pnl_eur", "pnl"))
        )
        if entry_at is None or pnl is None:
            continue

        strategy = str(
            get_first(row, ("strategy", "signal_strategy", "entry_strategy")) or ""
        )

        result.append(
            make_trade(
                source="SCANNER_SHADOW",
                variant=str(row.get("variant") or "?"),
                symbol=str(row.get("symbol") or row.get("market") or "?"),
                entry_at=entry_at,
                pnl_eur=pnl,
                detail=strategy,
            )
        )

    return result


def group_summaries(
    rows: List[Dict[str, Any]],
    key_fn,
) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(key_fn(row))].append(row)

    return {
        key: summary(items)
        for key, items in sorted(groups.items(), key=lambda kv: kv[0])
    }


def analyze_variant(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "overall": summary(rows),
        "by_utc_hour": group_summaries(
            rows,
            lambda x: f"{x['entry_at'].hour:02d}:00",
        ),
        "by_time_block": group_summaries(
            rows,
            lambda x: time_block(x["entry_at"].hour),
        ),
        "by_weekday": group_summaries(
            rows,
            lambda x: f"{x['entry_at'].weekday()}-{WEEKDAY_NAMES[x['entry_at'].weekday()]}",
        ),
        "weekday_vs_weekend": group_summaries(
            rows,
            lambda x: "weekend" if x["entry_at"].weekday() >= 5 else "werkdag",
        ),
    }


def ranked_groups(groups: Dict[str, Dict[str, Any]], minimum: int = 3):
    candidates = [
        (name, data)
        for name, data in groups.items()
        if int(data.get("trades") or 0) >= minimum
    ]
    candidates.sort(
        key=lambda item: (
            float(item[1].get("average_pnl_eur") or 0),
            float(item[1].get("net_pnl_eur") or 0),
        ),
        reverse=True,
    )
    return candidates


def print_group_line(name: str, data: Dict[str, Any]) -> None:
    pf = data.get("profit_factor")
    pf_text = "∞" if pf is None else f"{float(pf):.2f}"
    print(
        f"{name:12} "
        f"{data['trades']:>3} trades | "
        f"{data['wins']}W/{data['losses']}L | "
        f"{data['winrate_pct']:5.1f}% | "
        f"€{data['net_pnl_eur']:+7.2f} | "
        f"gem €{data['average_pnl_eur']:+6.2f} | "
        f"PF {pf_text} | {data['sample_quality']}"
    )


def print_variant(name: str, data: Dict[str, Any]) -> None:
    print(f"--- {name} ---")
    print_group_line("TOTAAL", data["overall"])

    print("TIJDBLOKKEN:")
    for block in ("00-05 UTC", "06-11 UTC", "12-17 UTC", "18-23 UTC"):
        if block in data["by_time_block"]:
            print_group_line(block, data["by_time_block"][block])

    print("WERKDAG/WEEKEND:")
    for key in ("werkdag", "weekend"):
        if key in data["weekday_vs_weekend"]:
            print_group_line(key, data["weekday_vs_weekend"][key])

    ranked = ranked_groups(data["by_utc_hour"], minimum=3)
    print("BESTE UREN MET >=3 TRADES:")
    if not ranked:
        print("nog geen uur met minimaal 3 trades")
    else:
        for hour, item in ranked[:3]:
            print_group_line(hour, item)

    print()


def main() -> int:
    all_rows: List[Dict[str, Any]] = []
    all_rows.extend(load_bot_trades())
    all_rows.extend(load_long_shadow())
    all_rows.extend(load_scanner_shadow())

    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in all_rows:
        groups[f"{row['source']}:{row['variant']}"].append(row)

    analyzed = {
        name: analyze_variant(items)
        for name, items in sorted(groups.items())
    }

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
        "rules": {
            "time_basis": "entry time in UTC",
            "time_blocks": [
                "00-05 UTC",
                "06-11 UTC",
                "12-17 UTC",
                "18-23 UTC",
            ],
            "minimum_samples_for_hour_ranking": 3,
            "sample_warning": (
                "Groepen met weinig trades zijn alleen indicatief en geen basis "
                "voor een live filter."
            ),
        },
        "sources": {
            "diamond_transactions.csv": TRANSACTIONS.exists(),
            "diamond_long_entry_shadow_trades.csv": LONG_SHADOW.exists(),
            "diamond_scanner_selective_shadow_trades.csv": SCANNER_SHADOW.exists(),
        },
        "datasets": analyzed,
    }

    with REPORT_JSON.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False, default=str)
        handle.write("\n")

    print("============================================================")
    print(" DIAMOND TRADER - TIME / SESSION ANALYSIS v1.0")
    print("============================================================")
    print()
    print("Basis: entrytijd in UTC")
    print("Let op: kleine steekproeven zijn alleen indicatief.")
    print()

    if not analyzed:
        print("Geen bruikbare gesloten trades gevonden.")
        return 1

    preferred_order = [
        "BOT_LONG:LONG",
        "PAPER_SHORT:SHORT",
        "LONG_SHADOW:CURRENT",
        "LONG_SHADOW:WAIT_15M",
        "LONG_SHADOW:WAIT_30M",
        "SCANNER_SHADOW:CURRENT",
        "SCANNER_SHADOW:SELECTIVE",
        "SCANNER_SHADOW:STRONG",
    ]

    done = set()
    for name in preferred_order:
        if name in analyzed:
            print_variant(name, analyzed[name])
            done.add(name)

    for name in analyzed:
        if name not in done:
            print_variant(name, analyzed[name])

    print("=== VEILIGHEID ===")
    print("Exchange calls      : NEE")
    print("Private API         : NEE")
    print("Orders mogelijk     : NEE")
    print("Bot/config wijzigen : NEE")
    print()
    print(f"Rapport: {REPORT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
