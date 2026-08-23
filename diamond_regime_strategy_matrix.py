#!/usr/bin/env python3
"""Read-only regime x strategy outcome matrix for Diamond Trader.

Purpose:
- Check whether current scanner regime labels improve actual strategy outcomes.
- Uses only completed scanner shadow trades already stored locally.
- No API calls, orders, config changes, strategy changes or LIVE changes.

Source:
  /var/data/diamond_shadow_trades.csv
"""

from __future__ import annotations

import argparse
import csv
import math
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

DATA = Path(os.getenv("DIAMOND_DATA_DIR", "/var/data"))
SOURCE = DATA / "diamond_shadow_trades.csv"
REGIMES = ("BULLISH", "BULLISH_WEAK", "NEUTRAL", "BEARISH_WEAK", "BEARISH")


def f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


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


def pf(rows: Iterable[Dict[str, Any]]) -> Optional[float]:
    pnl = [f(row.get("net_pnl_eur")) for row in rows]
    gp = sum(x for x in pnl if x > 0)
    gl = abs(sum(x for x in pnl if x < 0))
    if gl > 0:
        return gp / gl
    if gp > 0:
        return math.inf
    return None


def pf_text(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    if math.isinf(value):
        return "INF"
    return f"{value:.3f}"


def summary(rows: List[Dict[str, Any]]) -> Tuple[int, int, int, float, Optional[float], float]:
    pnl = [f(row.get("net_pnl_eur")) for row in rows]
    wins = sum(x > 0 for x in pnl)
    losses = sum(x < 0 for x in pnl)
    total = sum(pnl)
    avg = total / len(rows) if rows else 0.0
    return len(rows), wins, losses, total, pf(rows), avg


def load(days: int) -> List[Dict[str, Any]]:
    if not SOURCE.is_file():
        raise FileNotFoundError(SOURCE)

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    with SOURCE.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "opened_at", "closed_at", "symbol", "strategy", "side",
            "market_regime", "net_pnl_eur", "total_fees_eur",
            "entry_spread_pct", "exit_reason",
        }
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise RuntimeError("CSV mist kolommen: " + ", ".join(sorted(missing)))

        rows: List[Dict[str, Any]] = []
        for raw in reader:
            opened = parse_dt(raw.get("opened_at"))
            if opened is None or opened < cutoff:
                continue
            if not str(raw.get("closed_at") or "").strip():
                continue
            regime = str(raw.get("market_regime") or "").strip().upper()
            side = str(raw.get("side") or "").strip().upper()
            strategy = str(raw.get("strategy") or "").strip()
            if regime not in REGIMES or side not in {"LONG", "SHORT"} or not strategy:
                continue
            rows.append(dict(raw))
    return rows


def print_group(label: str, rows: List[Dict[str, Any]]) -> None:
    n, w, l, pnl, factor, avg = summary(rows)
    wr = (w / n * 100.0) if n else 0.0
    fees = sum(f(row.get("total_fees_eur")) for row in rows)
    print(
        f"{label:36} n={n:3d} W/L={w:2d}/{l:2d} WR={wr:5.1f}% "
        f"PnL=€{pnl:+8.3f} PF={pf_text(factor):>6} AVG=€{avg:+6.3f} fees=€{fees:.2f}"
    )


def exact(rows: List[Dict[str, Any]], side: str, strategy: str, regimes: Iterable[str]) -> List[Dict[str, Any]]:
    allowed = set(regimes)
    return [
        row for row in rows
        if str(row.get("side") or "").upper() == side
        and str(row.get("strategy") or "") == strategy
        and str(row.get("market_regime") or "").upper() in allowed
    ]


def run(days: int) -> int:
    rows = load(days)
    print("=" * 112)
    print(" DIAMOND REGIME x STRATEGY OUTCOME MATRIX")
    print("=" * 112)
    print(f"Periode           : laatste {days} dagen")
    print(f"Gesloten trades   : {len(rows)}")
    print("Bron              : bestaande scanner shadow-trades")
    print("LIVE/config       : ONGEWIJZIGD")
    print("Orders/private API: NEE")

    groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (
            str(row.get("side") or "").upper(),
            str(row.get("strategy") or ""),
            str(row.get("market_regime") or "").upper(),
        )
        groups[key].append(row)

    print("\n=== ALLE STRATEGIE x REGIME GROEPEN ===")
    for key in sorted(groups):
        side, strategy, regime = key
        print_group(f"{side} {strategy} | {regime}", groups[key])

    print("\n=== LONG MOMENTUM: HELPT REGIMEFILTER? ===")
    print_group("ALL regimes", exact(rows, "LONG", "momentum", REGIMES))
    print_group("BULLISH only", exact(rows, "LONG", "momentum", ["BULLISH"]))
    print_group("BULLISH_WEAK only", exact(rows, "LONG", "momentum", ["BULLISH_WEAK"]))
    print_group("BULLISH + BULLISH_WEAK", exact(rows, "LONG", "momentum", ["BULLISH", "BULLISH_WEAK"]))
    print_group("NEUTRAL", exact(rows, "LONG", "momentum", ["NEUTRAL"]))
    print_group("BEARISH labels", exact(rows, "LONG", "momentum", ["BEARISH", "BEARISH_WEAK"]))

    print("\n=== SHORT MOMENTUM: HELPT REGIMEFILTER? ===")
    print_group("ALL regimes", exact(rows, "SHORT", "momentum", REGIMES))
    print_group("BEARISH only", exact(rows, "SHORT", "momentum", ["BEARISH"]))
    print_group("BEARISH_WEAK only", exact(rows, "SHORT", "momentum", ["BEARISH_WEAK"]))
    print_group("BEARISH + BEARISH_WEAK", exact(rows, "SHORT", "momentum", ["BEARISH", "BEARISH_WEAK"]))
    print_group("NEUTRAL", exact(rows, "SHORT", "momentum", ["NEUTRAL"]))
    print_group("BULLISH labels", exact(rows, "SHORT", "momentum", ["BULLISH", "BULLISH_WEAK"]))

    print("\n=== LONG TREND_BREAKOUT: HELPT REGIMEFILTER? ===")
    print_group("ALL regimes", exact(rows, "LONG", "trend_breakout", REGIMES))
    print_group("BULLISH only", exact(rows, "LONG", "trend_breakout", ["BULLISH"]))
    print_group("BULLISH_WEAK only", exact(rows, "LONG", "trend_breakout", ["BULLISH_WEAK"]))
    print_group("BULLISH + BULLISH_WEAK", exact(rows, "LONG", "trend_breakout", ["BULLISH", "BULLISH_WEAK"]))
    print_group("NEUTRAL", exact(rows, "LONG", "trend_breakout", ["NEUTRAL"]))

    print("\n=== INTERPRETATIEHULP ===")
    print("Gebruik regime alleen als gate wanneer de gefilterde groep aantoonbaar beter is dan ALL regimes.")
    print("NEUTRAL betekent in de huidige scanner alleen gemengde 1u/4u EMA-checks; niet automatisch zijwaarts.")
    return 0


def self_test() -> int:
    sample = [
        {"side": "LONG", "strategy": "momentum", "market_regime": "BULLISH", "net_pnl_eur": "2", "total_fees_eur": "0.6"},
        {"side": "LONG", "strategy": "momentum", "market_regime": "BULLISH", "net_pnl_eur": "-1", "total_fees_eur": "0.6"},
        {"side": "SHORT", "strategy": "momentum", "market_regime": "BEARISH", "net_pnl_eur": "3", "total_fees_eur": "0.6"},
    ]
    group = exact(sample, "LONG", "momentum", ["BULLISH"])
    n, w, l, pnl, factor, avg = summary(group)
    assert n == 2 and w == 1 and l == 1
    assert abs(pnl - 1.0) < 1e-9
    assert abs(float(factor) - 2.0) < 1e-9
    assert abs(avg - 0.5) < 1e-9
    print("DIAMOND_REGIME_STRATEGY_MATRIX_SELF_TEST_OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    return run(args.days)


if __name__ == "__main__":
    raise SystemExit(main())
