#!/usr/bin/env python3
"""Read-only AUTO LIVE 5 status."""

import json
from pathlib import Path

AUTO = Path("/var/data/diamond_auto_live_5_state.json")
BOT = Path("/var/data/diamond_state.json")
APPROVAL = Path("/var/data/diamond_live_approval.json")


def load(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def count(value):
    if isinstance(value, (dict, list)):
        return len(value)
    return int(bool(value))


def main():
    a = load(AUTO)
    b = load(BOT)
    p = load(APPROVAL)

    print("=" * 72)
    print(" DIAMOND AUTO LIVE 5 STATUS")
    print("=" * 72)
    if not a:
        print("Status        : NOG NIET GEACTIVEERD")
        print("Voortgang     : 0/5")
    else:
        print(f"Status        : {a.get('status') or 'UNKNOWN'}")
        print(f"Voortgang     : {int(a.get('completed_buys') or 0)}/{int(a.get('target_buys') or 5)}")
        print(f"Geactiveerd   : {a.get('activated_at') or '-'}")
        print(f"Start sequence: {a.get('start_sequence')}")
        print(f"Actieve coin  : {a.get('active_symbol') or '-'}")
        print(f"Actieve seq   : {a.get('active_expected_sequence') or '-'}")
        print(f"Laatste reden : {a.get('last_reason') or '-'}")

    positions = b.get("positions") or b.get("open_positions") or {}
    pending = b.get("pending_orders") or {}
    recovery = bool(b.get("recovery_required") or b.get("recovery_needed"))
    print(f"Open posities : {count(positions)}")
    print(f"Pending orders: {count(pending)}")
    print(f"Recovery      : {recovery}")
    print(f"Bot sequence  : {int(b.get('canary_trade_sequence') or 0)}")
    print(f"Approval      : {p.get('status') or 'GEEN'} | source={p.get('source') or '-'}")
    print("Orders/API    : NEE - alleen-lezen")


if __name__ == "__main__":
    main()
