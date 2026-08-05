#!/usr/bin/env python3
"""
Diamond Trader Early Entry Collector v1.2 - extra geheugenarm (REST-only)

Doel
----
Snelle publieke Bitvavo-marktdata verzamelen met zo weinig mogelijk RAM.

Verschil met v1.1
-----------------
- Geen ccxt.pro.
- Geen WebSocket-verbindingen.
- Alleen gewone publieke CCXT/REST-calls.
- Per munt sequentieel:
    * orderboek top 10
    * recente trades
- 1m/5m OHLCV ongeveer eens per minuut.
- Sample ongeveer elke 15 seconden.

Veiligheid
----------
- Alleen publieke Bitvavo-data.
- Geen API-key.
- Geen private API.
- Geen orders.
- Geen bot/config/strategie-wijzigingen.

Uitvoer
-------
/var/data/diamond_early_entry/early_entry_samples_v1_2.csv
/var/data/diamond_early_entry/early_entry_state_v1_2.json

Gebruik
-------
python3 early_entry_collector_v1_2.py --self-test
python3 early_entry_collector_v1_2.py --duration-seconds 300
python3 early_entry_collector_v1_2.py
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import signal
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

import ccxt


VERSION = "1.2"
MODE = "READ_ONLY_EARLY_ENTRY_COLLECTOR_REST_LOW_MEMORY"

SYMBOLS = [
    "BTC/EUR",
    "ETH/EUR",
    "SOL/EUR",
    "XRP/EUR",
    "ADA/EUR",
]

DATA_DIR = Path("/var/data/diamond_early_entry")
CSV_FILE = DATA_DIR / "early_entry_samples_v1_2.csv"
STATE_FILE = DATA_DIR / "early_entry_state_v1_2.json"

SAMPLE_INTERVAL_SECONDS = 15
ORDERBOOK_LEVELS = 10
TRADE_WINDOW_SECONDS = 60
OHLCV_REFRESH_SECONDS = 60
MAX_TRADE_EVENTS_PER_SYMBOL = 200

STOP_REQUESTED = False

CSV_FIELDS = [
    "timestamp_utc",
    "symbol",
    "bid",
    "ask",
    "last",
    "spread_pct",
    "book_bid_value_top10",
    "book_ask_value_top10",
    "book_imbalance",
    "trade_count_60s",
    "buy_count_60s",
    "sell_count_60s",
    "buy_value_60s",
    "sell_value_60s",
    "trade_imbalance_60s",
    "close_1m",
    "volume_1m",
    "close_5m",
    "volume_5m",
]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat()


def finite_float(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
        if math.isfinite(x):
            return x
    except Exception:
        pass
    return default


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def ensure_csv_header() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if CSV_FILE.exists() and CSV_FILE.stat().st_size > 0:
        return

    with CSV_FILE.open("w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()


def append_csv(row: Dict[str, Any]) -> None:
    ensure_csv_header()
    with CSV_FILE.open("a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=CSV_FIELDS).writerow(
            {k: row.get(k, "") for k in CSV_FIELDS}
        )


def make_symbol_state() -> Dict[str, Any]:
    return {
        "orderbook": {},
        "last_trade_price": 0.0,
        "ohlcv_1m": None,
        "ohlcv_5m": None,
        "last_update": {},
        "errors": {},
    }


class Collector:
    def __init__(self) -> None:
        self.exchange = ccxt.bitvavo({
            "enableRateLimit": True,
        })

        self.symbol_state: Dict[str, Dict[str, Any]] = {
            symbol: make_symbol_state()
            for symbol in SYMBOLS
        }

        self.trades: Dict[str, Deque[Dict[str, Any]]] = {
            symbol: deque(maxlen=MAX_TRADE_EVENTS_PER_SYMBOL)
            for symbol in SYMBOLS
        }

        self.seen_trade_ids: Dict[str, Deque[str]] = {
            symbol: deque(maxlen=400)
            for symbol in SYMBOLS
        }

        self.seen_trade_sets: Dict[str, set[str]] = {
            symbol: set()
            for symbol in SYMBOLS
        }

        self.started_at = iso_now()
        self.samples_written = 0
        self.cycles = 0
        self.errors_total = defaultdict(int)
        self.last_ohlcv_refresh_monotonic = 0.0

    def record_ok(self, symbol: str, name: str) -> None:
        state = self.symbol_state[symbol]
        state["last_update"][name] = iso_now()
        state["errors"].pop(name, None)

    def record_error(self, symbol: str, name: str, exc: Exception) -> None:
        self.symbol_state[symbol]["errors"][name] = (
            f"{type(exc).__name__}: {exc}"
        )
        self.errors_total[f"{symbol}:{name}"] += 1

    def fetch_orderbook(self, symbol: str) -> None:
        try:
            book = self.exchange.fetch_order_book(
                symbol,
                limit=ORDERBOOK_LEVELS,
            )

            self.symbol_state[symbol]["orderbook"] = {
                "bids": list((book or {}).get("bids") or [])[:ORDERBOOK_LEVELS],
                "asks": list((book or {}).get("asks") or [])[:ORDERBOOK_LEVELS],
            }
            self.record_ok(symbol, "orderbook")

        except Exception as exc:
            self.record_error(symbol, "orderbook", exc)

    def fetch_recent_trades(self, symbol: str) -> None:
        try:
            rows = self.exchange.fetch_trades(
                symbol,
                limit=100,
            )

            now_ms = int(time.time() * 1000)
            ids = self.seen_trade_ids[symbol]
            idset = self.seen_trade_sets[symbol]

            for trade in rows or []:
                trade_id = str(
                    trade.get("id")
                    or (
                        f"{trade.get('timestamp')}:"
                        f"{trade.get('price')}:"
                        f"{trade.get('amount')}:"
                        f"{trade.get('side')}"
                    )
                )

                if trade_id in idset:
                    continue

                if len(ids) == ids.maxlen:
                    old = ids.popleft()
                    idset.discard(old)

                ids.append(trade_id)
                idset.add(trade_id)

                price = finite_float(trade.get("price"))
                amount = finite_float(trade.get("amount"))

                self.trades[symbol].append({
                    "timestamp": int(trade.get("timestamp") or now_ms),
                    "price": price,
                    "amount": amount,
                    "side": str(trade.get("side") or "").lower(),
                })

                if price > 0:
                    self.symbol_state[symbol]["last_trade_price"] = price

            self.record_ok(symbol, "trades")

        except Exception as exc:
            self.record_error(symbol, "trades", exc)

    def fetch_ohlcv(self, symbol: str, timeframe: str) -> None:
        name = f"ohlcv_{timeframe}"

        try:
            candles = self.exchange.fetch_ohlcv(
                symbol,
                timeframe=timeframe,
                limit=2,
            )

            if candles:
                self.symbol_state[symbol][name] = candles[-1]
                self.record_ok(symbol, name)

        except Exception as exc:
            self.record_error(symbol, name, exc)

    def maybe_refresh_ohlcv(self) -> None:
        now_mono = time.monotonic()

        if (
            self.last_ohlcv_refresh_monotonic > 0
            and now_mono - self.last_ohlcv_refresh_monotonic
            < OHLCV_REFRESH_SECONDS
        ):
            return

        for symbol in SYMBOLS:
            if STOP_REQUESTED:
                return
            self.fetch_ohlcv(symbol, "1m")
            self.fetch_ohlcv(symbol, "5m")

        self.last_ohlcv_refresh_monotonic = time.monotonic()

    def prune_trades(self, symbol: str) -> List[Dict[str, Any]]:
        cutoff_ms = int((time.time() - TRADE_WINDOW_SECONDS) * 1000)
        dq = self.trades[symbol]

        while dq and int(dq[0].get("timestamp") or 0) < cutoff_ms:
            dq.popleft()

        return list(dq)

    def make_sample(self, symbol: str) -> Optional[Dict[str, Any]]:
        state = self.symbol_state[symbol]
        book = state.get("orderbook") or {}
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        c1 = state.get("ohlcv_1m")
        c5 = state.get("ohlcv_5m")

        if not (bids and asks and c1 and c5):
            return None

        bid = finite_float(bids[0][0]) if len(bids[0]) >= 1 else 0.0
        ask = finite_float(asks[0][0]) if len(asks[0]) >= 1 else 0.0

        if not (bid > 0 and ask > 0):
            return None

        mid = (bid + ask) / 2.0
        last = finite_float(state.get("last_trade_price"), mid)

        if last <= 0:
            last = mid

        spread_pct = (
            (ask - bid) / mid * 100.0
            if mid > 0
            else 0.0
        )

        bid_value = sum(
            finite_float(level[0]) * finite_float(level[1])
            for level in bids[:ORDERBOOK_LEVELS]
            if len(level) >= 2
        )

        ask_value = sum(
            finite_float(level[0]) * finite_float(level[1])
            for level in asks[:ORDERBOOK_LEVELS]
            if len(level) >= 2
        )

        book_total = bid_value + ask_value

        book_imbalance = (
            (bid_value - ask_value) / book_total
            if book_total > 0
            else 0.0
        )

        recent = self.prune_trades(symbol)

        buys = [
            trade
            for trade in recent
            if trade.get("side") == "buy"
        ]

        sells = [
            trade
            for trade in recent
            if trade.get("side") == "sell"
        ]

        buy_value = sum(
            trade["price"] * trade["amount"]
            for trade in buys
        )

        sell_value = sum(
            trade["price"] * trade["amount"]
            for trade in sells
        )

        trade_total = buy_value + sell_value

        trade_imbalance = (
            (buy_value - sell_value) / trade_total
            if trade_total > 0
            else 0.0
        )

        return {
            "timestamp_utc": iso_now(),
            "symbol": symbol,
            "bid": round(bid, 12),
            "ask": round(ask, 12),
            "last": round(last, 12),
            "spread_pct": round(spread_pct, 6),
            "book_bid_value_top10": round(bid_value, 6),
            "book_ask_value_top10": round(ask_value, 6),
            "book_imbalance": round(book_imbalance, 6),
            "trade_count_60s": len(recent),
            "buy_count_60s": len(buys),
            "sell_count_60s": len(sells),
            "buy_value_60s": round(buy_value, 6),
            "sell_value_60s": round(sell_value, 6),
            "trade_imbalance_60s": round(trade_imbalance, 6),
            "close_1m": finite_float(c1[4]) if len(c1) > 4 else 0.0,
            "volume_1m": finite_float(c1[5]) if len(c1) > 5 else 0.0,
            "close_5m": finite_float(c5[4]) if len(c5) > 4 else 0.0,
            "volume_5m": finite_float(c5[5]) if len(c5) > 5 else 0.0,
        }

    def save_state(self) -> None:
        compact_symbols = {}

        for symbol, state in self.symbol_state.items():
            compact_symbols[symbol] = {
                "last_update": state.get("last_update") or {},
                "errors": state.get("errors") or {},
                "trade_buffer_size": len(self.trades[symbol]),
            }

        atomic_write_json(
            STATE_FILE,
            {
                "version": VERSION,
                "mode": MODE,
                "started_at": self.started_at,
                "last_update": iso_now(),
                "symbols": SYMBOLS,
                "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
                "orderbook_levels": ORDERBOOK_LEVELS,
                "trade_window_seconds": TRADE_WINDOW_SECONDS,
                "ohlcv_refresh_seconds": OHLCV_REFRESH_SECONDS,
                "samples_written": self.samples_written,
                "cycles": self.cycles,
                "errors_total": dict(self.errors_total),
                "symbol_status": compact_symbols,
                "architecture": {
                    "websocket_streams_total": 0,
                    "transport": "public_rest_only",
                    "sequential": True,
                },
                "safety": {
                    "orders_possible": False,
                    "private_api": False,
                    "bot_changed": False,
                    "config_changed": False,
                },
            },
        )

    def run_cycle(self) -> None:
        cycle_started = time.monotonic()

        self.maybe_refresh_ohlcv()

        cycle_rows = 0

        for symbol in SYMBOLS:
            if STOP_REQUESTED:
                break

            self.fetch_orderbook(symbol)
            self.fetch_recent_trades(symbol)

            row = self.make_sample(symbol)

            if row is not None:
                append_csv(row)
                self.samples_written += 1
                cycle_rows += 1

        self.cycles += 1
        self.save_state()

        elapsed = time.monotonic() - cycle_started

        print(
            f"{iso_now()} | "
            f"samples={cycle_rows}/{len(SYMBOLS)} "
            f"| totaal={self.samples_written} "
            f"| cycle={elapsed:.2f}s",
            flush=True,
        )

    def run(self, duration_seconds: int = 0) -> None:
        ensure_csv_header()

        started = time.monotonic()

        while not STOP_REQUESTED:
            cycle_started = time.monotonic()

            self.run_cycle()

            if duration_seconds > 0:
                elapsed_total = time.monotonic() - started
                if elapsed_total >= duration_seconds:
                    break

            elapsed_cycle = time.monotonic() - cycle_started
            sleep_for = max(
                0.5,
                SAMPLE_INTERVAL_SECONDS - elapsed_cycle,
            )

            end_sleep = time.monotonic() + sleep_for

            while not STOP_REQUESTED and time.monotonic() < end_sleep:
                time.sleep(
                    min(
                        0.5,
                        max(0.0, end_sleep - time.monotonic()),
                    )
                )

        self.save_state()


def request_stop(*_: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def self_test() -> None:
    assert VERSION == "1.2"
    assert MODE == "READ_ONLY_EARLY_ENTRY_COLLECTOR_REST_LOW_MEMORY"
    assert SYMBOLS == [
        "BTC/EUR",
        "ETH/EUR",
        "SOL/EUR",
        "XRP/EUR",
        "ADA/EUR",
    ]
    assert SAMPLE_INTERVAL_SECONDS == 15
    assert ORDERBOOK_LEVELS == 10
    assert TRADE_WINDOW_SECONDS == 60
    assert OHLCV_REFRESH_SECONDS == 60
    assert MAX_TRADE_EVENTS_PER_SYMBOL == 200
    assert str(CSV_FILE).startswith("/var/data/")
    assert str(STATE_FILE).startswith("/var/data/")

    print("EARLY_ENTRY_COLLECTOR_V1_2_SELF_TEST_OK")
    print("Transport       : publieke REST-only")
    print("Sequentieel     : JA")
    print("WebSockets      : 0")
    print("Orders mogelijk : NEE")
    print("Private API     : NEE")
    print("Bot/config      : ONGEWIJZIGD")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--duration-seconds", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    args = parse_args()

    if args.self_test:
        self_test()
        return 0

    collector = Collector()
    collector.run(max(0, int(args.duration_seconds)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
