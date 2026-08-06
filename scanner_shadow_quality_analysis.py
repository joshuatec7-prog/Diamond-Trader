#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import defaultdict
from pathlib import Path

VERSION = "1.0"
MODE = "READ_ONLY_SCANNER_SHADOW_QUALITY"
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
        "signal_score", "stake_eur", "total_fees_eur", "exit_reason",
        "gross_pnl_eur", "net_pnl_eur", "duration_minutes"
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


def summary(label, rows):
    n = len(rows)
    wins = sum(num(r.get("net_pnl_eur")) > 0 for r in rows)
    losses = sum(num(r.get("net_pnl_eur")) < 0 for r in rows)
    neutral = n - wins - losses
    gross = sum(num(r.get("gross_pnl_eur")) for r in rows)
    fees = sum(num(r.get("total_fees_eur")) for r in rows)
    net = sum(num(r.get("net_pnl_eur")) for r in rows)
    pf = profit_factor(rows)
    pf_text = "inf" if math.isinf(pf) else f"{pf:.2f}"
    wr = wins / n * 100 if n else 0.0
    return (
        f"{label:<24} n={n:3d} W/L/N={wins:2d}/{losses:2d}/{neutral:2d} "
        f"WR={wr:5.1f}% gross=€{gross:+8.3f} fees=€{fees:7.3f} "
        f"net=€{net:+8.3f} PF={pf_text}"
    )


def print_groups(title, rows, key_fn, min_n=1):
    groups = defaultdict(list)
    for r in rows:
        groups[key_fn(r)].append(r)
    print()
    print(title)
    print("-" * 96)
    for key, subset in sorted(groups.items(), key=lambda kv: (-len(kv[1]), str(kv[0]))):
        if len(subset) >= min_n:
            print(summary(str(key), subset))


def score_bucket(r):
    s = num(r.get("signal_score"))
    if s >= 95:
        return "score >=95"
    if s >= 90:
        return "score 90-94.9"
    if s >= 80:
        return "score 80-89.9"
    if s >= 70:
        return "score 70-79.9"
    return "score <70"


def duration_bucket(r):
    m = num(r.get("duration_minutes"))
    if m <= 60:
        return "<=1 uur"
    if m <= 240:
        return "1-4 uur"
    if m <= 720:
        return "4-12 uur"
    if m <= 1440:
        return "12-24 uur"
    return ">24 uur"


def analysis():
    rows = load_rows()

    print("=" * 96)
    print(" DIAMOND TRADER SCANNER SHADOW QUALITY ANALYSIS")
    print("=" * 96)
    print(f"Versie                 : {VERSION}")
    print(f"Modus                  : {MODE}")
    print(f"Gesloten schaduwtrades : {len(rows)}")
    print("Orders mogelijk        : NEE")
    print("Private API            : NEE")
    print("Config/state gewijzigd : NEE")
    print("/var/data geschreven   : NEE")

    if not rows:
        print("Geen schaduwtrades gevonden.")
        return 0

    print()
    print("TOTAAL")
    print("-" * 96)
    print(summary("ALLE TRADES", rows))

    gross = sum(num(r.get("gross_pnl_eur")) for r in rows)
    fees = sum(num(r.get("total_fees_eur")) for r in rows)
    net = sum(num(r.get("net_pnl_eur")) for r in rows)

    print()
    print("KOSTENEFFECT")
    print("-" * 96)
    print(f"Bruto resultaat        : €{gross:+.4f}")
    print(f"Totale handelskosten   : €{fees:.4f}")
    print(f"Netto resultaat        : €{net:+.4f}")
    if gross > 0 > net:
        print("Conclusie              : bruto positief, kosten maken het netto negatief")
    elif gross <= 0:
        print("Conclusie              : al bruto niet winstgevend")
    else:
        print("Conclusie              : bruto en netto positief")

    print_groups("PER RICHTING", rows, lambda r: r.get("side") or "-")
    print_groups("PER STRATEGIE", rows, lambda r: r.get("strategy") or "-")
    print_groups("PER MARKTREGIME", rows, lambda r: r.get("market_regime") or "-")
    print_groups("PER SCOREGROEP", rows, score_bucket)
    print_groups("PER HOUDTIJD", rows, duration_bucket)
    print_groups("PER MUNT (MINIMAAL 2 TRADES)", rows, lambda r: r.get("symbol") or "-", min_n=2)
    print_groups("PER EXIT-REDEN", rows, lambda r: r.get("exit_reason") or "-")

    print()
    print("RECENTE VENSTERS")
    print("-" * 96)
    for n in (5, 10, 20, 30):
        if len(rows) >= n:
            print(summary(f"laatste {n}", rows[-n:]))

    print()
    print("BESTE 10")
    print("-" * 96)
    for r in sorted(rows, key=lambda x: num(x.get("net_pnl_eur")), reverse=True)[:10]:
        print(
            f"{r.get('closed_at','-')[:19]:19s} "
            f"{r.get('symbol','-'):<11} {r.get('side','-'):<5} "
            f"{r.get('strategy','-'):<16} score={num(r.get('signal_score')):5.1f} "
            f"net=€{num(r.get('net_pnl_eur')):+7.3f} {r.get('exit_reason','-')}"
        )

    print()
    print("SLECHTSTE 10")
    print("-" * 96)
    for r in sorted(rows, key=lambda x: num(x.get("net_pnl_eur")))[:10]:
        print(
            f"{r.get('closed_at','-')[:19]:19s} "
            f"{r.get('symbol','-'):<11} {r.get('side','-'):<5} "
            f"{r.get('strategy','-'):<16} score={num(r.get('signal_score')):5.1f} "
            f"net=€{num(r.get('net_pnl_eur')):+7.3f} {r.get('exit_reason','-')}"
        )

    print()
    print("SLOT")
    print("-" * 96)
    print("Alleen-lezen analyse. Geen filter of handelsinstelling is aangepast.")
    print("=" * 96)
    return 0


def self_test():
    sample = [
        {"net_pnl_eur": "2", "gross_pnl_eur": "2.6", "total_fees_eur": "0.6",
         "signal_score": "96", "duration_minutes": "30"},
        {"net_pnl_eur": "-1", "gross_pnl_eur": "-0.4", "total_fees_eur": "0.6",
         "signal_score": "82", "duration_minutes": "300"},
    ]
    assert abs(profit_factor(sample) - 2.0) < 1e-9
    assert score_bucket(sample[0]) == "score >=95"
    assert duration_bucket(sample[1]) == "4-12 uur"
    print("SCANNER_SHADOW_QUALITY_SELF_TEST_OK")
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    return self_test() if a.self_test else analysis()


if __name__ == "__main__":
    sys.exit(main())
