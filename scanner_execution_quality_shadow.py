#!/usr/bin/env python3
"""
Diamond Trader Execution Quality Shadow v1.0

Prospectieve read-only test op nieuwe SELECTIVE trades.
Geen orders, geen private API en geen live wijzigingen.
"""

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

DATA = Path("/var/data")

SIGNALS = DATA / "diamond_market_signals.csv"

TRADES = (
    DATA /
    "diamond_scanner_selective_shadow_trades.csv"
)

BASELINE = (
    DATA /
    "diamond_execution_quality_shadow_baseline.json"
)

REPORT = (
    DATA /
    "diamond_execution_quality_shadow_report.json"
)

VERSION = "1.0"
TARGET = 20

VOLUME_MIN = 2.389331
QUOTE_MIN = 1441684.12


def now_iso():
    return datetime.now(
        timezone.utc
    ).isoformat()


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_dt(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            str(value).replace(
                "Z",
                "+00:00",
            )
        )
    except ValueError:
        return None


def read_csv(path):
    if (
        not path.exists()
        or path.stat().st_size == 0
    ):
        return []

    with path.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as f:
        return list(csv.DictReader(f))


def signal_key(row):
    return "|".join([
        str(row.get("symbol") or "").upper(),
        str(row.get("strategy") or ""),
        str(row.get("side") or "").upper(),
        str(row.get("candle_timestamp") or ""),
    ])


def save_json(path, data):
    tmp = path.with_suffix(
        path.suffix + ".tmp"
    )

    tmp.write_text(
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    tmp.replace(path)


def load_baseline():
    if BASELINE.exists():
        baseline = json.loads(
            BASELINE.read_text(
                encoding="utf-8"
            )
        )

        if not baseline.get(
            "strong_high_volume_started_at"
        ):
            baseline[
                "strong_high_volume_started_at"
            ] = now_iso()

            baseline.setdefault(
                "groups", {}
            )["STRONG_HIGH_VOLUME"] = (
                "STRONG en volume_ratio >= 2.389331"
            )

            save_json(
                BASELINE,
                baseline,
            )

        return baseline

    baseline = {
        "version": VERSION,
        "started_at": now_iso(),

        "fixed_thresholds": {
            "volume_ratio_min": VOLUME_MIN,
            "quote_volume_min": QUOTE_MIN,
        },

        "groups": {
            "BASELINE":
                "alle nieuwe SELECTIVE trades",

            "HIGH_VOLUME":
                "volume_ratio >= 2.389331",

            "HIGH_VOLUME_QUOTE":
                "volume_ratio >= 2.389331 EN "
                "quote_volume >= 1441684.12",

            "LOW_VOLUME":
                "volume_ratio < 2.389331",
        },

        "safety": {
            "orders_possible": False,
            "private_api": False,
            "live_changes": False,
        },
    }

    save_json(
        BASELINE,
        baseline,
    )

    return baseline


def stats(rows):
    pnl = [
        num(row.get("net_pnl_eur"))
        for row in rows
    ]

    wins = sum(
        value > 0
        for value in pnl
    )

    losses = sum(
        value < 0
        for value in pnl
    )

    gain = sum(
        max(0.0, value)
        for value in pnl
    )

    loss = sum(
        abs(min(0.0, value))
        for value in pnl
    )

    pf = (
        round(gain / loss, 4)
        if loss
        else (
            "inf"
            if gain
            else 0.0
        )
    )

    return {
        "closed": len(rows),
        "wins": wins,
        "losses": losses,
        "pnl": round(sum(pnl), 4),
        "profit_factor": pf,
        "remaining": max(
            0,
            TARGET - len(rows),
        ),
    }


def run():
    baseline = load_baseline()
    started = parse_dt(
        baseline["started_at"]
    )

    signals = read_csv(SIGNALS)
    trades = read_csv(TRADES)

    strong_keys = {
        str(row.get("candidate_key") or "")
        for row in trades
        if str(
            row.get("variant") or ""
        ).upper() == "STRONG"
    }

    signal_map = {}

    for row in signals:
        signal_map[signal_key(row)] = {
            "detected_at": parse_dt(
                row.get("detected_at")
            ),
            "volume_ratio": num(
                row.get("volume_ratio")
            ),
            "quote_volume": num(
                row.get("quote_volume")
            ),
        }

    selected = []

    for trade in trades:
        if str(
            trade.get("variant") or ""
        ).upper() != "SELECTIVE":
            continue

        key = str(
            trade.get("candidate_key")
            or ""
        )

        info = signal_map.get(key)

        if not info:
            continue

        detected = info["detected_at"]

        if (
            detected is None
            or detected < started
        ):
            continue

        row = dict(trade)
        row.update(info)
        row["is_strong"] = key in strong_keys
        selected.append(row)

    strong_started = parse_dt(
        baseline[
            "strong_high_volume_started_at"
        ]
    )

    groups = {
        "BASELINE": selected,

        "HIGH_VOLUME": [
            row for row in selected
            if row["volume_ratio"] >= VOLUME_MIN
        ],

        "HIGH_VOLUME_QUOTE": [
            row for row in selected
            if (
                row["volume_ratio"] >= VOLUME_MIN
                and row["quote_volume"] >= QUOTE_MIN
            )
        ],

        "LOW_VOLUME": [
            row for row in selected
            if (
                row["volume_ratio"] > 0
                and row["volume_ratio"] < VOLUME_MIN
            )
        ],

        "STRONG_HIGH_VOLUME": [
            row for row in selected
            if (
                row["is_strong"]
                and row["volume_ratio"] >= VOLUME_MIN
                and row["detected_at"] >= strong_started
            )
        ],
    }

    report = {
        "version": VERSION,
        "generated_at": now_iso(),
        "started_at": baseline["started_at"],
        "thresholds": baseline["fixed_thresholds"],
        "groups": {
            name: stats(rows)
            for name, rows in groups.items()
        },
    }

    save_json(REPORT, report)

    print("=" * 82)
    print(" DIAMOND TRADER EXECUTION QUALITY SHADOW")
    print("=" * 82)
    print("Gestart :", report["started_at"])
    print()

    for name, item in report["groups"].items():
        print(
            f"{name:<20} "
            f"closed={item['closed']:2d}/{TARGET} "
            f"W/L={item['wins']:2d}/{item['losses']:2d} "
            f"pnl=€{item['pnl']:+.4f} "
            f"PF={item['profit_factor']}"
        )

    print()
    print(
        "Orders: NEE | Private API: NEE | "
        "Live wijziging: NEE"
    )
    print("=" * 82)


if __name__ == "__main__":
    run()
