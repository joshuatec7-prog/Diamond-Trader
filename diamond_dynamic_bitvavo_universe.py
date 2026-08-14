#!/usr/bin/env python3
# Diamond Trader Dynamic Bitvavo Universe v1.0
#
# Publieke, read-only scanner over ALLE actieve EUR-markten op Bitvavo.
# Geen API-key, geen private API, geen orders en geen live/config wijziging.

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List


VERSION = "1.0"
BASE_URL = "https://api.bitvavo.com/v2"
DATA = Path("/var/data")
DEFAULT_OUTPUT = DATA / "diamond_dynamic_universe.json"

CORE_BASES = {"BTC", "ETH", "SOL", "XRP", "ADA"}

DEFAULT_MAX_SPREAD_PCT = 0.25
DEFAULT_MIN_VOLUME_QUOTE = 100_000.0
DEFAULT_EVENT_MOVE_PCT = 8.0
DEFAULT_EVENT_RANGE_PCT = 12.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def api_get(path: str, timeout: int = 20) -> Any:
    url = BASE_URL + path
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Diamond-Trader-Dynamic-Universe/1.0",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def spread_pct(bid: float, ask: float) -> float:
    if bid <= 0 or ask <= 0 or ask < bid:
        return 999.0
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return 999.0
    return ((ask - bid) / mid) * 100.0


def change_pct(open_price: float, last: float) -> float:
    if open_price <= 0:
        return 0.0
    return ((last - open_price) / open_price) * 100.0


def range_pct(low: float, high: float) -> float:
    if low <= 0 or high < low:
        return 0.0
    return ((high - low) / low) * 100.0


def rank_score(
    *,
    volume_quote: float,
    spread: float,
    abs_move: float,
    day_range: float,
) -> float:
    # Alleen voor research-ranking. Geen handelsbesluit.
    volume_component = max(0.0, math.log10(max(volume_quote, 1.0))) * 10.0
    spread_penalty = min(40.0, max(0.0, spread) * 40.0)
    movement_component = min(20.0, abs_move)
    range_component = min(10.0, day_range / 2.0)
    return round(
        volume_component - spread_penalty + movement_component + range_component,
        4,
    )


def build_universe(
    markets_payload: Any,
    ticker_payload: Any,
    *,
    max_spread_pct: float,
    min_volume_quote: float,
    event_move_pct: float,
    event_range_pct: float,
) -> Dict[str, Any]:
    markets = normalize_rows(markets_payload)
    tickers = normalize_rows(ticker_payload)

    ticker_by_market = {
        str(row.get("market") or ""): row
        for row in tickers
        if row.get("market")
    }

    active_eur = []
    for market in markets:
        market_name = str(market.get("market") or "")
        quote = str(market.get("quote") or "").upper()
        status = str(market.get("status") or "").lower()

        if quote != "EUR" or status != "trading":
            continue

        base = str(market.get("base") or "").upper()
        ticker = ticker_by_market.get(market_name, {})

        bid = to_float(ticker.get("bid"))
        ask = to_float(ticker.get("ask"))
        open_price = to_float(ticker.get("open"))
        last = to_float(ticker.get("last"))
        high = to_float(ticker.get("high"))
        low = to_float(ticker.get("low"))
        volume_quote = to_float(ticker.get("volumeQuote"))

        spr = spread_pct(bid, ask)
        move = change_pct(open_price, last)
        day_range = range_pct(low, high)

        spread_ok = spr <= max_spread_pct
        volume_ok = volume_quote >= min_volume_quote
        market_event = (
            abs(move) >= event_move_pct
            or day_range >= event_range_pct
        )

        if spread_ok and volume_ok:
            liquidity_status = "PASS"
        elif spr <= max_spread_pct * 2 and volume_quote >= min_volume_quote / 4:
            liquidity_status = "WATCH"
        else:
            liquidity_status = "LOW"

        row = {
            "market": market_name,
            "base": base,
            "quote": quote,
            "core_coin": base in CORE_BASES,
            "status": status,
            "last": last,
            "bid": bid,
            "ask": ask,
            "spread_pct": round(spr, 6),
            "volume_quote_24h": round(volume_quote, 4),
            "change_24h_pct": round(move, 6),
            "range_24h_pct": round(day_range, 6),
            "spread_ok": spread_ok,
            "volume_ok": volume_ok,
            "liquidity_status": liquidity_status,
            "market_event_candidate": market_event,
            "rank_score": rank_score(
                volume_quote=volume_quote,
                spread=spr,
                abs_move=abs(move),
                day_range=day_range,
            ),
            "live_eligible": False,
            "research_only": True,
        }
        active_eur.append(row)

    active_eur.sort(
        key=lambda row: (
            row["liquidity_status"] == "PASS",
            row["market_event_candidate"],
            row["rank_score"],
        ),
        reverse=True,
    )

    passing = [row for row in active_eur if row["liquidity_status"] == "PASS"]
    events = [row for row in active_eur if row["market_event_candidate"]]

    return {
        "version": VERSION,
        "generated_at": now_iso(),
        "source": "Bitvavo public REST",
        "research_only": True,
        "private_api_used": False,
        "orders_used": False,
        "automatic_live_change": False,
        "core_markets_unchanged": sorted(f"{base}-EUR" for base in CORE_BASES),
        "thresholds": {
            "max_spread_pct": max_spread_pct,
            "min_volume_quote_24h": min_volume_quote,
            "market_event_move_pct": event_move_pct,
            "market_event_range_pct": event_range_pct,
        },
        "counts": {
            "active_eur_markets": len(active_eur),
            "liquidity_pass": len(passing),
            "market_event_candidates": len(events),
        },
        "top_candidates": active_eur[:25],
        "market_event_candidates": events[:25],
        "all_active_eur_markets": active_eur,
    }


def atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass


def print_report(result: Dict[str, Any]) -> None:
    counts = result["counts"]
    print("=" * 78)
    print(f" DIAMOND DYNAMIC BITVAVO UNIVERSE v{VERSION}")
    print("=" * 78)
    print(f"Actieve EUR-markten : {counts['active_eur_markets']}")
    print(f"Liquiditeit PASS    : {counts['liquidity_pass']}")
    print(f"Event-kandidaten    : {counts['market_event_candidates']}")
    print("Research-only       : JA")
    print("Core 5 gewijzigd    : NEE")

    print("\n=== TOP KANDIDATEN ===")
    for row in result["top_candidates"][:12]:
        tag = "EVENT" if row["market_event_candidate"] else row["liquidity_status"]
        core = " CORE" if row["core_coin"] else ""
        print(
            f"{row['market']:<12} "
            f"vol=€{row['volume_quote_24h']:>11,.0f} "
            f"spr={row['spread_pct']:>7.3f}% "
            f"24h={row['change_24h_pct']:>+7.2f}% "
            f"[{tag}{core}]"
        )

    if result["market_event_candidates"]:
        print("\n=== MARKT-EVENT KANDIDATEN ===")
        for row in result["market_event_candidates"][:10]:
            print(
                f"{row['market']:<12} "
                f"24h={row['change_24h_pct']:>+7.2f}% "
                f"range={row['range_24h_pct']:>7.2f}% "
                f"vol=€{row['volume_quote_24h']:,.0f}"
            )

    print("\nNieuws bevestigd     : NEE - dat komt in Lijst 4 punt 2")
    print("Live toelating       : NEE")
    print("Orders/private API   : NEE")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--max-spread-pct",
        type=float,
        default=DEFAULT_MAX_SPREAD_PCT,
    )
    parser.add_argument(
        "--min-volume-quote",
        type=float,
        default=DEFAULT_MIN_VOLUME_QUOTE,
    )
    parser.add_argument(
        "--event-move-pct",
        type=float,
        default=DEFAULT_EVENT_MOVE_PCT,
    )
    parser.add_argument(
        "--event-range-pct",
        type=float,
        default=DEFAULT_EVENT_RANGE_PCT,
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        markets = api_get("/markets")
        tickers = api_get("/ticker/24h")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print("=" * 78)
        print(f" DIAMOND DYNAMIC BITVAVO UNIVERSE v{VERSION}")
        print("=" * 78)
        print(f"STATUS : FAIL_PUBLIC_DATA | {type(exc).__name__}")
        print("Orders/private API : NEE")
        return 2

    result = build_universe(
        markets,
        tickers,
        max_spread_pct=max(0.0, args.max_spread_pct),
        min_volume_quote=max(0.0, args.min_volume_quote),
        event_move_pct=max(0.0, args.event_move_pct),
        event_range_pct=max(0.0, args.event_range_pct),
    )
    atomic_json(Path(args.output), result)
    print_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
