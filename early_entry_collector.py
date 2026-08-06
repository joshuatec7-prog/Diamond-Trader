#!/usr/bin/env python3
"""
Diamond Trader Early Entry Collector v1.0

Doel:
- Alleen publieke Bitvavo-marktdata verzamelen.
- Geen orders, geen private API, geen bot/config-wijzigingen.
- 5 munten via CCXT Pro WebSocket volgen.
- Alleen compacte afgeleide metrics opslaan; geen volledige orderboeken.

Databronnen:
- ticker
- orderboek top 10
- trades
- 1m OHLCV
- 5m OHLCV

Uitvoer:
- /var/data/diamond_early_entry/early_entry_samples.csv
- /var/data/diamond_early_entry/early_entry_state.json

Gebruik:
  python3 early_entry_collector.py --self-test
  python3 early_entry_collector.py --duration-seconds 120
  python3 early_entry_collector.py
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import signal
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

import ccxt.pro as ccxtpro


VERSION = "1.0"
MODE = "READ_ONLY_EARLY_ENTRY_COLLECTOR"

SYMBOLS = [
    "BTC/EUR",
    "ETH/EUR",
    "SOL/EUR",
    "XRP/EUR",
    "ADA/EUR",
]

DATA_DIR = Path("/var/data/diamond_early_entry")
CSV_FILE = DATA_DIR / "early_entry_samples.csv"
STATE_FILE = DATA_DIR / "early_entry_state.json"

SAMPLE_INTERVAL_SECONDS = 10
ORDERBOOK_LEVELS = 10
TRADE_WINDOW_SECONDS = 60
RECONNECT_DELAY_SECONDS = 3
MAX_TRADE_EVENTS_PER_SYMBOL = 500

STOP_REQUESTED = False


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

    fields = [
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
    with CSV_FILE.open("w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=fields).writeheader()


def append_csv(row: Dict[str, Any]) -> None:
    ensure_csv_header()
    fields = [
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
    with CSV_FILE.open("a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=fields).writerow(
            {k: row.get(k, "") for k in fields}
        )


def make_symbol_state() -> Dict[str, Any]:
    return {
        "ticker": {},
        "orderbook": {},
        "ohlcv_1m": None,
        "ohlcv_5m": None,
        "last_stream_update": {},
        "errors": {},
    }


class Collector:
    def __init__(self) -> None:
        self.exchange = ccxtpro.bitvavo({"enableRateLimit": True})
        self.symbol_state: Dict[str, Dict[str, Any]] = {
            s: make_symbol_state() for s in SYMBOLS
        }
        self.trades: Dict[str, Deque[Dict[str, Any]]] = {
            s: deque(maxlen=MAX_TRADE_EVENTS_PER_SYMBOL) for s in SYMBOLS
        }
        self.started_at = iso_now()
        self.samples_written = 0
        self.reconnects = defaultdict(int)

    def record_stream_ok(self, symbol: str, stream: str) -> None:
        self.symbol_state[symbol]["last_stream_update"][stream] = iso_now()
        self.symbol_state[symbol]["errors"].pop(stream, None)

    def record_stream_error(self, symbol: str, stream: str, exc: Exception) -> None:
        self.symbol_state[symbol]["errors"][stream] = (
            f"{type(exc).__name__}: {exc}"
        )
        self.reconnects[f"{symbol}:{stream}"] += 1

    async def watch_ticker(self, symbol: str) -> None:
        while not STOP_REQUESTED:
            try:
                ticker = await self.exchange.watch_ticker(symbol)
                self.symbol_state[symbol]["ticker"] = ticker or {}
                self.record_stream_ok(symbol, "ticker")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.record_stream_error(symbol, "ticker", exc)
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    async def watch_orderbook(self, symbol: str) -> None:
        while not STOP_REQUESTED:
            try:
                book = await self.exchange.watch_order_book(
                    symbol,
                    limit=ORDERBOOK_LEVELS,
                )
                self.symbol_state[symbol]["orderbook"] = {
                    "bids": list((book or {}).get("bids") or [])[:ORDERBOOK_LEVELS],
                    "asks": list((book or {}).get("asks") or [])[:ORDERBOOK_LEVELS],
                }
                self.record_stream_ok(symbol, "orderbook")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.record_stream_error(symbol, "orderbook", exc)
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    async def watch_trades(self, symbol: str) -> None:
        seen_ids: Deque[str] = deque(maxlen=1000)
        seen_set = set()

        while not STOP_REQUESTED:
            try:
                trades = await self.exchange.watch_trades(symbol)
                now_ms = int(time.time() * 1000)

                for trade in trades or []:
                    trade_id = str(
                        trade.get("id")
                        or f"{trade.get('timestamp')}:{trade.get('price')}:{trade.get('amount')}:{trade.get('side')}"
                    )
                    if trade_id in seen_set:
                        continue

                    if len(seen_ids) == seen_ids.maxlen:
                        old = seen_ids.popleft()
                        seen_set.discard(old)

                    seen_ids.append(trade_id)
                    seen_set.add(trade_id)

                    self.trades[symbol].append(
                        {
                            "timestamp": int(trade.get("timestamp") or now_ms),
                            "price": finite_float(trade.get("price")),
                            "amount": finite_float(trade.get("amount")),
                            "side": str(trade.get("side") or "").lower(),
                        }
                    )

                self.record_stream_ok(symbol, "trades")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.record_stream_error(symbol, "trades", exc)
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    async def watch_ohlcv(self, symbol: str, timeframe: str) -> None:
        key = f"ohlcv_{timeframe}"
        while not STOP_REQUESTED:
            try:
                candles = await self.exchange.watch_ohlcv(symbol, timeframe)
                if candles:
                    self.symbol_state[symbol][key] = candles[-1]
                self.record_stream_ok(symbol, timeframe)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.record_stream_error(symbol, timeframe, exc)
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    def prune_trades(self, symbol: str) -> List[Dict[str, Any]]:
        cutoff_ms = int((time.time() - TRADE_WINDOW_SECONDS) * 1000)
        dq = self.trades[symbol]
        while dq and int(dq[0].get("timestamp") or 0) < cutoff_ms:
            dq.popleft()
        return list(dq)

    def make_sample(self, symbol: str) -> Optional[Dict[str, Any]]:
        state = self.symbol_state[symbol]
        ticker = state.get("ticker") or {}
        book = state.get("orderbook") or {}
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        c1 = state.get("ohlcv_1m")
        c5 = state.get("ohlcv_5m")

        bid = finite_float(ticker.get("bid"))
        ask = finite_float(ticker.get("ask"))
        last = finite_float(ticker.get("last"))

        # Voor een bruikbare sample moeten ticker, orderboek en beide candles
        # minstens één keer data hebben geleverd.
        if not (bid > 0 and ask > 0 and bids and asks and c1 and c5):
            return None

        mid = (bid + ask) / 2.0
        spread_pct = ((ask - bid) / mid * 100.0) if mid > 0 else 0.0

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
            (bid_value - ask_value) / book_total if book_total > 0 else 0.0
        )

        recent = self.prune_trades(symbol)
        buys = [t for t in recent if t.get("side") == "buy"]
        sells = [t for t in recent if t.get("side") == "sell"]

        buy_value = sum(t["price"] * t["amount"] for t in buys)
        sell_value = sum(t["price"] * t["amount"] for t in sells)
        trade_total = buy_value + sell_value
        trade_imbalance = (
            (buy_value - sell_value) / trade_total if trade_total > 0 else 0.0
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
                "last_stream_update": state.get("last_stream_update") or {},
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
                "samples_written": self.samples_written,
                "reconnects": dict(self.reconnects),
                "symbol_status": compact_symbols,
                "safety": {
                    "orders_possible": False,
                    "private_api": False,
                    "bot_changed": False,
                    "config_changed": False,
                },
            },
        )

    async def sample_loop(self) -> None:
        while not STOP_REQUESTED:
            cycle_rows = 0

            for symbol in SYMBOLS:
                row = self.make_sample(symbol)
                if row is not None:
                    append_csv(row)
                    self.samples_written += 1
                    cycle_rows += 1

            self.save_state()

            print(
                f"{iso_now()} | samples={cycle_rows}/{len(SYMBOLS)} "
                f"| totaal={self.samples_written}",
                flush=True,
            )

            await asyncio.sleep(SAMPLE_INTERVAL_SECONDS)

    async def run(self, duration_seconds: int = 0) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ensure_csv_header()

        tasks = []

        for symbol in SYMBOLS:
            tasks.extend(
                [
                    asyncio.create_task(self.watch_ticker(symbol)),
                    asyncio.create_task(self.watch_orderbook(symbol)),
                    asyncio.create_task(self.watch_trades(symbol)),
                    asyncio.create_task(self.watch_ohlcv(symbol, "1m")),
                    asyncio.create_task(self.watch_ohlcv(symbol, "5m")),
                ]
            )

        sampler = asyncio.create_task(self.sample_loop())
        tasks.append(sampler)

        try:
            if duration_seconds > 0:
                await asyncio.sleep(duration_seconds)
            else:
                while not STOP_REQUESTED:
                    await asyncio.sleep(1)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.save_state()
            await self.exchange.close()


def request_stop(*_: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def self_test() -> None:
    assert VERSION == "1.0"
    assert MODE == "READ_ONLY_EARLY_ENTRY_COLLECTOR"
    assert SYMBOLS == [
        "BTC/EUR",
        "ETH/EUR",
        "SOL/EUR",
        "XRP/EUR",
        "ADA/EUR",
    ]
    assert SAMPLE_INTERVAL_SECONDS == 10
    assert ORDERBOOK_LEVELS == 10
    assert TRADE_WINDOW_SECONDS == 60
    assert str(CSV_FILE).startswith("/var/data/")
    assert str(STATE_FILE).startswith("/var/data/")
    print("EARLY_ENTRY_COLLECTOR_SELF_TEST_OK")
    print("Orders mogelijk : NEE")
    print("Private API     : NEE")
    print("Bot/config      : ONGEWIJZIGD")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--duration-seconds", type=int, default=0)
    return parser.parse_args()


async def async_main() -> int:
    args = parse_args()

    if args.self_test:
        self_test()
        return 0

    collector = Collector()
    await collector.run(max(0, int(args.duration_seconds)))
    return 0


def main() -> int:
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
