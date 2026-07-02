#!/usr/bin/env python3
"""
check_all.py - Uitgebreid overzicht van de Diamond Grid Bot.

Nieuw t.o.v. vorige versie:
- Open posities per coin t.o.v. max (4)
- % vrij saldo t.o.v. total_inleg
- Laatste 10 trades met koop/verkoop gekoppeld (matched pairs)

Runnen op Render:
    python3 tools/check_all.py
"""

import json
import csv
from pathlib import Path

STATE_FILE = Path("/var/data/grid_state.json")
TRANSACTIONS_FILE = Path("/var/data/grid_transactions.csv")
MAX_POSITIONS_PER_COIN = 4


def load_state():
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def print_posities_overzicht(state):
    """Toont per coin hoeveel posities open staan t.o.v. het maximum."""
    print("\n=== POSITIES T.O.V. MAXIMUM ===")
    coins = state.get("coins", {})
    for coin, data in coins.items():
        posities = data.get("positions", [])
        aantal = len(posities)
        vol = "VOL" if aantal >= MAX_POSITIONS_PER_COIN else ""
        print(f"  {coin}: {aantal}/{MAX_POSITIONS_PER_COIN} {vol}")


def print_saldo_overzicht(state):
    """Toont vrij saldo als percentage van total_inleg."""
    total_inleg = state.get("total_inleg", 0)
    vrij_saldo = state.get("vrij_saldo", state.get("free_balance", 0))
    if total_inleg > 0:
        pct = (vrij_saldo / total_inleg) * 100
    else:
        pct = 0
    print("\n=== SALDO ===")
    print(f"  Total inleg : €{total_inleg:.2f}")
    print(f"  Vrij saldo  : €{vrij_saldo:.2f} ({pct:.1f}%)")


def print_matched_trades(n=10):
    """Koppelt koop- en verkooporders per coin op basis van hoeveelheid,
    zodat je meteen ziet welke koop bij welke verkoop hoort."""
    if not TRANSACTIONS_FILE.exists():
        print("\nGeen grid_transactions.csv gevonden.")
        return

    with open(TRANSACTIONS_FILE, "r") as f:
        rows = list(csv.DictReader(f))

    # verwacht kolommen: timestamp, coin, side, amount, price_eur
    open_buys = {}  # coin -> lijst van openstaande koopregels
    matched = []

    for row in rows:
        coin = row["coin"]
        side = row["side"].lower()
        open_buys.setdefault(coin, [])

        if side == "buy":
            open_buys[coin].append(row)
        elif side == "sell":
            if open_buys[coin]:
                buy = open_buys[coin].pop(0)  # FIFO matching
                pnl = float(row["price_eur"]) - float(buy["price_eur"])
                matched.append({
                    "coin": coin,
                    "buy_time": buy["timestamp"],
                    "sell_time": row["timestamp"],
                    "amount": row["amount"],
                    "pnl": pnl,
                })
            else:
                # verkoop zonder gekoppelde koop in deze dataset
                matched.append({
                    "coin": coin,
                    "buy_time": "?",
                    "sell_time": row["timestamp"],
                    "amount": row["amount"],
                    "pnl": None,
                })

    print(f"\n=== LAATSTE {n} MATCHED TRADES ===")
    for m in matched[-n:]:
        pnl_str = f"€{m['pnl']:+.2f}" if m["pnl"] is not None else "n.v.t."
        print(f"  {m['coin']}: koop {m['buy_time']} -> verkoop {m['sell_time']} | "
              f"{m['amount']} | PnL {pnl_str}")

    # onafgeronde koopposities (nog open)
    print("\n=== NOG OPEN KOOPPOSITIES (ongematcht) ===")
    for coin, buys in open_buys.items():
        for b in buys:
            print(f"  {coin}: gekocht {b['timestamp']} | {b['amount']} @ €{b['price_eur']}")


def main():
    state = load_state()
    print(f"Status: {state.get('status', '?')}")
    print_saldo_overzicht(state)
    print_posities_overzicht(state)
    print_matched_trades()


if __name__ == "__main__":
    main()
