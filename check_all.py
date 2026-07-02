#!/usr/bin/env python3
"""
check_all.py - Uitgebreid overzicht van de Diamond Grid Bot.
Runnen op Render:
    python3 check_all.py
"""
import json
import csv
import os
from pathlib import Path

import ccxt
from dotenv import load_dotenv

STATE_FILE = Path("/var/data/grid_state.json")
TRANSACTIONS_FILE = Path("/var/data/grid_transactions.csv")
MAX_POSITIONS_PER_COIN = 4


def load_state():
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def get_live_saldo():
    """Saldo staat niet in state.json, dus live ophalen bij Bitvavo."""
    load_dotenv()
    exchange = ccxt.bitvavo({
        "apiKey": os.getenv("BITVAVO_API_KEY", "").strip(),
        "secret": os.getenv("BITVAVO_API_SECRET", "").strip(),
        "enableRateLimit": True,
    })
    bal = exchange.fetch_balance()
    return float((bal.get("free") or {}).get("EUR", 0))


def print_posities_overzicht(state):
    """Toont per coin hoeveel posities open staan t.o.v. het maximum."""
    print("\n=== POSITIES T.O.V. MAXIMUM ===")
    grids = state.get("grids", {})
    for coin, data in grids.items():
        posities = data.get("positions", {})
        aantal = len(posities)
        vol = "VOL" if aantal >= MAX_POSITIONS_PER_COIN else ""
        print(f"  {coin}: {aantal}/{MAX_POSITIONS_PER_COIN} {vol}")


def print_saldo_overzicht(state):
    """Toont vrij saldo (live) als percentage van total_inleg."""
    total_inleg = state.get("total_inleg", 0)
    try:
        vrij_saldo = get_live_saldo()
    except Exception as e:
        print(f"\n=== SALDO ===\n  Kon live saldo niet ophalen: {e}")
        return
    pct = (vrij_saldo / total_inleg) * 100 if total_inleg > 0 else 0
    print("\n=== SALDO ===")
    print(f"  Total inleg : €{total_inleg:.2f}")
    print(f"  Vrij saldo  : €{vrij_saldo:.2f} ({pct:.1f}%)")


def print_matched_trades(n=10):
    """Koppelt koop- en verkooporders per coin (FIFO) op basis van de
    daadwerkelijke CSV-kolommen: ts, market, side, amount, price, quote_amount, pnl."""
    if not TRANSACTIONS_FILE.exists():
        print("\nGeen grid_transactions.csv gevonden.")
        return
    with open(TRANSACTIONS_FILE, "r") as f:
        rows = list(csv.DictReader(f))

    open_buys = {}
    matched = []
    for row in rows:
        coin = row["market"]
        side = row["side"].lower()
        open_buys.setdefault(coin, [])
        if side == "buy":
            open_buys[coin].append(row)
        elif side == "sell":
            pnl = float(row.get("pnl", 0) or 0)
            if open_buys[coin]:
                buy = open_buys[coin].pop(0)
                matched.append({
                    "coin": coin,
                    "buy_time": buy["ts"],
                    "sell_time": row["ts"],
                    "amount": row["amount"],
                    "pnl": pnl,
                })
            else:
                matched.append({
                    "coin": coin,
                    "buy_time": "?",
                    "sell_time": row["ts"],
                    "amount": row["amount"],
                    "pnl": pnl,
                })

    print(f"\n=== LAATSTE {n} MATCHED TRADES ===")
    for m in matched[-n:]:
        print(f"  {m['coin']}: koop {m['buy_time']} -> verkoop {m['sell_time']} | "
              f"{m['amount']} | PnL €{m['pnl']:+.2f}")

    print("\n=== NOG OPEN KOOPPOSITIES (ongematcht) ===")
    for coin, buys in open_buys.items():
        for b in buys:
            print(f"  {coin}: gekocht {b['ts']} | {b['amount']} @ €{b['price']}")


def main():
    state = load_state()
    print(f"Status: {'GEPAUZEERD - ' + state.get('pause_reason','') if state.get('paused') else 'ACTIEF'}")
    print_saldo_overzicht(state)
    print_posities_overzicht(state)
    print_matched_trades()


if __name__ == "__main__":
    main()
