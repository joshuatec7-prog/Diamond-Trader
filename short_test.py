#!/usr/bin/env python3
"""
check_short_test.py - Overzicht van de short paper-test.
Runnen op Render:
    python3 check_short_test.py
"""
import json
import csv
from pathlib import Path

STATE_FILE = Path("/var/data/short_test_state.json")
TRANSACTIONS_FILE = Path("/var/data/short_test_transactions.csv")


def load_state():
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def print_overzicht(state):
    print(f"\n=== TOTAAL === \n  Trades: {state.get('trades', 0)} | "
          f"Wins: {state.get('wins', 0)} | PnL: €{state.get('pnl', 0):+.2f}")

    print("\n=== OPEN POSITIES PER COIN ===")
    for coin, grid in state.get("grids", {}).items():
        posities = grid.get("positions", {})
        for key, pos in posities.items():
            print(f"  {coin}: level={key} | entry={pos['entry_price']:.4f} | "
                  f"tp={pos['take_profit_at']:.4f} | stop={pos['stop_at']:.4f} | ts={pos['ts']}")


def print_trades_per_coin(n=20):
    if not TRANSACTIONS_FILE.exists():
        print("\nGeen short_test_transactions.csv gevonden.")
        return

    with open(TRANSACTIONS_FILE, "r") as f:
        rows = list(csv.DictReader(f))

    print(f"\n=== LAATSTE {n} TRADES ===")
    for row in rows[-n:]:
        print(f"  {row['ts']} | {row['market']} | {row['side']} | "
              f"prijs={row['price']} | pnl=€{float(row.get('pnl', 0)):+.2f}")

    # winst/verlies per coin optellen
    per_coin = {}
    for row in rows:
        if row["side"] != "SHORT_CLOSE":
            continue
        coin = row["market"]
        pnl = float(row.get("pnl", 0))
        per_coin.setdefault(coin, {"pnl": 0.0, "trades": 0, "wins": 0})
        per_coin[coin]["pnl"] += pnl
        per_coin[coin]["trades"] += 1
        if pnl > 0:
            per_coin[coin]["wins"] += 1

    print("\n=== PER COIN (gesloten trades) ===")
    for coin, d in per_coin.items():
        winrate = (d["wins"] / d["trades"] * 100) if d["trades"] else 0
        print(f"  {coin}: trades={d['trades']} | winrate={winrate:.0f}% | pnl=€{d['pnl']:+.2f}")


def main():
    state = load_state()
    print_overzicht(state)
    print_trades_per_coin()


if __name__ == "__main__":
    main()
