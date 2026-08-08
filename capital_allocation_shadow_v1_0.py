#!/usr/bin/env python3

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.0"
MODE = "READ_ONLY_CAPITAL_ALLOCATION_SHADOW"

TRADES = Path(
    "/var/data/diamond_scanner_selective_shadow_trades.csv"
)

STATE = Path(
    "/var/data/diamond_capital_allocation_shadow_state.json"
)

START_BALANCE = 3000.0
RESERVE = 250.0

VARIANTS = {
    "BASE": "€130 altijd",
    "RR160": "€195 bij RR >= 1.60, anders €130",
    "QUALITY": "€195 bij LONG + trend_breakout, anders €130",
    "STRONG_QUALITY":
        "€260 bij LONG + trend_breakout + RR >= 1.60, anders €130",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def stake_for(name, row):
    rr = float(row["reward_risk"])
    side = row["side"]
    strategy = row["strategy"]

    if name == "BASE":
        return 130.0

    if name == "RR160":
        return 195.0 if rr >= 1.60 else 130.0

    quality = (
        side == "LONG"
        and strategy == "trend_breakout"
    )

    if name == "QUALITY":
        return 195.0 if quality else 130.0

    if name == "STRONG_QUALITY":
        return 260.0 if quality and rr >= 1.60 else 130.0

    return 130.0


def load_trades():
    rows = []

    with TRADES.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["variant"] != "SELECTIVE":
                continue
            if not r["closed_at"]:
                continue
            rows.append(r)

    return rows

def initialize():
    trades = load_trades()

    baseline = len(trades)

    state = {
        "version": VERSION,
        "mode": MODE,
        "started_at": now_iso(),
        "baseline_closed_trades": baseline,
        "start_balance_eur": START_BALANCE,
        "reserve_eur": RESERVE,
        "variants": {
            name: {
                "closed": 0,
                "net_pnl_eur": 0.0,
                "balance_eur": START_BALANCE,
            }
            for name in VARIANTS
        },
        "processed_keys": [],
        "safety": {
            "orders_possible": False,
            "private_api": False,
            "config_modified": False,
            "bot_state_modified": False,
        },
    }

    with STATE.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

    return state


def trade_key(row):
    return (
        f"{row['candidate_key']}|"
        f"{row['opened_at']}|"
        f"{row['closed_at']}"
    )


def update():
    if not STATE.exists():
        state = initialize()
    else:
        with STATE.open(encoding="utf-8") as f:
            state = json.load(f)

    rows = load_trades()
    baseline = int(state["baseline_closed_trades"])

    rows = rows[baseline:]

    processed = set(state.get("processed_keys", []))

    for row in rows:
        key = trade_key(row)

        if key in processed:
            continue

        old_stake = float(row["stake_eur"])
        old_pnl = float(row["net_pnl_eur"])

        for name in VARIANTS:
            stake = stake_for(name, row)

            scaled_pnl = (
                old_pnl * stake / old_stake
                if old_stake > 0 else 0.0
            )

            v = state["variants"][name]

            v["closed"] += 1
            v["net_pnl_eur"] = round(
                float(v["net_pnl_eur"]) + scaled_pnl,
                6,
            )

            v["balance_eur"] = round(
                START_BALANCE + float(v["net_pnl_eur"]),
                6,
            )

        processed.add(key)

    state["processed_keys"] = list(processed)[-500:]
    state["last_update"] = now_iso()

    with STATE.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

    return state

def print_status(state):
    print("=" * 70)
    print("DIAMOND TRADER CAPITAL ALLOCATION SHADOW")
    print("=" * 70)
    print("Versie        :", state["version"])
    print("Gestart       :", state["started_at"])
    print("Startsaldo    : €", state["start_balance_eur"])
    print("Reserve       : €", state["reserve_eur"])
    print()

    for name, rule in VARIANTS.items():
        v = state["variants"][name]
        print(
            f"{name:15s} "
            f"closed={v['closed']:2d} "
            f"pnl=€{v['net_pnl_eur']:+.2f} "
            f"saldo=€{v['balance_eur']:.2f}"
        )
        print(" ", rule)

    print()
    print("Orders mogelijk : NEE")
    print("Private API     : NEE")


def self_test():
    test = {
        "reward_risk": "1.70",
        "side": "LONG",
        "strategy": "trend_breakout",
    }

    assert stake_for("BASE", test) == 130
    assert stake_for("RR160", test) == 195
    assert stake_for("QUALITY", test) == 195
    assert stake_for("STRONG_QUALITY", test) == 260

    print("CAPITAL_ALLOCATION_SHADOW_V1_0_SELF_TEST_OK")
    print("Reserve : €250")
    print("Basis   : €130")
    print("Max sterk signaal : €260")
    print("Orders  : NEE")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        self_test()
    elif args.init:
        print_status(initialize())
    elif args.update:
        print_status(update())
    elif args.status:
        if not STATE.exists():
            print("NOG NIET GEINITIALISEERD")
        else:
            with STATE.open(encoding="utf-8") as f:
                print_status(json.load(f))


if __name__ == "__main__":
    main()
