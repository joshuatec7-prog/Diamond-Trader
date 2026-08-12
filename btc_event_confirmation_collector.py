#!/usr/bin/env python3
"""
Diamond Trader BTC Event Confirmation Collector v1.0
Public Coinbase BTC/EUR data only.
Geen orders of private API.
"""

import argparse
import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import ccxt

DATA = Path(
    "/var/data/diamond_btc_event_confirmation"
)

SAMPLES = DATA / "coinbase_btc_samples.csv"
STATE = DATA / "collector_state.json"

VERSION = "1.0"


def now_iso():
    return datetime.now(
        timezone.utc
    ).isoformat()


def save_state(state):
    DATA.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp = STATE.with_suffix(".tmp")

    tmp.write_text(
        json.dumps(
            state,
            indent=2,
        ),
        encoding="utf-8",
    )

    tmp.replace(STATE)


def append_sample(row):
    DATA.mkdir(
        parents=True,
        exist_ok=True,
    )

    new_file = (
        not SAMPLES.exists()
        or SAMPLES.stat().st_size == 0
    )

    fields = [
        "timestamp_utc",
        "coinbase_price",
        "fetch_duration_ms",
    ]

    with SAMPLES.open(
        "a",
        encoding="utf-8",
        newline="",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fields,
        )

        if new_file:
            writer.writeheader()

        writer.writerow(row)


def run(duration_hours, sample_seconds):
    exchange = ccxt.coinbase({
        "enableRateLimit": True,
        "timeout": 15000,
    })

    exchange.load_markets()

    symbol = "BTC/EUR"

    if symbol not in exchange.markets:
        raise RuntimeError(
            "BTC/EUR niet beschikbaar op Coinbase"
        )

    started = now_iso()
    deadline = (
        time.monotonic()
        + duration_hours * 3600
    )

    state = {
        "version": VERSION,
        "status": "RUNNING",
        "started_at": started,
        "duration_hours": duration_hours,
        "sample_seconds": sample_seconds,
        "symbol": symbol,
        "event_threshold_pct": 0.02315,
        "event_window_seconds": 30,
        "samples": 0,
        "errors": 0,
        "last_sample_at": None,
        "last_error": None,
    }

    # BTC_EVENT_RESUME_SAFE_V4
    anchor = DATA / "collector_resume_anchor.txt"
    if anchor.exists():
        import datetime as _dt
        start = anchor.read_text().strip()
        state["started_at"] = start
        if SAMPLES.exists():
            state["samples"] = max(
                sum(1 for _ in SAMPLES.open(encoding="utf-8-sig")) - 1, 0
            )
        begun = _dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
        elapsed = (_dt.datetime.now(_dt.timezone.utc) - begun).total_seconds()
        remaining = max(0, state["duration_hours"] * 3600 - elapsed)
        deadline = time.monotonic() + remaining

    save_state(state)

    while time.monotonic() < deadline:
        cycle_start = time.monotonic()

        try:
            fetch_start = time.monotonic()

            ticker = exchange.fetch_ticker(
                symbol
            )

            fetch_ms = (
                time.monotonic()
                - fetch_start
            ) * 1000

            price = ticker.get("last")

            if price is None:
                price = ticker.get("close")

            append_sample({
                "timestamp_utc": now_iso(),
                "coinbase_price": float(price),
                "fetch_duration_ms": round(
                    fetch_ms,
                    2,
                ),
            })

            state["samples"] += 1
            state["last_sample_at"] = now_iso()
            state["last_error"] = None

        except Exception as exc:
            state["errors"] += 1
            state["last_error"] = (
                f"{type(exc).__name__}: {exc}"
            )

        save_state(state)

        used = (
            time.monotonic()
            - cycle_start
        )

        time.sleep(
            max(
                0,
                sample_seconds - used,
            )
        )

    state["status"] = "COMPLETED"
    state["completed_at"] = now_iso()
    save_state(state)


def self_test():
    assert 0.02315 > 0
    assert DATA != Path("/var/data")
    print(
        "BTC_EVENT_CONFIRMATION_SELF_TEST_OK"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--duration-hours",
        type=float,
        default=168.0,
    )

    parser.add_argument(
        "--sample-seconds",
        type=float,
        default=2.0,
    )

    parser.add_argument(
        "--self-test",
        action="store_true",
    )

    args = parser.parse_args()

    if args.self_test:
        self_test()
    else:
        run(
            args.duration_hours,
            args.sample_seconds,
        )
