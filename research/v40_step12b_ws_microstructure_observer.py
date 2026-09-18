#!/usr/bin/env python3
"""Step 12B public WebSocket microstructure observer.

OBSERVE ONLY. PUBLIC BITVAVO MARKET DATA ONLY. NO AUTHENTICATION. NO ORDERS.

This module is deliberately separate from the frozen Step 12A selector. It captures
extra evidence for one already-chosen market:
- order-book pressure / near-book imbalance;
- taker trade flow (buy vs sell quote volume);
- top-of-book spread level and stability;
- message continuity / book resync diagnostics.

It never emits ALLOW/REDUCE/BLOCK and cannot change PAPER/runtime/live behavior.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

WS_URL = "wss://ws.bitvavo.com/v2"
BOOK_DEPTH = 100
NEAR_BOOK_BAND_PCT = 0.50
SAMPLE_INTERVAL_SECONDS = 1.0


class BookDesync(RuntimeError):
    """Raised when a WebSocket book nonce gap means local L2 is no longer reliable."""


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return v if math.isfinite(v) else default


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    xs = sorted(float(x) for x in values)
    if len(xs) == 1:
        return xs[0]
    pos = min(1.0, max(0.0, q)) * (len(xs) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    weight = pos - lo
    return xs[lo] * (1.0 - weight) + xs[hi] * weight


@dataclass
class FlowStats:
    trade_count: int = 0
    buy_count: int = 0
    sell_count: int = 0
    buy_quote: float = 0.0
    sell_quote: float = 0.0

    def add_trade(self, event: dict[str, Any]) -> None:
        price = _finite(event.get("price"))
        amount = _finite(event.get("amount"))
        side = str(event.get("side", "")).lower()
        if price <= 0.0 or amount <= 0.0 or side not in {"buy", "sell"}:
            return
        quote = price * amount
        self.trade_count += 1
        if side == "buy":
            self.buy_count += 1
            self.buy_quote += quote
        else:
            self.sell_count += 1
            self.sell_quote += quote

    def summary(self) -> dict[str, Any]:
        total = self.buy_quote + self.sell_quote
        imbalance = (self.buy_quote - self.sell_quote) / total if total > 0.0 else 0.0
        return {
            "trade_count": self.trade_count,
            "taker_buy_count": self.buy_count,
            "taker_sell_count": self.sell_count,
            "taker_buy_quote": round(self.buy_quote, 8),
            "taker_sell_quote": round(self.sell_quote, 8),
            "taker_flow_imbalance": round(imbalance, 8),
            "taker_sell_share_pct": round(self.sell_quote / total * 100.0, 6) if total > 0 else None,
        }


@dataclass
class OrderBookState:
    market: str
    nonce: int
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)

    @staticmethod
    def _levels(rows: Any) -> dict[float, float]:
        result: dict[float, float] = {}
        if not isinstance(rows, list):
            return result
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            price = _finite(row[0])
            size = _finite(row[1])
            if price > 0.0 and size > 0.0:
                result[price] = size
        return result

    @classmethod
    def from_snapshot(cls, response: dict[str, Any]) -> "OrderBookState":
        market = str(response.get("market", "")).upper()
        nonce = int(response.get("nonce", -1))
        bids = cls._levels(response.get("bids"))
        asks = cls._levels(response.get("asks"))
        if not market or nonce < 0 or not bids or not asks:
            raise ValueError("ongeldige WebSocket getBook snapshot")
        state = cls(market=market, nonce=nonce, bids=bids, asks=asks)
        state.metrics()
        return state

    @staticmethod
    def _apply_levels(target: dict[float, float], rows: Any) -> None:
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            price = _finite(row[0])
            size = _finite(row[1], default=-1.0)
            if price <= 0.0 or size < 0.0:
                continue
            if size == 0.0:
                target.pop(price, None)
            else:
                target[price] = size

    def apply_event(self, event: dict[str, Any]) -> str:
        if str(event.get("market", "")).upper() != self.market:
            return "other_market"
        nonce = int(event.get("nonce", -1))
        if nonce <= self.nonce:
            return "stale"
        if nonce != self.nonce + 1:
            raise BookDesync(f"nonce gap {self.nonce}->{nonce}")
        self._apply_levels(self.bids, event.get("bids"))
        self._apply_levels(self.asks, event.get("asks"))
        self.nonce = nonce
        self.metrics()
        return "applied"

    def metrics(self) -> dict[str, float]:
        if not self.bids or not self.asks:
            raise ValueError("orderboek leeg")
        bid = max(self.bids)
        ask = min(self.asks)
        if ask < bid:
            raise ValueError("gekruist orderboek")
        mid = (bid + ask) / 2.0
        band = NEAR_BOOK_BAND_PCT / 100.0
        bid_depth = sum(p * s for p, s in self.bids.items() if p >= mid * (1.0 - band))
        ask_depth = sum(p * s for p, s in self.asks.items() if p <= mid * (1.0 + band))
        total = bid_depth + ask_depth
        imbalance = (bid_depth - ask_depth) / total if total > 0.0 else 0.0
        return {
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread_pct": (ask / bid - 1.0) * 100.0,
            "near_bid_depth_quote": bid_depth,
            "near_ask_depth_quote": ask_depth,
            "near_book_imbalance": imbalance,
        }


def summarize_samples(samples: list[dict[str, float]]) -> dict[str, Any]:
    spreads = [_finite(x.get("spread_pct")) for x in samples]
    imbalances = [_finite(x.get("near_book_imbalance")) for x in samples]
    mids = [_finite(x.get("mid")) for x in samples if _finite(x.get("mid")) > 0.0]
    return {
        "sample_count": len(samples),
        "spread_median_pct": round(statistics.median(spreads), 8) if spreads else None,
        "spread_p90_pct": round(_percentile(spreads, 0.90), 8) if spreads else None,
        "spread_max_pct": round(max(spreads), 8) if spreads else None,
        "spread_range_pct": round(max(spreads) - min(spreads), 8) if spreads else None,
        "book_imbalance_median": round(statistics.median(imbalances), 8) if imbalances else None,
        "book_imbalance_p10": round(_percentile(imbalances, 0.10), 8) if imbalances else None,
        "book_imbalance_p90": round(_percentile(imbalances, 0.90), 8) if imbalances else None,
        "bid_heavy_share_pct": round(sum(x > 0.10 for x in imbalances) / len(imbalances) * 100.0, 6) if imbalances else None,
        "ask_heavy_share_pct": round(sum(x < -0.10 for x in imbalances) / len(imbalances) * 100.0, 6) if imbalances else None,
        "mid_change_pct": round((mids[-1] / mids[0] - 1.0) * 100.0, 8) if len(mids) >= 2 else 0.0,
    }


def capture_market(market: str, *, seconds: float = 60.0, depth: int = BOOK_DEPTH) -> dict[str, Any]:
    from websockets.sync.client import connect  # type: ignore
    from websockets.exceptions import ConnectionClosed  # type: ignore

    market = str(market).upper().strip()
    if not market.endswith("-EUR"):
        raise ValueError("Step 12B verwacht een EUR-markt")
    if seconds <= 0.0:
        raise ValueError("seconds moet positief zijn")

    request_id = 12001
    flow = FlowStats()
    book: OrderBookState | None = None
    pending_books: list[dict[str, Any]] = []
    samples: list[dict[str, float]] = []
    message_counts: dict[str, int] = {}
    desync_count = 0
    snapshot_count = 0
    last_sample = 0.0
    capture_start: float | None = None
    started_wall_ms = int(time.time() * 1000)

    def send_snapshot(ws: Any) -> None:
        nonlocal request_id
        request_id += 1
        ws.send(json.dumps({"action": "getBook", "requestId": request_id, "market": market, "depth": int(depth)}))

    with connect(WS_URL, open_timeout=15, close_timeout=5) as ws:
        ws.send(json.dumps({
            "action": "subscribe",
            "channels": [
                {"name": "trades", "markets": [market]},
                {"name": "book", "markets": [market]},
            ],
        }))
        send_snapshot(ws)
        hard_deadline = time.monotonic() + max(seconds + 30.0, 45.0)

        while time.monotonic() < hard_deadline:
            now = time.monotonic()
            if capture_start is not None and now - capture_start >= seconds:
                break
            try:
                raw = ws.recv(timeout=1.0)
            except TimeoutError:
                if book is not None and capture_start is not None and now - last_sample >= SAMPLE_INTERVAL_SECONDS:
                    samples.append(book.metrics())
                    last_sample = now
                continue
            except ConnectionClosed as exc:
                raise RuntimeError(f"WebSocket gesloten tijdens observe-only capture: {exc}") from exc

            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            try:
                event = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                message_counts["invalid_json"] = message_counts.get("invalid_json", 0) + 1
                continue
            if not isinstance(event, dict):
                continue

            event_name = str(event.get("event") or event.get("action") or "other")
            message_counts[event_name] = message_counts.get(event_name, 0) + 1

            response = event.get("response")
            if event.get("action") == "getBook" and isinstance(response, dict):
                fresh = OrderBookState.from_snapshot(response)
                if fresh.market != market:
                    continue
                book = fresh
                snapshot_count += 1
                buffered = sorted(
                    (x for x in pending_books if int(x.get("nonce", -1)) > book.nonce),
                    key=lambda x: int(x.get("nonce", -1)),
                )
                pending_books.clear()
                try:
                    for update in buffered:
                        book.apply_event(update)
                except BookDesync:
                    desync_count += 1
                    book = None
                    send_snapshot(ws)
                    continue
                if capture_start is None:
                    capture_start = time.monotonic()
                    last_sample = capture_start
                    samples.append(book.metrics())
                continue

            if event.get("event") == "trade" and str(event.get("market", "")).upper() == market:
                if capture_start is not None:
                    flow.add_trade(event)
                continue

            if event.get("event") == "book" and str(event.get("market", "")).upper() == market:
                if book is None:
                    pending_books.append(event)
                    if len(pending_books) > 5000:
                        pending_books = pending_books[-2500:]
                    continue
                try:
                    status = book.apply_event(event)
                except BookDesync:
                    desync_count += 1
                    book = None
                    pending_books = [event]
                    send_snapshot(ws)
                    continue
                if status == "applied" and capture_start is not None:
                    now = time.monotonic()
                    if now - last_sample >= SAMPLE_INTERVAL_SECONDS:
                        samples.append(book.metrics())
                        last_sample = now

    if capture_start is None or book is None:
        raise RuntimeError("geen bruikbare WebSocket orderboek-snapshot binnen capturevenster")
    if not samples:
        samples.append(book.metrics())

    ended_wall_ms = int(time.time() * 1000)
    return {
        "version": "v40-step12b-ws-microstructure-observer-1",
        "mode": "PUBLIC_WEBSOCKET_OBSERVE_ONLY",
        "market": market,
        "requested_seconds": float(seconds),
        "captured_seconds": round((ended_wall_ms - started_wall_ms) / 1000.0, 3),
        "started_at_ms": started_wall_ms,
        "ended_at_ms": ended_wall_ms,
        "book_depth": int(depth),
        "near_book_band_pct": NEAR_BOOK_BAND_PCT,
        "flow": flow.summary(),
        "book": summarize_samples(samples),
        "continuity": {
            "last_nonce": int(book.nonce),
            "book_resyncs": desync_count,
            "snapshots": snapshot_count,
            "message_counts": message_counts,
        },
        "decision_influence": "NONE_OBSERVE_ONLY",
        "execution_enabled": False,
        "authenticated_api_used": False,
        "order_actions": 0,
        "active_paper_changed": False,
        "live_orders_possible": False,
        "notes": [
            "Taker side is measured directly from Bitvavo public trades events.",
            "Order-book pressure uses the local nonce-checked L2 book within ±0.5% of mid.",
            "These fields are evidence only and do not create or veto a trade in Step 12B.",
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Step 12B Bitvavo WebSocket microstructure observer")
    ap.add_argument("market")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--depth", type=int, default=BOOK_DEPTH)
    ap.add_argument("--output", default="v40_step12b_ws_microstructure_observation.json")
    args = ap.parse_args()
    report = capture_market(args.market, seconds=args.seconds, depth=args.depth)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
