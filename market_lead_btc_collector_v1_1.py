#!/usr/bin/env python3
"""
Diamond Trader - BTC Market Lead Collector v1.1

Doel
----
Strengere nachtmeting voor alleen BTC-EUR om te onderzoeken of Coinbase
enkele seconden vóór Bitvavo beweegt.

Verbeteringen t.o.v. v1.0
------------------------
- alleen BTC-EUR
- sample iedere 2 seconden
- Coinbase-tick leeftijd wordt per sample opgeslagen
- Bitvavo REST-latency wordt per sample opgeslagen
- aparte dataset, zodat de eerste 6-uursmeting intact blijft
- automatische memory safety stop
- standaard 6 uur

Uitvoer
-------
/var/data/diamond_market_lead_btc/
    btc_market_lead_samples_v1_1.csv
    btc_market_lead_state_v1_1.json
    btc_market_lead_collector_v1_1.log

Veiligheid
----------
- publieke Coinbase WebSocket
- publieke Bitvavo REST
- geen API keys
- geen private API
- geen orders
- geen bot/config-wijzigingen
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp

VERSION = "1.1"
MODE = "READ_ONLY_BTC_MARKET_LEAD"

SYMBOL = "BTC-EUR"
COINBASE_WS = "wss://advanced-trade-ws.coinbase.com"
BITVAVO_PRICE_URL = "https://api.bitvavo.com/v2/ticker/price"

DATA_DIR = Path("/var/data/diamond_market_lead_btc")
CSV_FILE = DATA_DIR / "btc_market_lead_samples_v1_1.csv"
STATE_FILE = DATA_DIR / "btc_market_lead_state_v1_1.json"

DEFAULT_DURATION_HOURS = 6.0
DEFAULT_SAMPLE_SECONDS = 2.0

MEMORY_STOP_MIB = 1600.0
MEMORY_STOP_CONSECUTIVE = 5

FIELDS = [
    "timestamp_utc",
    "coinbase_price",
    "bitvavo_price",
    "coinbase_age_ms",
    "bitvavo_fetch_ms",
    "price_diff_pct",
]

SAFETY = {
    "orders_possible": False,
    "private_api": False,
    "api_keys_used": False,
    "config_modified": False,
    "bot_state_modified": False,
    "transactions_modified": False,
    "automatic_live_changes": False,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_float(value: Any) -> Optional[float]:
    try:
        x = float(value)
        if math.isfinite(x) and x > 0:
            return x
    except Exception:
        pass
    return None


def cgroup_memory_mib() -> float:
    try:
        return int(
            Path("/sys/fs/cgroup/memory.current").read_text().strip()
        ) / 1048576
    except Exception:
        return 0.0


def process_rss_mib() -> float:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except Exception:
        pass
    return 0.0


def save_state(state: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, STATE_FILE)


def append_sample(row: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    need_header = not CSV_FILE.exists() or CSV_FILE.stat().st_size == 0

    with CSV_FILE.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if need_header:
            writer.writeheader()
        writer.writerow(row)


async def coinbase_reader(
    session: aiohttp.ClientSession,
    latest: Dict[str, Any],
    counters: Dict[str, Any],
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        try:
            async with session.ws_connect(
                COINBASE_WS,
                heartbeat=20,
                receive_timeout=30,
            ) as ws:
                counters["coinbase_connects"] += 1

                await ws.send_json({
                    "type": "subscribe",
                    "product_ids": [SYMBOL],
                    "channel": "ticker",
                })

                await ws.send_json({
                    "type": "subscribe",
                    "channel": "heartbeats",
                })

                async for msg in ws:
                    if stop.is_set():
                        break

                    if msg.type != aiohttp.WSMsgType.TEXT:
                        if msg.type in (
                            aiohttp.WSMsgType.ERROR,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSE,
                        ):
                            break
                        continue

                    try:
                        data = json.loads(msg.data)
                    except Exception:
                        continue

                    if data.get("channel") != "ticker":
                        continue

                    for event in data.get("events") or []:
                        for ticker in event.get("tickers") or []:
                            if ticker.get("product_id") != SYMBOL:
                                continue

                            price = safe_float(ticker.get("price"))
                            if price is None:
                                continue

                            latest["price"] = price
                            latest["received_monotonic"] = time.monotonic()
                            latest["received_at"] = now_iso()
                            counters["coinbase_updates"] += 1

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            counters["coinbase_errors"] += 1
            counters["last_error"] = (
                f"coinbase_ws: {type(exc).__name__}: {exc}"
            )

        if not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass


async def fetch_bitvavo(
    session: aiohttp.ClientSession,
) -> tuple[Optional[float], float]:
    started = time.monotonic()
    try:
        async with session.get(
            BITVAVO_PRICE_URL,
            params={"market": SYMBOL},
        ) as response:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            if response.status != 200:
                return None, elapsed_ms

            data = await response.json()
            return safe_float(data.get("price")), elapsed_ms

    except Exception:
        return None, (time.monotonic() - started) * 1000.0


def fresh_state(duration_hours: float, sample_seconds: float) -> Dict[str, Any]:
    current = cgroup_memory_mib()

    return {
        "version": VERSION,
        "mode": MODE,
        "status": "RUNNING",
        "started_at": now_iso(),
        "completed_at": None,
        "duration_hours": duration_hours,
        "sample_seconds": sample_seconds,
        "symbol": SYMBOL,
        "cycles": 0,
        "samples_written": 0,
        "coinbase_updates": 0,
        "coinbase_connects": 0,
        "coinbase_errors": 0,
        "bitvavo_success": 0,
        "bitvavo_errors": 0,
        "last_error": None,
        "memory": {
            "start_cgroup_mib": round(current, 1),
            "max_cgroup_mib": round(current, 1),
            "max_process_rss_mib": round(process_rss_mib(), 1),
            "stop_threshold_mib": MEMORY_STOP_MIB,
        },
        "safety": SAFETY,
    }


async def run(duration_hours: float, sample_seconds: float) -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    state = fresh_state(duration_hours, sample_seconds)
    save_state(state)

    latest: Dict[str, Any] = {}
    counters = {
        "coinbase_updates": 0,
        "coinbase_connects": 0,
        "coinbase_errors": 0,
        "last_error": None,
    }

    stop = asyncio.Event()
    deadline = time.monotonic() + duration_hours * 3600.0
    high_memory_count = 0

    timeout = aiohttp.ClientTimeout(total=15)
    connector = aiohttp.TCPConnector(limit=4)

    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
    ) as session:

        reader_task = asyncio.create_task(
            coinbase_reader(
                session=session,
                latest=latest,
                counters=counters,
                stop=stop,
            )
        )

        try:
            await asyncio.sleep(2)

            while time.monotonic() < deadline:
                cycle_start = time.monotonic()

                bv_price, fetch_ms = await fetch_bitvavo(session)

                if bv_price is None:
                    state["bitvavo_errors"] += 1
                else:
                    state["bitvavo_success"] += 1

                    if latest.get("price") is not None:
                        cb_price = float(latest["price"])
                        cb_age_ms = max(
                            0.0,
                            (
                                time.monotonic()
                                - float(latest["received_monotonic"])
                            ) * 1000.0,
                        )

                        diff_pct = (
                            (cb_price - bv_price)
                            / bv_price
                            * 100.0
                        )

                        append_sample({
                            "timestamp_utc": now_iso(),
                            "coinbase_price": f"{cb_price:.12f}",
                            "bitvavo_price": f"{bv_price:.12f}",
                            "coinbase_age_ms": f"{cb_age_ms:.3f}",
                            "bitvavo_fetch_ms": f"{fetch_ms:.3f}",
                            "price_diff_pct": f"{diff_pct:.8f}",
                        })

                        state["samples_written"] += 1

                state["cycles"] += 1
                state["coinbase_updates"] = counters["coinbase_updates"]
                state["coinbase_connects"] = counters["coinbase_connects"]
                state["coinbase_errors"] = counters["coinbase_errors"]
                state["last_error"] = counters["last_error"]

                current_mem = cgroup_memory_mib()
                process_rss = process_rss_mib()

                state["memory"]["max_cgroup_mib"] = round(
                    max(
                        float(state["memory"]["max_cgroup_mib"]),
                        current_mem,
                    ),
                    1,
                )
                state["memory"]["max_process_rss_mib"] = round(
                    max(
                        float(state["memory"]["max_process_rss_mib"]),
                        process_rss,
                    ),
                    1,
                )

                if current_mem >= MEMORY_STOP_MIB:
                    high_memory_count += 1
                else:
                    high_memory_count = 0

                if high_memory_count >= MEMORY_STOP_CONSECUTIVE:
                    state["status"] = "MEMORY_SAFETY_STOP"
                    state["last_error"] = (
                        f"memory.current >= {MEMORY_STOP_MIB:.0f} MiB "
                        f"for {MEMORY_STOP_CONSECUTIVE} cycles"
                    )
                    save_state(state)
                    break

                save_state(state)

                elapsed = time.monotonic() - cycle_start
                sleep_for = max(0.0, sample_seconds - elapsed)
                if sleep_for:
                    await asyncio.sleep(sleep_for)

        finally:
            stop.set()
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    if state["status"] == "RUNNING":
        state["status"] = "COMPLETED"

    state["completed_at"] = now_iso()
    state["coinbase_updates"] = counters["coinbase_updates"]
    state["coinbase_connects"] = counters["coinbase_connects"]
    state["coinbase_errors"] = counters["coinbase_errors"]
    state["memory"]["end_cgroup_mib"] = round(cgroup_memory_mib(), 1)
    state["memory"]["end_process_rss_mib"] = round(process_rss_mib(), 1)
    save_state(state)

    return 0 if state["status"] == "COMPLETED" else 2


def status() -> int:
    if not STATE_FILE.exists():
        print("Nog geen BTC Market Lead state aanwezig.")
        return 1

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))

    print("=== BTC MARKET LEAD v1.1 STATUS ===")
    for key in (
        "version",
        "status",
        "started_at",
        "completed_at",
        "duration_hours",
        "sample_seconds",
        "cycles",
        "samples_written",
        "coinbase_updates",
        "coinbase_connects",
        "coinbase_errors",
        "bitvavo_success",
        "bitvavo_errors",
        "last_error",
    ):
        print(f"{key:20}: {state.get(key)}")

    print("memory              :", state.get("memory"))
    print("safety              :", state.get("safety"))
    return 0


def self_test() -> int:
    assert SYMBOL == "BTC-EUR"
    assert DEFAULT_SAMPLE_SECONDS == 2.0
    assert MEMORY_STOP_MIB == 1600.0
    assert SAFETY["orders_possible"] is False
    assert SAFETY["private_api"] is False
    assert SAFETY["api_keys_used"] is False

    print("BTC_MARKET_LEAD_COLLECTOR_V1_1_SELF_TEST_OK")
    print("Markt               : BTC-EUR")
    print("Coinbase            : publieke WebSocket")
    print("Bitvavo             : publieke REST")
    print("Standaard duur      : 6 uur")
    print("Sample interval     : 2 seconden")
    print("Memory safety stop  : 1600 MiB x 5")
    print("API keys            : NEE")
    print("Private API         : NEE")
    print("Orders mogelijk     : NEE")
    print("Bot/config wijzigen : NEE")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument(
        "--duration-hours",
        type=float,
        default=DEFAULT_DURATION_HOURS,
    )
    parser.add_argument(
        "--sample-seconds",
        type=float,
        default=DEFAULT_SAMPLE_SECONDS,
    )
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    if args.status:
        return status()

    duration = min(max(float(args.duration_hours), 0.05), 24.0)
    sample_seconds = min(max(float(args.sample_seconds), 1.0), 60.0)

    return asyncio.run(run(duration, sample_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
