#!/usr/bin/env python3
# Diamond Trader Coin Admission Shadow Gate v1.0
#
# Bepaalt welke NIET-core munten veilig naar een research/shadow-queue mogen.
# Wijzigt geen config/symbolen, start geen trades en laat niets live toe.

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


VERSION = "1.0"
DATA = Path("/var/data")

FUSION_PATH = DATA / "diamond_event_market_fusion.json"
OUTPUT_PATH = DATA / "diamond_shadow_admission_queue.json"

CORE_MARKETS = {
    "BTC-EUR",
    "ETH-EUR",
    "SOL-EUR",
    "XRP-EUR",
    "ADA-EUR",
}

MAX_SPREAD_PCT = 0.25
MIN_VOLUME_QUOTE_24H = 100_000.0
MAX_NEW_SHADOW_ADMISSIONS = 5


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def load_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
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


def admission_checks(row: Dict[str, Any]) -> Dict[str, bool]:
    market = str(row.get("market") or "")
    status = str(row.get("fusion_status") or "").upper()
    liquidity = str(row.get("liquidity_status") or "").upper()
    relation = str(row.get("direction_relation") or "NEUTRAL").upper()

    spread = to_float(row.get("spread_pct"), 999.0)
    volume = to_float(row.get("volume_quote_24h"), 0.0)

    return {
        "not_core": market not in CORE_MARKETS,
        "fusion_status": status in {"FUSED_STRONG", "FUSED"},
        "liquidity_pass": liquidity == "PASS",
        "spread_ok": spread <= MAX_SPREAD_PCT,
        "volume_ok": volume >= MIN_VOLUME_QUOTE_24H,
        "not_conflict": relation != "CONFLICT",
        "research_only": bool(row.get("research_only", True)),
        "not_live_eligible": not bool(row.get("live_eligible", False)),
    }


def watch_reason(row: Dict[str, Any]) -> str:
    status = str(row.get("fusion_status") or "").upper()

    if status == "FUSED_CONFLICT":
        return "NEWS_MARKET_CONFLICT"
    if status == "NEWS_WATCH":
        return "NEWS_ZONDER_MARKTBEVESTIGING"
    if status == "MARKET_WATCH":
        return "MARKTBEWEGING_ZONDER_NIEUWS"
    if str(row.get("liquidity_status") or "").upper() != "PASS":
        return "LIQUIDITEIT_NOG_NIET_PASS"
    return "NOG_NIET_SHADOW_TOELAATBAAR"


def build_gate(fusion: Dict[str, Any]) -> Dict[str, Any]:
    rows = [
        row for row in (fusion.get("all_markets") or [])
        if isinstance(row, dict)
    ]

    admissions: List[Dict[str, Any]] = []
    watches: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []

    for row in rows:
        market = str(row.get("market") or "")
        if not market:
            continue

        if market in CORE_MARKETS:
            continue

        checks = admission_checks(row)
        all_pass = all(checks.values())

        compact = {
            "market": market,
            "fusion_status": str(row.get("fusion_status") or ""),
            "fusion_score": to_float(row.get("fusion_score"), 0.0),
            "spread_pct": to_float(row.get("spread_pct"), 999.0),
            "volume_quote_24h": to_float(
                row.get("volume_quote_24h"), 0.0
            ),
            "change_24h_pct": to_float(
                row.get("change_24h_pct"), 0.0
            ),
            "event_type": row.get("event_type"),
            "impact_hint": row.get("impact_hint"),
            "direction_relation": row.get("direction_relation"),
            "news_title": row.get("news_title"),
            "checks": checks,
            "shadow_only": True,
            "live_eligible": False,
        }

        if all_pass:
            compact["decision"] = "ADMIT_SHADOW"
            admissions.append(compact)
            continue

        status = str(row.get("fusion_status") or "").upper()
        if status in {
            "FUSED_CONFLICT",
            "NEWS_WATCH",
            "MARKET_WATCH",
            "FUSED",
            "FUSED_STRONG",
        }:
            compact["decision"] = "WATCH"
            compact["watch_reason"] = watch_reason(row)
            watches.append(compact)
        else:
            compact["decision"] = "REJECT_FOR_NOW"
            rejected.append(compact)

    admissions.sort(
        key=lambda x: (x["fusion_score"], x["volume_quote_24h"]),
        reverse=True,
    )
    watches.sort(
        key=lambda x: (x["fusion_score"], x["volume_quote_24h"]),
        reverse=True,
    )

    selected = admissions[:MAX_NEW_SHADOW_ADMISSIONS]
    overflow = admissions[MAX_NEW_SHADOW_ADMISSIONS:]

    for row in overflow:
        row["decision"] = "WAIT_CAPACITY"
        row["watch_reason"] = "MAX_NIEUWE_SHADOW_ADMISSIONS_BEREIKT"
        watches.append(row)

    return {
        "version": VERSION,
        "generated_at": now_iso(),
        "research_only": True,
        "core_markets_unchanged": sorted(CORE_MARKETS),
        "config_changed": False,
        "symbols_changed": False,
        "orders_used": False,
        "private_api_used": False,
        "automatic_shadow_trade_start": False,
        "automatic_live_change": False,
        "live_eligible_markets": 0,
        "thresholds": {
            "max_spread_pct": MAX_SPREAD_PCT,
            "min_volume_quote_24h": MIN_VOLUME_QUOTE_24H,
            "max_new_shadow_admissions": MAX_NEW_SHADOW_ADMISSIONS,
            "required_fusion_status": [
                "FUSED_STRONG",
                "FUSED",
            ],
            "conflict_allowed": False,
        },
        "counts": {
            "fusion_markets_seen": len(rows),
            "new_shadow_admissions": len(selected),
            "watch": len(watches),
            "rejected_for_now": len(rejected),
        },
        "shadow_admissions": selected,
        "watch_candidates": watches[:40],
        "rejected_for_now": rejected[:40],
    }


def print_report(result: Dict[str, Any]) -> None:
    c = result["counts"]

    print("=" * 78)
    print(f" DIAMOND COIN ADMISSION SHADOW GATE v{VERSION}")
    print("=" * 78)
    print(f"Fusion markten gezien : {c['fusion_markets_seen']}")
    print(f"Nieuwe shadow admit   : {c['new_shadow_admissions']}")
    print(f"Watch kandidaten      : {c['watch']}")
    print(f"Reject for now        : {c['rejected_for_now']}")
    print("Core 5 gewijzigd      : NEE")
    print("Symbols/config        : ONGEWIJZIGD")
    print("Auto shadow trade     : NEE")
    print("Live eligible         : 0")

    print("\n=== SHADOW ADMISSIONS ===")
    if not result["shadow_admissions"]:
        print("Geen. Er wordt niets geforceerd.")
    else:
        for row in result["shadow_admissions"]:
            print(
                f"{row['market']:<12} "
                f"score={row['fusion_score']:>6.1f} "
                f"spr={row['spread_pct']:>6.3f}% "
                f"vol=€{row['volume_quote_24h']:,.0f} "
                f"[ADMIT_SHADOW]"
            )

    print("\n=== TOP WATCH ===")
    if not result["watch_candidates"]:
        print("Geen watch kandidaten.")
    else:
        for row in result["watch_candidates"][:12]:
            print(
                f"{row['market']:<12} "
                f"score={row['fusion_score']:>6.1f} "
                f"[{row.get('watch_reason','WATCH')}]"
            )

    print("\nADMIT_SHADOW betekent alleen toelating tot een latere research/shadow-run.")
    print("Dit script start zelf GEEN shadow-trade.")
    print("Live toelating       : NEE")
    print("Orders/private API   : NEE")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fusion",
        default=str(FUSION_PATH),
    )
    parser.add_argument(
        "--output",
        default=str(OUTPUT_PATH),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    fusion = load_json(Path(args.fusion))

    if not fusion.get("all_markets"):
        print("=" * 78)
        print(f" DIAMOND COIN ADMISSION SHADOW GATE v{VERSION}")
        print("=" * 78)
        print("STATUS : WAIT_FUSION_DATA")
        print("Draai eerst diamond_event_market_fusion.py")
        print("Orders/private API : NEE")
        return 1

    result = build_gate(fusion)
    atomic_json(Path(args.output), result)
    print_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
