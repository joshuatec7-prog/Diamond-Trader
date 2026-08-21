#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import defaultdict
from pathlib import Path

VERSION = "1.0"
MODE = "READ_ONLY_SCANNER_SHADOW_COMBO_ANALYSIS"
TRADES_FILE = Path("/var/data/diamond_shadow_trades.csv")


def num(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def load_rows():
    if not TRADES_FILE.exists():
        raise FileNotFoundError(f"Bestand ontbreekt: {TRADES_FILE}")
    with TRADES_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return []
    required = {
        "closed_at", "symbol", "strategy", "side", "market_regime",
        "signal_score", "gross_pnl_eur", "total_fees_eur",
        "net_pnl_eur", "exit_reason", "duration_minutes"
    }
    missing = required - set(rows[0].keys())
    if missing:
        raise ValueError("Ontbrekende kolommen: " + ", ".join(sorted(missing)))
    return rows


def profit_factor(rows):
    gains = sum(max(0.0, num(r.get("net_pnl_eur"))) for r in rows)
    losses = sum(abs(min(0.0, num(r.get("net_pnl_eur")))) for r in rows)
    if losses <= 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def stats(rows):
    n = len(rows)
    wins = sum(num(r.get("net_pnl_eur")) > 0 for r in rows)
    losses = sum(num(r.get("net_pnl_eur")) < 0 for r in rows)
    net = sum(num(r.get("net_pnl_eur")) for r in rows)
    gross = sum(num(r.get("gross_pnl_eur")) for r in rows)
    fees = sum(num(r.get("total_fees_eur")) for r in rows)
    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "wr": (wins / n * 100.0) if n else 0.0,
        "gross": gross,
        "fees": fees,
        "net": net,
        "pf": profit_factor(rows),
    }


def fmt(label, rows):
    s = stats(rows)
    pf_text = "inf" if math.isinf(s["pf"]) else f'{s["pf"]:.2f}'
    return (
        f"{label:<42} n={s['n']:2d} "
        f"W/L={s['wins']:2d}/{s['losses']:2d} "
        f"WR={s['wr']:5.1f}% "
        f"net=€{s['net']:+8.3f} PF={pf_text}"
    )


def group_report(title, rows, key_fn, min_n=2):
    groups = defaultdict(list)
    for r in rows:
        groups[key_fn(r)].append(r)

    items = []
    for key, subset in groups.items():
        if len(subset) >= min_n:
            s = stats(subset)
            items.append((s["net"], s["pf"], len(subset), str(key), subset))

    items.sort(reverse=True)

    print()
    print(title)
    print("-" * 104)
    for _, _, _, key, subset in items:
        print(fmt(key, subset))


def candidate_report(rows):
    candidates = {
        "SHORT alle": lambda r: r.get("side") == "SHORT",
        "LONG alle": lambda r: r.get("side") == "LONG",
        "trend_breakout alle": lambda r: r.get("strategy") == "trend_breakout",
        "SHORT + trend_breakout": lambda r: r.get("side") == "SHORT" and r.get("strategy") == "trend_breakout",
        "LONG + trend_breakout": lambda r: r.get("side") == "LONG" and r.get("strategy") == "trend_breakout",
        "SHORT + pullback_retest": lambda r: r.get("side") == "SHORT" and r.get("strategy") == "pullback_retest",
        "SHORT + momentum": lambda r: r.get("side") == "SHORT" and r.get("strategy") == "momentum",
        "SHORT + range_breakout": lambda r: r.get("side") == "SHORT" and r.get("strategy") == "range_breakout",
        "BEARISH_WEAK alle": lambda r: r.get("market_regime") == "BEARISH_WEAK",
        "SHORT + BEARISH_WEAK": lambda r: r.get("side") == "SHORT" and r.get("market_regime") == "BEARISH_WEAK",
        "BULLISH alle": lambda r: str(r.get("market_regime") or "").startswith("BULLISH"),
        "BEARISH alle": lambda r: str(r.get("market_regime") or "").startswith("BEARISH"),
        "score >=95": lambda r: num(r.get("signal_score")) >= 95,
        "SHORT + score >=95": lambda r: r.get("side") == "SHORT" and num(r.get("signal_score")) >= 95,
        "trend_breakout + score >=95": lambda r: r.get("strategy") == "trend_breakout" and num(r.get("signal_score")) >= 95,
    }

    print()
    print("GERICHTE HYPOTHESES")
    print("-" * 104)
    for label, test in candidates.items():
        subset = [r for r in rows if test(r)]
        print(fmt(label, subset))


def recent_split(rows):
    print()
    print("RECENT VERSUS EERDER")
    print("-" * 104)
    for n in (5, 10, 20):
        if len(rows) > n:
            print(fmt(f"laatste {n}", rows[-n:]))
            print(fmt(f"alles vóór laatste {n}", rows[:-n]))


def symbol_exclusions(rows):
    counts = defaultdict(list)
    for r in rows:
        counts[r.get("symbol") or "-"].append(r)

    bad = {
        symbol for symbol, subset in counts.items()
        if len(subset) >= 3 and stats(subset)["net"] < 0
    }

    print()
    print("SYMBOL-EXCLUSIE HYPOTHESE")
    print("-" * 104)
    print("Negatieve munten met minimaal 3 trades:", ", ".join(sorted(bad)) or "geen")
    kept = [r for r in rows if (r.get("symbol") or "-") not in bad]
    print(fmt("zonder deze munten", kept))
    shorts = [r for r in kept if r.get("side") == "SHORT"]
    print(fmt("SHORT zonder deze munten", shorts))


def self_test():
    sample = [
        {"net_pnl_eur": "2", "gross_pnl_eur": "2.6", "total_fees_eur": "0.6",
         "side": "SHORT", "strategy": "trend_breakout", "market_regime": "BEARISH_WEAK",
         "signal_score": "97"},
        {"net_pnl_eur": "-1", "gross_pnl_eur": "-0.4", "total_fees_eur": "0.6",
         "side": "LONG", "strategy": "momentum", "market_regime": "BULLISH",
         "signal_score": "92"},
    ]
    s = stats(sample)
    assert s["n"] == 2
    assert abs(s["net"] - 1.0) < 1e-9
    assert abs(s["pf"] - 2.0) < 1e-9
    print("SCANNER_SHADOW_COMBO_SELF_TEST_OK")
    return 0


def analysis():
    rows = load_rows()

    print("=" * 104)
    print(" DIAMOND TRADER SCANNER SHADOW COMBINATION ANALYSIS")
    print("=" * 104)
    print(f"Versie                 : {VERSION}")
    print(f"Modus                  : {MODE}")
    print(f"Gesloten schaduwtrades : {len(rows)}")
    print("Orders mogelijk        : NEE")
    print("Private API            : NEE")
    print("Config/state gewijzigd : NEE")
    print("/var/data geschreven   : NEE")

    if not rows:
        return 0

    print()
    print("BASIS")
    print("-" * 104)
    print(fmt("ALLE TRADES", rows))

    candidate_report(rows)

    group_report(
        "SIDE + STRATEGIE",
        rows,
        lambda r: f"{r.get('side','-')} + {r.get('strategy','-')}",
        min_n=2,
    )

    group_report(
        "SIDE + MARKTREGIME",
        rows,
        lambda r: f"{r.get('side','-')} + {r.get('market_regime','-')}",
        min_n=2,
    )

    group_report(
        "STRATEGIE + MARKTREGIME",
        rows,
        lambda r: f"{r.get('strategy','-')} + {r.get('market_regime','-')}",
        min_n=2,
    )

    recent_split(rows)
    symbol_exclusions(rows)

    print()
    print("SLOT")
    print("-" * 104)
    print(
        "Alleen-lezen analyse. Kleine groepen zijn alleen hypotheses; "
        "geen filter of handelsinstelling wordt automatisch aangepast."
    )
    print("=" * 104)
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    return self_test() if a.self_test else analysis()


if __name__ == "__main__":
    sys.exit(main())
