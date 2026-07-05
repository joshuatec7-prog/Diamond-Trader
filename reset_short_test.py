#!/usr/bin/env python3
"""
reset_short_test.py - Wist de short-test state en CSV, zodat je met een
schone lei test. Raakt grid_bot.py / grid_state.json NIET aan.
Runnen op Render:
    python3 reset_short_test.py
"""
from pathlib import Path

STATE_FILE = Path("/var/data/short_test_state.json")
TRADES_FILE = Path("/var/data/short_test_transactions.csv")

for f in (STATE_FILE, TRADES_FILE):
    if f.exists():
        f.unlink()
        print(f"Verwijderd: {f}")
    else:
        print(f"Bestond niet: {f}")

print("Klaar. Start short_test.py opnieuw voor een schone test.")
