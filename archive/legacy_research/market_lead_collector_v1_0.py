#!/usr/bin/env python3
"""
Diamond Trader - Coinbase vs Bitvavo Market Lead Collector v1.0

Doel
----
Tijdelijke read-only meetproef om later te onderzoeken of Coinbase BTC-EUR
en ETH-EUR prijsbewegingen eerder laat zien dan Bitvavo.

Werking
-------
- Coinbase: publieke Advanced Trade WebSocket ticker
- Bitvavo: publieke REST ticker/price
- geen API keys
- geen private API
- geen orders
- standaard 8 uur
- standaard iedere 5 seconden een gesynchroniseerde snapshot
- stopt automatisch na de ingestelde duur
- stopt uit voorzorg wanneer cgroup memory.current 3 metingen achter elkaar
  >= 440 MiB is

Uitvoer
-------
/var/data/diamond_market_lead/
    market_lead_samples_v1_0.csv
    market_lead_state_v1_0.json

Gebruik
-------
python3 market_lead_collector_v1_0.py --self-test
python3 market_lead_collector_v1_0.py --duration-hours 8
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

VERSION = "1.0"
MODE = "READ_ONLY_COINBASE_BITVAVO_MARKET_LEAD"

PRODUCTS = ("BTC-EUR", "ETH-EUR")

COINBASE_WS = "wss://advanced-trade-ws.coinbase.com"
BITVAVO_PRICE_URL = "https://api.bitvavo.com/v2/ticker/price"

DATA_DIR = Path("/var/data/diamond_market_lead")
CSV_FILE = DATA_DIR / "market_lead_samples_v1_0.csv"
STATE_FILE = DATA_DIR / "market_lead_state_v1_0.json"

DEFAULT_DURATION_HOURS = 8.0
DEFAULT_SAMPLE_SECONDS = 5.0
MEMORY_STOP_MIB = 440.0
MEMORY_STOP_CONSECUTIVE = 3

CSV_FIELDS = [
    "timestamp_utc",
    "symbol",
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


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def atomic_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def cgroup_memory_mib() -> float:
    try:
        raw = Path("/sys/fs/cgroup/memory.current").read_text().strip()
        return int(raw) / 1048576
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


def safe_float(value: Any) -> Optional[float]:
    try:
        x = float(value)
        return x if math.isfinite(x) and x > 0 else None
    except Exception:
        return None


def append_rows(rows: list[Dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    header = not CSV_FILE.exists() or CSV_FILE.stat().st_size == 0

    with CSV_FILE.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        if header:
            writer.writeheader()
        writer.writerows(rows)


async def coinbase_reader(
    session: aiohttp.ClientSession,
    latest: Dict[str, Dict[str, Any]],
    counters: Dict[str, Any],
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        try:
            async with session.ws_connect(
                COINBASE_WS,
                heartbeat=20,
                receive_timeout=30,
            ) as ws:
                counters["coinbase_connects"] += 1

                await ws.send_json({
                    "type": "subscribe",
                    "product_ids": list(PRODUCTS),
                    "channel": "ticker",
                })
                await ws.send_json({
                    "type": "subscribe",
                    "channel": "heartbeats",
                })

                async for message in ws:
                    if stop_event.is_set():
                        break

                    if message.type == aiohttp.WSMsgType.TEXT:
                        try:
                            data = json.loads(message.data)
                        except Exception:
                            continue

                        if data.get("channel") != "ticker":
                            continue

                        received_monotonic = time.monotonic()
                        received_at = now_iso()

                        for event in data.get("events") or []:
                            for ticker in event.get("tickers") or []:
                                symbol = ticker.get("product_id")
                                price = safe_float(ticker.get("price"))
                                if symbol not in PRODUCTS or price is None:
                                    continue

                                latest[symbol] = {
                                    "price": price,
                                    "received_monotonic": received_monotonic,
                                    "received_at": received_at,
                                }
                                counters["coinbase_updates"][symbol] += 1

                    elif message.type in (
                        aiohttp.WSMsgType.ERROR,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSE,
                    ):
                        break

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            counters["errors"]["coinbase_ws"] = (
                counters["errors"].get("coinbase_ws", 0) + 1
            )
            counters["last_error"] = (
                f"coinbase_ws: {type(exc).__name__}: {exc}"
            )

        if not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass


async def fetch_bitvavo_price(
    session: aiohttp.ClientSession,
    symbol: str,
) -> tuple[Optional[float], float]:
    started = time.monotonic()
    try:
        async with session.get(
            BITVAVO_PRICE_URL,
            params={"market": symbol},
        ) as response:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            if response.status != 200:
                return None, elapsed_ms

            data = await response.json()
            return safe_float(data.get("price")), elapsed_ms
    except Exception:
        elapsed_ms = (time.monotonic() - started) * 1000.0
        return None, elapsed_ms


def initial_state(duration_hours: float, sample_seconds: float) -> Dict[str, Any]:
    return {
        "version": VERSION,
        "mode": MODE,
        "started_at": now_iso(),
        "completed_at": None,
        "status": "RUNNING",
        "duration_hours": duration_hours,
        "sample_seconds": sample_seconds,
        "symbols": list(PRODUCTS),
        "cycles": 0,
        "samples_written": 0,
        "coinbase_updates": {symbol: 0 for symbol in PRODUCTS},
        "bitvavo_success": {symbol: 0 for symbol in PRODUCTS},
        "bitvavo_errors": {symbol: 0 for symbol in PRODUCTS},
        "coinbase_connects": 0,
        "errors": {},
        "last_error": None,
        "memory": {
            "start_cgroup_mib": round(cgroup_memory_mib(), 1),
            "max_cgroup_mib": round(cgroup_memory_mib(), 1),
            "max_process_rss_mib": round(process_rss_mib(), 1),
            "stop_threshold_mib": MEMORY_STOP_MIB,
        },
        "rate": {
            "bitvavo_requests_per_cycle": len(PRODUCTS),
            "estimated_requests_per_minute": round(
                len(PRODUCTS) * 60.0 / sample_seconds,
                2,
            ),
        },
        "safety": SAFETY,
    }


async def run_collector(duration_hours: float, sample_seconds: float) -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    latest: Dict[str, Dict[str, Any]] = {}
    stop_event = asyncio.Event()

    counters: Dict[str, Any] = {
        "coinbase_updates": {symbol: 0 for symbol in PRODUCTS},
        "coinbase_connects": 0,
        "errors": {},
        "last_error": None,
    }

    state = initial_state(duration_hours, sample_seconds)
    atomic_json(STATE_FILE, state)

    deadline = time.monotonic() + duration_hours * 3600.0
    high_memory_count = 0

    timeout = aiohttp.ClientTimeout(total=20)
    connector = aiohttp.TCPConnector(limit=6)

    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
    ) as session:
        ws_task = asyncio.create_task(
            coinbase_reader(
                session=session,
                latest=latest,
                counters=counters,
                stop_event=stop_event,
            )
        )

        try:
            # Geef de WebSocket kort tijd om de eerste tickers te ontvangen.
            await asyncio.sleep(2)

            while time.monotonic() < deadline:
                cycle_started = time.monotonic()
                timestamp = now_iso()

                tasks = [
                    fetch_bitvavo_price(session, symbol)
                    for symbol in PRODUCTS
                ]
                bitvavo_results = await asyncio.gather(*tasks)

                rows: list[Dict[str, Any]] = []

                for symbol, (bitvavo_price, fetch_ms) in zip(
                    PRODUCTS,
                    bitvavo_results,
                ):
                    if bitvavo_price is None:
                        state["bitvavo_errors"][symbol] += 1
                        state["errors"]["bitvavo_rest"] = (
                            state["errors"].get("bitvavo_rest", 0) + 1
                        )
                        continue

                    state["bitvavo_success"][symbol] += 1
                    cb = latest.get(symbol)

                    if not cb:
                        state["errors"]["coinbase_missing_price"] = (
                            state["errors"].get("coinbase_missing_price", 0) + 1
                        )
                        continue

                    coinbase_price = float(cb["price"])
                    age_ms = max(
                        0.0,
                        (time.monotonic() - float(cb["received_monotonic"]))
                        * 1000.0,
                    )

                    diff_pct = (
                        (coinbase_price - bitvavo_price)
                        / bitvavo_price
                        * 100.0
                    )

                    rows.append({
                        "timestamp_utc": timestamp,
                        "symbol": symbol,
                        "coinbase_price": f"{coinbase_price:.12f}",
                        "bitvavo_price": f"{bitvavo_price:.12f}",
                        "coinbase_age_ms": f"{age_ms:.3f}",
                        "bitvavo_fetch_ms": f"{fetch_ms:.3f}",
                        "price_diff_pct": f"{diff_pct:.8f}",
                    })

                if rows:
                    append_rows(rows)
                    state["samples_written"] += len(rows)

                state["cycles"] += 1
                state["coinbase_updates"] = dict(counters["coinbase_updates"])
                state["coinbase_connects"] = counters["coinbase_connects"]
                state["last_error"] = counters["last_error"]

                for key, value in counters["errors"].items():
                    state["errors"][key] = max(
                        int(state["errors"].get(key, 0)),
                        int(value),
                    )

                current_mem = cgroup_memory_mib()
                rss = process_rss_mib()

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
                        rss,
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
                        f"for {MEMORY_STOP_CONSECUTIVE} consecutive cycles"
                    )
                    atomic_json(STATE_FILE, state)
                    break

                atomic_json(STATE_FILE, state)

                elapsed = time.monotonic() - cycle_started
                sleep_for = max(0.0, sample_seconds - elapsed)
                if sleep_for:
                    await asyncio.sleep(sleep_for)

        finally:
            stop_event.set()
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    if state["status"] == "RUNNING":
        state["status"] = "COMPLETED"

    state["completed_at"] = now_iso()
    state["coinbase_updates"] = dict(counters["coinbase_updates"])
    state["coinbase_connects"] = counters["coinbase_connects"]
    state["memory"]["end_cgroup_mib"] = round(cgroup_memory_mib(), 1)
    state["memory"]["end_process_rss_mib"] = round(process_rss_mib(), 1)
    atomic_json(STATE_FILE, state)

    return 0 if state["status"] == "COMPLETED" else 2


def print_status() -> int:
    if not STATE_FILE.exists():
        print("Nog geen Market Lead state aanwezig.")
        return 1

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))

    print("=== MARKET LEAD COLLECTOR STATUS ===")
    print("versie      :", state.get("version"))
    print("status      :", state.get("status"))
    print("gestart     :", state.get("started_at"))
    print("klaar       :", state.get("completed_at"))
    print("duur uren   :", state.get("duration_hours"))
    print("cycles      :", state.get("cycles"))
    print("samples     :", state.get("samples_written"))
    print("CB updates  :", state.get("coinbase_updates"))
    print("BV success  :", state.get("bitvavo_success"))
    print("BV errors   :", state.get("bitvavo_errors"))
    print("errors      :", state.get("errors"))
    print("last error  :", state.get("last_error"))
    print("memory      :", state.get("memory"))
    print("safety      :", state.get("safety"))
    return 0


def self_test() -> int:
    assert PRODUCTS == ("BTC-EUR", "ETH-EUR")
    assert MEMORY_STOP_MIB == 440.0
    assert SAFETY["orders_possible"] is False
    assert SAFETY["private_api"] is False
    assert SAFETY["api_keys_used"] is False
    assert SAFETY["automatic_live_changes"] is False

    print("MARKET_LEAD_COLLECTOR_V1_0_SELF_TEST_OK")
    print("Markten             : BTC-EUR, ETH-EUR")
    print("Coinbase            : publieke WebSocket")
    print("Bitvavo             : publieke REST")
    print("Standaard duur      : 8 uur")
    print("Sample interval     : 5 seconden")
    print("Memory safety stop  : 440 MiB x 3")
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
        return print_status()

    duration_hours = min(max(float(args.duration_hours), 0.05), 24.0)
    sample_seconds = min(max(float(args.sample_seconds), 2.0), 60.0)

    return asyncio.run(
        run_collector(
            duration_hours=duration_hours,
            sample_seconds=sample_seconds,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
