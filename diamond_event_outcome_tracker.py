#!/usr/bin/env python3
"""
Diamond Trader Event / Universe Outcome Tracker v1.0

Doel
----
Prospectief meten wat er NA Lijst-4 researchsignalen gebeurt.

Volgt vanaf het moment dat dit script voor het eerst draait:
- FUSED_STRONG
- FUSED
- FUSED_CONFLICT
- NEWS_WATCH
- MARKET_WATCH

Per nieuw signaal wordt de actuele Bitvavo-prijs vastgelegd. Daarna worden
read-only checkpoints gevuld rond:
- 1 uur
- 4 uur
- 12 uur

Zo kunnen we later objectief zien of nieuws, markt-events, fusion en externe
bevestiging werkelijk voorspellende waarde toevoegen.

Bronnen
-------
/var/data/diamond_event_market_fusion.json
/var/data/diamond_multi_exchange_confirmation.json

Publieke prijsbron
------------------
GET https://api.bitvavo.com/v2/ticker/price
Geen API-key nodig. Geen orders. Geen private API.

State
-----
/var/data/diamond_event_outcome_tracker_state.json

Rapport
-------
/var/data/diamond_event_outcome_tracker_report.json
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


VERSION = "1.0"

DATA = Path(os.getenv("DIAMOND_DATA_DIR", "/var/data"))

FUSION = DATA / "diamond_event_market_fusion.json"
MULTI = DATA / "diamond_multi_exchange_confirmation.json"

STATE = DATA / "diamond_event_outcome_tracker_state.json"
REPORT = DATA / "diamond_event_outcome_tracker_report.json"

BITVAVO_TICKER_PRICE = "https://api.bitvavo.com/v2/ticker/price"

TRACK_STATUSES = {
    "FUSED_STRONG",
    "FUSED",
    "FUSED_CONFLICT",
    "NEWS_WATCH",
    "MARKET_WATCH",
}

HORIZONS = {
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "12h": 12 * 60 * 60,
}

# Zelfde ongewijzigde signaal mag na 6 uur opnieuw als nieuw observatiepunt
# worden vastgelegd. Zo groeit de dataset wel, maar niet elke 15 minuten dubbel.
REPEAT_AFTER_SECONDS = 6 * 60 * 60

RETENTION_SECONDS = 35 * 24 * 60 * 60
MAX_EVENTS = 5000

SAFETY = {
    "research_only": True,
    "orders": False,
    "private_api": False,
    "config_change": False,
    "strategy_change": False,
    "filter_change": False,
    "stake_change": False,
    "live_change": False,
}


def now_ts() -> int:
    return int(time.time())


def now_iso(ts: Optional[int] = None) -> str:
    if ts is None:
        ts = now_ts()
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def f(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def load_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


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


def fetch_prices() -> Dict[str, float]:
    req = urllib.request.Request(
        BITVAVO_TICKER_PRICE,
        headers={
            "Accept": "application/json",
            "User-Agent": "Diamond-Trader-Event-Outcome/1.0",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))

    result: Dict[str, float] = {}
    if isinstance(payload, dict):
        payload = [payload]

    for row in payload if isinstance(payload, list) else []:
        if not isinstance(row, dict):
            continue
        market = str(row.get("market") or "")
        price = f(row.get("price"), 0.0)
        if market and price > 0:
            result[market] = price

    return result


def confirmation_map(multi: Dict[str, Any]) -> Dict[str, str]:
    result = {}
    for row in multi.get("markets") or []:
        if not isinstance(row, dict):
            continue
        market = str(row.get("market") or "")
        status = str(row.get("confirmation_status") or "")
        if market:
            result[market] = status
    return result


def fingerprint(row: Dict[str, Any], confirmation: str) -> str:
    parts = [
        str(row.get("market") or ""),
        str(row.get("fusion_status") or ""),
        str(row.get("event_type") or ""),
        str(row.get("impact_hint") or ""),
        str(row.get("direction_relation") or ""),
        str(row.get("news_title") or ""),
        "market_event=1" if row.get("market_event_candidate") else "market_event=0",
        confirmation,
    ]
    raw = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def eligible_signal_rows(
    fusion: Dict[str, Any],
    multi: Dict[str, Any],
) -> List[Dict[str, Any]]:
    confirmations = confirmation_map(multi)
    rows = []

    for row in fusion.get("all_markets") or []:
        if not isinstance(row, dict):
            continue

        status = str(row.get("fusion_status") or "").upper()
        market = str(row.get("market") or "")

        if status not in TRACK_STATUSES or not market:
            continue

        copy = dict(row)
        copy["confirmation_status"] = confirmations.get(
            market,
            "NO_EXTERNAL_DATA",
        )
        copy["_fingerprint"] = fingerprint(
            copy,
            copy["confirmation_status"],
        )
        rows.append(copy)

    rows.sort(
        key=lambda row: (
            f(row.get("fusion_score")),
            f(row.get("volume_quote_24h")),
        ),
        reverse=True,
    )
    return rows


def empty_state() -> Dict[str, Any]:
    return {
        "version": VERSION,
        "created_at": now_iso(),
        "events": [],
        "last_seen": {},
    }


def new_event(
    row: Dict[str, Any],
    price: float,
    ts: int,
) -> Dict[str, Any]:
    return {
        "event_id": hashlib.sha256(
            (
                f"{row['market']}|{row['_fingerprint']}|{ts}"
            ).encode("utf-8")
        ).hexdigest()[:24],
        "market": row["market"],
        "started_ts": ts,
        "started_at": now_iso(ts),
        "entry_price": price,
        "fusion_status": str(row.get("fusion_status") or ""),
        "fusion_score": round(f(row.get("fusion_score")), 4),
        "market_score": round(f(row.get("market_score")), 4),
        "news_score": round(f(row.get("news_score")), 4),
        "liquidity_status": str(row.get("liquidity_status") or ""),
        "spread_pct": round(f(row.get("spread_pct")), 4),
        "volume_quote_24h": round(f(row.get("volume_quote_24h")), 2),
        "change_24h_pct_at_start": round(f(row.get("change_24h_pct")), 4),
        "market_event_candidate": bool(row.get("market_event_candidate")),
        "news_present": bool(row.get("news_present")),
        "event_type": row.get("event_type"),
        "impact_hint": row.get("impact_hint"),
        "direction_relation": row.get("direction_relation"),
        "news_source": row.get("news_source"),
        "news_title": row.get("news_title"),
        "confirmation_status": row.get("confirmation_status"),
        "fingerprint": row["_fingerprint"],
        "checkpoints": {
            label: {
                "target_seconds": seconds,
                "completed": False,
                "observed_ts": None,
                "observed_at": None,
                "elapsed_minutes": None,
                "price": None,
                "return_pct": None,
            }
            for label, seconds in HORIZONS.items()
        },
    }


def should_create(
    state: Dict[str, Any],
    row: Dict[str, Any],
    ts: int,
) -> bool:
    market = row["market"]
    seen = (state.get("last_seen") or {}).get(market) or {}

    if seen.get("fingerprint") != row["_fingerprint"]:
        return True

    last_created_ts = int(seen.get("created_ts") or 0)
    return (ts - last_created_ts) >= REPEAT_AFTER_SECONDS


def update_checkpoints(
    events: List[Dict[str, Any]],
    prices: Dict[str, float],
    ts: int,
) -> int:
    completed_now = 0

    for event in events:
        market = str(event.get("market") or "")
        entry = f(event.get("entry_price"), 0.0)
        price = f(prices.get(market), 0.0)

        if not market or entry <= 0 or price <= 0:
            continue

        started_ts = int(event.get("started_ts") or 0)
        elapsed = max(0, ts - started_ts)

        for label, target in HORIZONS.items():
            checkpoint = (event.get("checkpoints") or {}).get(label)
            if not isinstance(checkpoint, dict):
                continue
            if checkpoint.get("completed"):
                continue
            if elapsed < target:
                continue

            checkpoint["completed"] = True
            checkpoint["observed_ts"] = ts
            checkpoint["observed_at"] = now_iso(ts)
            checkpoint["elapsed_minutes"] = round(elapsed / 60.0, 1)
            checkpoint["price"] = price
            checkpoint["return_pct"] = round(
                ((price - entry) / entry) * 100.0,
                4,
            )
            completed_now += 1

    return completed_now


def prune_state(state: Dict[str, Any], ts: int) -> int:
    events = [
        event
        for event in state.get("events") or []
        if isinstance(event, dict)
    ]

    before = len(events)
    cutoff = ts - RETENTION_SECONDS

    events = [
        event
        for event in events
        if int(event.get("started_ts") or 0) >= cutoff
    ]

    if len(events) > MAX_EVENTS:
        events.sort(
            key=lambda event: int(event.get("started_ts") or 0),
            reverse=True,
        )
        events = events[:MAX_EVENTS]
        events.sort(
            key=lambda event: int(event.get("started_ts") or 0)
        )

    state["events"] = events
    return before - len(events)


def summarize_events(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_status: Dict[str, Dict[str, Any]] = {}

    for status in sorted(TRACK_STATUSES):
        subset = [
            event for event in events
            if event.get("fusion_status") == status
        ]

        checkpoints = {}
        for label in HORIZONS:
            returns = [
                f(event["checkpoints"][label].get("return_pct"))
                for event in subset
                if (
                    isinstance(event.get("checkpoints"), dict)
                    and isinstance(event["checkpoints"].get(label), dict)
                    and event["checkpoints"][label].get("completed")
                    and event["checkpoints"][label].get("return_pct") is not None
                )
            ]

            checkpoints[label] = {
                "n": len(returns),
                "average_return_pct": (
                    round(sum(returns) / len(returns), 4)
                    if returns else None
                ),
                "positive": sum(1 for value in returns if value > 0),
                "negative": sum(1 for value in returns if value < 0),
            }

        by_status[status] = {
            "events": len(subset),
            "checkpoints": checkpoints,
        }

    return {
        "events_total": len(events),
        "by_fusion_status": by_status,
    }


def build_report(
    state: Dict[str, Any],
    created_now: int,
    checkpoints_now: int,
    active_signals: int,
    pruned: int,
) -> Dict[str, Any]:
    events = [
        event
        for event in state.get("events") or []
        if isinstance(event, dict)
    ]

    return {
        "version": VERSION,
        "generated_at": now_iso(),
        "research_only": True,
        "active_signals_seen": active_signals,
        "events_created_now": created_now,
        "checkpoints_completed_now": checkpoints_now,
        "events_pruned_now": pruned,
        "summary": summarize_events(events),
        "safety": SAFETY,
    }


def checkpoint_text(item: Dict[str, Any]) -> str:
    n = int(item.get("n") or 0)
    avg = item.get("average_return_pct")
    if n == 0 or avg is None:
        return "n=0"
    return (
        f"n={n} avg={float(avg):+.2f}% "
        f"+/-={int(item.get('positive') or 0)}/"
        f"{int(item.get('negative') or 0)}"
    )


def print_report(report: Dict[str, Any]) -> None:
    print("=" * 92)
    print(f" DIAMOND EVENT / UNIVERSE OUTCOME TRACKER v{VERSION}")
    print("=" * 92)
    print(f"Actieve signalen gezien : {report['active_signals_seen']}")
    print(f"Nieuwe events           : {report['events_created_now']}")
    print(f"Checkpoints gevuld      : {report['checkpoints_completed_now']}")
    print(f"Events totaal           : {report['summary']['events_total']}")
    print(f"Events opgeschoond      : {report['events_pruned_now']}")

    print("\n=== RESULTATEN PER SIGNAAKTYPE ===")
    for status in (
        "FUSED_STRONG",
        "FUSED",
        "FUSED_CONFLICT",
        "NEWS_WATCH",
        "MARKET_WATCH",
    ):
        row = report["summary"]["by_fusion_status"].get(status) or {}
        cp = row.get("checkpoints") or {}
        print(
            f"{status:<16} "
            f"events={int(row.get('events') or 0):>3} | "
            f"1h {checkpoint_text(cp.get('1h') or {})} | "
            f"4h {checkpoint_text(cp.get('4h') or {})} | "
            f"12h {checkpoint_text(cp.get('12h') or {})}"
        )

    print("\n=== STATUS ===")
    if report["summary"]["events_total"] == 0:
        print("Nog geen events vastgelegd.")
    elif all(
        (row.get("checkpoints") or {}).get("1h", {}).get("n", 0) == 0
        for row in report["summary"]["by_fusion_status"].values()
    ):
        print("Baseline staat; wachten op eerste 1h-checkpoints.")
    else:
        print("Prospectieve outcome-data wordt opgebouwd.")

    print("\n=== VEILIGHEID ===")
    print("Publieke Bitvavo prijsdata : JA")
    print("Orders                    : NEE")
    print("Private API               : NEE")
    print("Strategy/filter gewijzigd : NEE")
    print("Stake/config/live         : NEE")


def main() -> int:
    fusion = load_json(FUSION)
    multi = load_json(MULTI)

    if not fusion.get("all_markets"):
        print("=" * 92)
        print(f" DIAMOND EVENT / UNIVERSE OUTCOME TRACKER v{VERSION}")
        print("=" * 92)
        print("STATUS: WAIT_FUSION_DATA")
        print("Orders/private API/live wijziging: NEE")
        return 2

    try:
        prices = fetch_prices()
    except Exception as exc:
        print("=" * 92)
        print(f" DIAMOND EVENT / UNIVERSE OUTCOME TRACKER v{VERSION}")
        print("=" * 92)
        print(f"STATUS: PRICE_SOURCE_FAIL | {type(exc).__name__}")
        print("Orders/private API/live wijziging: NEE")
        return 3

    ts = now_ts()
    state = load_json(STATE)
    if not state.get("events") and not state.get("last_seen"):
        state = empty_state()

    rows = eligible_signal_rows(fusion, multi)

    created_now = 0
    events = state.setdefault("events", [])
    last_seen = state.setdefault("last_seen", {})

    for row in rows:
        market = row["market"]
        price = f(prices.get(market), 0.0)
        if price <= 0:
            continue

        if should_create(state, row, ts):
            events.append(new_event(row, price, ts))
            last_seen[market] = {
                "fingerprint": row["_fingerprint"],
                "created_ts": ts,
                "created_at": now_iso(ts),
            }
            created_now += 1

    checkpoints_now = update_checkpoints(events, prices, ts)
    pruned = prune_state(state, ts)

    state["version"] = VERSION
    state["updated_at"] = now_iso(ts)
    atomic_json(STATE, state)

    report = build_report(
        state,
        created_now=created_now,
        checkpoints_now=checkpoints_now,
        active_signals=len(rows),
        pruned=pruned,
    )
    atomic_json(REPORT, report)
    print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
