#!/usr/bin/env python3
# Diamond Trader Event + Market Fusion v1.0
#
# Combineert publieke marktdata uit punt 1 met nieuws/events uit punt 2.
# Research-only: geen API-key, geen private API, geen orders en geen live/config wijziging.

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


VERSION = "1.0"
ROOT = Path(__file__).resolve().parent
DATA = Path("/var/data")

UNIVERSE_PATH = DATA / "diamond_dynamic_universe.json"
NEWS_PATH = DATA / "diamond_crypto_news_events.json"
OUTPUT_PATH = DATA / "diamond_event_market_fusion.json"

UNIVERSE_HELPER = ROOT / "diamond_dynamic_bitvavo_universe.py"
NEWS_HELPER = ROOT / "diamond_crypto_news_event_radar.py"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
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


def run_helper(path: Path) -> tuple[int, str]:
    if not path.exists():
        return 127, f"ONTBREEKT:{path.name}"
    try:
        result = subprocess.run(
            ["python3", str(path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        return result.returncode, (result.stdout or "") + (result.stderr or "")
    except Exception as exc:
        return 126, f"{type(exc).__name__}:{exc}"


def direction_relation(change_pct: float, impact_hint: str) -> str:
    hint = str(impact_hint or "NEUTRAL_OR_MIXED").upper()

    if abs(change_pct) < 0.25 or hint == "NEUTRAL_OR_MIXED":
        return "NEUTRAL"

    if change_pct > 0 and hint == "POSITIVE_HINT":
        return "ALIGNED"
    if change_pct < 0 and hint == "NEGATIVE_HINT":
        return "ALIGNED"

    if change_pct > 0 and hint == "NEGATIVE_HINT":
        return "CONFLICT"
    if change_pct < 0 and hint == "POSITIVE_HINT":
        return "CONFLICT"

    return "NEUTRAL"


def news_map(news: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    result = {}
    for row in news.get("market_summary") or []:
        if not isinstance(row, dict):
            continue
        market = str(row.get("market") or "")
        if not market:
            continue
        latest = row.get("latest") or []
        first = latest[0] if latest and isinstance(latest[0], dict) else {}
        result[market] = {
            "news_events": to_int(row.get("news_events"), 0),
            "best_score": to_float(row.get("best_score"), 0.0),
            "market_event_confirmed_by_news": bool(
                row.get("market_event_confirmed_by_news")
            ),
            "event_type": str(first.get("event_type") or "general_news"),
            "impact_hint": str(
                first.get("impact_hint") or "NEUTRAL_OR_MIXED"
            ),
            "source": str(first.get("source") or ""),
            "title": str(first.get("title") or ""),
            "published_at": first.get("published_at"),
            "mention_confidence": str(
                first.get("mention_confidence") or "UNKNOWN"
            ),
        }
    return result


def market_component(row: Dict[str, Any]) -> float:
    # rank_score uit punt 1 is al gebaseerd op volume, spread en beweging.
    raw = to_float(row.get("rank_score"), 0.0)
    liquidity = str(row.get("liquidity_status") or "LOW").upper()

    bonus = {
        "PASS": 12.0,
        "WATCH": 3.0,
        "LOW": -15.0,
    }.get(liquidity, -15.0)

    if bool(row.get("market_event_candidate")):
        bonus += 8.0

    return clamp(raw + bonus)


def news_component(news_row: Dict[str, Any] | None) -> float:
    if not news_row:
        return 0.0

    score = clamp(to_float(news_row.get("best_score"), 0.0))
    count_bonus = min(8.0, max(0, to_int(news_row.get("news_events"), 0) - 1) * 2.0)

    confidence = str(news_row.get("mention_confidence") or "UNKNOWN").upper()
    confidence_bonus = {
        "HIGH": 5.0,
        "MEDIUM": 2.0,
        "LOW": -4.0,
    }.get(confidence, 0.0)

    return clamp(score + count_bonus + confidence_bonus)


def classify(
    *,
    liquidity: str,
    has_news: bool,
    market_event: bool,
    relation: str,
    fusion_score: float,
) -> str:
    liquidity = liquidity.upper()

    if liquidity == "LOW":
        return "LOW_LIQUIDITY"

    if has_news and market_event:
        if relation == "ALIGNED" and liquidity == "PASS" and fusion_score >= 70:
            return "FUSED_STRONG"
        if relation == "CONFLICT":
            return "FUSED_CONFLICT"
        return "FUSED"

    if has_news:
        return "NEWS_WATCH"

    if market_event:
        return "MARKET_WATCH"

    if liquidity == "PASS":
        return "LIQUIDITY_PASS"

    return "WATCH"


def build_fusion(
    universe: Dict[str, Any],
    news: Dict[str, Any],
) -> Dict[str, Any]:
    nmap = news_map(news)
    rows: List[Dict[str, Any]] = []

    for market_row in universe.get("all_active_eur_markets") or []:
        if not isinstance(market_row, dict):
            continue

        market = str(market_row.get("market") or "")
        if not market:
            continue

        nrow = nmap.get(market)
        has_news = bool(nrow)
        market_event = bool(market_row.get("market_event_candidate"))
        change = to_float(market_row.get("change_24h_pct"), 0.0)

        mscore = market_component(market_row)
        nscore = news_component(nrow)

        relation = direction_relation(
            change,
            (nrow or {}).get("impact_hint", "NEUTRAL_OR_MIXED"),
        )

        fusion = (0.60 * mscore) + (0.40 * nscore)

        if has_news and market_event:
            fusion += 12.0
        if relation == "ALIGNED":
            fusion += 8.0
        elif relation == "CONFLICT":
            fusion -= 10.0

        liquidity = str(
            market_row.get("liquidity_status") or "LOW"
        ).upper()

        # Lage liquiditeit mag nooit door nieuws omhoog worden gepromoveerd.
        if liquidity == "LOW":
            fusion = min(fusion, 39.9)

        fusion = round(clamp(fusion), 4)

        status = classify(
            liquidity=liquidity,
            has_news=has_news,
            market_event=market_event,
            relation=relation,
            fusion_score=fusion,
        )

        row = {
            "market": market,
            "base": market_row.get("base"),
            "core_coin": bool(market_row.get("core_coin")),
            "fusion_status": status,
            "fusion_score": fusion,
            "market_score": round(mscore, 4),
            "news_score": round(nscore, 4),
            "liquidity_status": liquidity,
            "spread_pct": to_float(market_row.get("spread_pct"), 0.0),
            "volume_quote_24h": to_float(
                market_row.get("volume_quote_24h"), 0.0
            ),
            "change_24h_pct": change,
            "range_24h_pct": to_float(
                market_row.get("range_24h_pct"), 0.0
            ),
            "market_event_candidate": market_event,
            "news_present": has_news,
            "news_events": to_int((nrow or {}).get("news_events"), 0),
            "event_type": (nrow or {}).get("event_type"),
            "impact_hint": (nrow or {}).get("impact_hint"),
            "direction_relation": relation,
            "news_source": (nrow or {}).get("source"),
            "news_title": (nrow or {}).get("title"),
            "mention_confidence": (nrow or {}).get("mention_confidence"),
            "deep_scan_candidate": status in {
                "FUSED_STRONG",
                "FUSED",
                "NEWS_WATCH",
                "MARKET_WATCH",
            } and liquidity in {"PASS", "WATCH"},
            "shadow_candidate": status in {
                "FUSED_STRONG",
                "FUSED",
            } and liquidity == "PASS",
            "live_eligible": False,
            "research_only": True,
        }
        rows.append(row)

    priority = {
        "FUSED_STRONG": 6,
        "FUSED": 5,
        "FUSED_CONFLICT": 4,
        "NEWS_WATCH": 3,
        "MARKET_WATCH": 2,
        "LIQUIDITY_PASS": 1,
        "WATCH": 0,
        "LOW_LIQUIDITY": -1,
    }

    rows.sort(
        key=lambda row: (
            priority.get(row["fusion_status"], -2),
            row["fusion_score"],
            row["volume_quote_24h"],
        ),
        reverse=True,
    )

    status_counts: Dict[str, int] = {}
    for row in rows:
        status_counts[row["fusion_status"]] = (
            status_counts.get(row["fusion_status"], 0) + 1
        )

    return {
        "version": VERSION,
        "generated_at": now_iso(),
        "research_only": True,
        "private_api_used": False,
        "orders_used": False,
        "automatic_live_change": False,
        "live_eligible_markets": 0,
        "counts": {
            "markets_evaluated": len(rows),
            "news_markets": sum(1 for row in rows if row["news_present"]),
            "market_events": sum(
                1 for row in rows if row["market_event_candidate"]
            ),
            "fused": sum(
                1 for row in rows
                if row["fusion_status"] in {"FUSED_STRONG", "FUSED", "FUSED_CONFLICT"}
            ),
            "deep_scan_candidates": sum(
                1 for row in rows if row["deep_scan_candidate"]
            ),
            "shadow_candidates": sum(
                1 for row in rows if row["shadow_candidate"]
            ),
        },
        "status_counts": status_counts,
        "top_candidates": rows[:40],
        "shadow_candidates": [
            row for row in rows if row["shadow_candidate"]
        ][:25],
        "all_markets": rows,
    }


def print_report(result: Dict[str, Any]) -> None:
    c = result["counts"]

    print("=" * 78)
    print(f" DIAMOND EVENT + MARKET FUSION v{VERSION}")
    print("=" * 78)
    print(f"Markten beoordeeld  : {c['markets_evaluated']}")
    print(f"Markten met nieuws  : {c['news_markets']}")
    print(f"Market-events       : {c['market_events']}")
    print(f"Fused               : {c['fused']}")
    print(f"Deep-scan kandidaten: {c['deep_scan_candidates']}")
    print(f"Shadow kandidaten   : {c['shadow_candidates']}")
    print("Research-only       : JA")
    print("Live eligible       : 0")

    print("\n=== TOP FUSION KANDIDATEN ===")
    candidates = [
        row for row in result["top_candidates"]
        if row["fusion_status"] not in {"LOW_LIQUIDITY", "WATCH"}
    ]

    if not candidates:
        print("Geen verhoogde kandidaten in deze run.")
    else:
        for row in candidates[:15]:
            print(
                f"{row['market']:<12} "
                f"score={row['fusion_score']:>6.1f} "
                f"24h={row['change_24h_pct']:>+7.2f}% "
                f"spr={row['spread_pct']:>6.3f}% "
                f"[{row['fusion_status']}]"
            )
            if row["news_present"]:
                print(
                    f"  {row['event_type']} | "
                    f"{row['impact_hint']} | "
                    f"{row['direction_relation']} | "
                    f"{(row['news_title'] or '')[:90]}"
                )

    print("\n=== SHADOW KANDIDATEN ===")
    shadows = result["shadow_candidates"]
    if not shadows:
        print("Geen. Dat is toegestaan; er wordt niets geforceerd.")
    else:
        for row in shadows[:10]:
            print(
                f"{row['market']:<12} "
                f"score={row['fusion_score']:>6.1f} "
                f"[{row['fusion_status']}]"
            )

    print("\nNieuws alleen is GEEN kooptrigger.")
    print("Shadow kandidaat is GEEN live toelating.")
    print("Live toelating      : NEE")
    print("Orders/private API  : NEE")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--universe",
        default=str(UNIVERSE_PATH),
    )
    parser.add_argument(
        "--news",
        default=str(NEWS_PATH),
    )
    parser.add_argument(
        "--output",
        default=str(OUTPUT_PATH),
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Gebruik bestaande JSON-bestanden zonder helpers opnieuw te draaien.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.no_refresh:
        rc1, _ = run_helper(UNIVERSE_HELPER)
        if rc1 != 0:
            print("=" * 78)
            print(f" DIAMOND EVENT + MARKET FUSION v{VERSION}")
            print("=" * 78)
            print("STATUS : WAIT_UNIVERSE_HELPER")
            print("Orders/private API : NEE")
            return 1

        rc2, _ = run_helper(NEWS_HELPER)
        if rc2 != 0:
            print("=" * 78)
            print(f" DIAMOND EVENT + MARKET FUSION v{VERSION}")
            print("=" * 78)
            print("STATUS : WAIT_NEWS_HELPER")
            print("Orders/private API : NEE")
            return 1

    universe = load_json(Path(args.universe))
    news = load_json(Path(args.news))

    if not universe.get("all_active_eur_markets"):
        print("STATUS : WAIT_UNIVERSE_DATA")
        print("Orders/private API : NEE")
        return 1

    if "market_summary" not in news:
        print("STATUS : WAIT_NEWS_DATA")
        print("Orders/private API : NEE")
        return 1

    result = build_fusion(universe, news)
    atomic_json(Path(args.output), result)
    print_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
