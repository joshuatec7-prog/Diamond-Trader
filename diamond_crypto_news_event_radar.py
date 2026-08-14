#!/usr/bin/env python3
# Diamond Trader Broad Crypto News / Event Radar v1.0
#
# Leest publieke RSS-feeds en koppelt nieuws aan ALLE actieve Bitvavo EUR-assets.
# Geen API-key, geen private API, geen orders en geen live/config wijziging.

from __future__ import annotations

import argparse
import email.utils
import html
import json
import math
import os
import re
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


VERSION = "1.1"
DATA = Path("/var/data")
UNIVERSE_PATH = DATA / "diamond_dynamic_universe.json"
OUTPUT_PATH = DATA / "diamond_crypto_news_events.json"

ASSETS_URL = "https://api.bitvavo.com/v2/assets"

DEFAULT_FEEDS = (
    ("coindesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("cointelegraph", "https://cointelegraph.com/rss"),
)

DEFAULT_MAX_AGE_HOURS = 36.0
DEFAULT_MAX_ITEMS_PER_FEED = 80

# Symbolen die als gewoon Engels woord vaak false positives geven.
AMBIGUOUS_SYMBOLS = {
    "AI", "API", "AR", "BAT", "CAT", "CITY", "CORE", "DENT", "FLOKI",
    "GAS", "GLM", "HARD", "HIGH", "HOT", "ID", "IO", "KEY", "LIT",
    "MAGIC", "MASK", "MOVE", "ONE", "ONDO", "PEOPLE", "POL", "PORTAL",
    "RARE", "RLC", "SAFE", "SAND", "SPELL", "STG", "SUPER", "TIME",
    "TON", "TRU", "WOO", "XAI",
}

EVENT_KEYWORDS = {
    "listing": (
        "listing", "listed", "lists", "exchange listing", "spot listing",
        "trading pair", "launchpool",
    ),
    "delisting": (
        "delist", "delisting", "remove trading", "trading halt",
    ),
    "partnership": (
        "partnership", "partners with", "collaboration", "integrates with",
        "integration", "strategic alliance",
    ),
    "upgrade": (
        "upgrade", "hard fork", "mainnet", "testnet", "network upgrade",
        "protocol upgrade", "v2", "v3",
    ),
    "security": (
        "hack", "hacked", "exploit", "exploited", "breach", "attack",
        "vulnerability", "drained", "stolen funds",
    ),
    "regulation": (
        "sec", "regulator", "regulation", "lawsuit", "court", "approval",
        "approved", "etf", "license", "licensed", "ban", "banned",
    ),
    "token_event": (
        "airdrop", "token unlock", "unlock", "burn", "token burn",
        "staking", "snapshot", "migration",
    ),
    "funding": (
        "funding round", "raises", "raised", "investment", "acquisition",
        "acquires", "merger",
    ),
    "outage": (
        "outage", "downtime", "halted", "paused", "network down",
    ),
}

POSITIVE_TERMS = (
    "approval", "approved", "partnership", "partners with", "integration",
    "integrates", "listing", "listed", "launch", "mainnet", "upgrade",
    "funding", "raises", "record", "adoption", "license", "licensed",
)
NEGATIVE_TERMS = (
    "hack", "exploit", "breach", "attack", "stolen", "delist", "ban",
    "lawsuit", "outage", "halted", "downtime", "investigation",
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


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


def http_get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/rss+xml, application/xml, text/xml, application/json, */*",
            "User-Agent": "Diamond-Trader-News-Radar/1.0",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def fetch_assets() -> List[Dict[str, Any]]:
    raw = http_get(ASSETS_URL)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def strip_html(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def child_text(node: ET.Element, names: Iterable[str]) -> str:
    wanted = set(names)
    for child in list(node):
        tag = child.tag.split("}")[-1]
        if tag in wanted:
            if child.text:
                return child.text.strip()
    return ""


def parse_date(value: str) -> Optional[datetime]:
    value = (value or "").strip()
    if not value:
        return None

    try:
        dt = email.utils.parsedate_to_datetime(value)
        if dt is not None:
            return dt.astimezone(timezone.utc)
    except Exception:
        pass

    try:
        clean = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def parse_feed(xml_bytes: bytes, source: str, url: str) -> List[Dict[str, Any]]:
    root = ET.fromstring(xml_bytes)
    items: List[Dict[str, Any]] = []

    # RSS <item> en Atom <entry>.
    nodes = []
    for node in root.iter():
        local = node.tag.split("}")[-1]
        if local in {"item", "entry"}:
            nodes.append(node)

    for node in nodes:
        title = child_text(node, ("title",))
        summary = child_text(node, ("description", "summary", "content", "encoded"))
        published_raw = child_text(
            node,
            ("pubDate", "published", "updated", "date"),
        )

        link = child_text(node, ("link",))
        if not link:
            for child in list(node):
                if child.tag.split("}")[-1] == "link":
                    href = child.attrib.get("href")
                    if href:
                        link = href
                        break

        guid = child_text(node, ("guid", "id"))
        published = parse_date(published_raw)

        if not title:
            continue

        items.append({
            "source": source,
            "feed_url": url,
            "title": strip_html(title),
            "summary": strip_html(summary),
            "url": link or guid,
            "published_at": published.isoformat() if published else None,
            "_published_dt": published,
        })

    return items


def universe_assets(
    universe: Dict[str, Any],
    asset_rows: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    active = universe.get("all_active_eur_markets") or []
    symbols = {
        str(row.get("base") or "").upper()
        for row in active
        if isinstance(row, dict) and row.get("base")
    }

    names = {
        str(row.get("symbol") or "").upper(): str(row.get("name") or "").strip()
        for row in asset_rows
        if isinstance(row, dict) and row.get("symbol")
    }

    result = []
    for symbol in sorted(symbols):
        name = names.get(symbol, "")
        result.append({
            "symbol": symbol,
            "market": f"{symbol}-EUR",
            "name": name,
        })
    return result


def name_is_usable(name: str) -> bool:
    clean = re.sub(r"[^a-z0-9 ]+", " ", name.lower())
    clean = re.sub(r"\s+", " ", clean).strip()
    if len(clean) < 4:
        return False
    if clean in {
        "gas", "magic", "move", "one", "safe", "super", "time",
        "people", "city", "core", "mask", "portal",
    }:
        return False
    return True


def symbol_mention_score(text: str, symbol: str) -> int:
    escaped = re.escape(symbol)

    # Expliciete ticker-vormen blijven altijd geldig.
    if re.search(rf"\${escaped}(?![A-Za-z0-9])", text, flags=re.IGNORECASE):
        return 5
    if re.search(rf"\({escaped}\)", text):
        return 5
    if re.search(
        rf"(?<![A-Za-z0-9]){escaped}[-/]EUR(?![A-Za-z0-9])",
        text,
        flags=re.IGNORECASE,
    ):
        return 5

    # 1- en 2-letter tickers zijn te ambigu als los woord.
    # Voorbeeld: U-EUR mag niet matchen op "U.S.".
    # Ze kunnen nog wel via de volledige assetnaam in detect_assets().
    if len(symbol) <= 2:
        return 0

    # Bekende ambigue symbolen: alleen expliciete uppercase token.
    if symbol in AMBIGUOUS_SYMBOLS:
        return 3 if re.search(
            rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])",
            text,
        ) else 0

    # Overige tickers.
    if re.search(
        rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])",
        text,
    ):
        return 4

    if len(symbol) >= 4 and re.search(
        rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])",
        text,
        flags=re.IGNORECASE,
    ):
        return 2

    return 0

def detect_assets(text: str, assets: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    lower = text.lower()

    for asset in assets:
        symbol = asset["symbol"]
        name = asset["name"]
        score = symbol_mention_score(text, symbol)
        reasons = []

        if score:
            reasons.append("symbol")

        if name and name_is_usable(name):
            pattern = rf"(?<![a-z0-9]){re.escape(name.lower())}(?![a-z0-9])"
            if re.search(pattern, lower):
                score += 4
                reasons.append("asset_name")

        if score <= 0:
            continue

        confidence = (
            "HIGH" if score >= 5
            else "MEDIUM" if score >= 3
            else "LOW"
        )
        found.append({
            "symbol": symbol,
            "market": asset["market"],
            "name": name,
            "mention_score": score,
            "mention_confidence": confidence,
            "match": "+".join(reasons),
        })

    found.sort(key=lambda x: x["mention_score"], reverse=True)
    return found[:8]


def classify_event(text: str) -> Tuple[str, List[str]]:
    lower = text.lower()
    scores = []
    hits_by_type: Dict[str, List[str]] = {}

    for event_type, keywords in EVENT_KEYWORDS.items():
        hits = [kw for kw in keywords if kw in lower]
        if hits:
            hits_by_type[event_type] = hits
            scores.append((len(hits), event_type))

    if not scores:
        return "general_news", []

    scores.sort(reverse=True)
    best = scores[0][1]
    return best, hits_by_type[best][:4]


def impact_hint(text: str) -> str:
    lower = text.lower()
    pos = sum(1 for term in POSITIVE_TERMS if term in lower)
    neg = sum(1 for term in NEGATIVE_TERMS if term in lower)

    if pos > neg and pos > 0:
        return "POSITIVE_HINT"
    if neg > pos and neg > 0:
        return "NEGATIVE_HINT"
    return "NEUTRAL_OR_MIXED"


def normalized_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def source_weight(source: str) -> float:
    # Geen kwaliteitsclaim; alleen vaste technische prioriteit tussen ingestelde feeds.
    return {
        "coindesk": 1.0,
        "cointelegraph": 1.0,
    }.get(source, 0.8)


def event_score(
    *,
    source: str,
    published: Optional[datetime],
    mention_score: int,
    event_type: str,
    market_event_candidate: bool,
) -> float:
    now = now_utc()

    if published:
        age_hours = max(0.0, (now - published).total_seconds() / 3600.0)
        recency = max(0.0, 30.0 - min(30.0, age_hours))
    else:
        recency = 5.0

    type_bonus = 8.0 if event_type != "general_news" else 0.0
    market_bonus = 10.0 if market_event_candidate else 0.0

    return round(
        (source_weight(source) * 10.0)
        + recency
        + (mention_score * 4.0)
        + type_bonus
        + market_bonus,
        4,
    )


def build_market_event_lookup(universe: Dict[str, Any]) -> Dict[str, bool]:
    result = {}
    for row in universe.get("all_active_eur_markets") or []:
        if not isinstance(row, dict):
            continue
        market = str(row.get("market") or "")
        if market:
            result[market] = bool(row.get("market_event_candidate"))
    return result


def build_radar(
    universe: Dict[str, Any],
    asset_rows: List[Dict[str, Any]],
    feed_items: List[Dict[str, Any]],
    *,
    max_age_hours: float,
) -> Dict[str, Any]:
    assets = universe_assets(universe, asset_rows)
    market_events = build_market_event_lookup(universe)
    cutoff = now_utc() - timedelta(hours=max_age_hours)

    dedup = set()
    events = []
    skipped_old = 0
    skipped_unmapped = 0

    for item in feed_items:
        published = item.get("_published_dt")
        if published and published < cutoff:
            skipped_old += 1
            continue

        key = item.get("url") or normalized_title(item.get("title") or "")
        if not key or key in dedup:
            continue
        dedup.add(key)

        text = f"{item.get('title','')} {item.get('summary','')}"
        mentions = detect_assets(text, assets)
        if not mentions:
            skipped_unmapped += 1
            continue

        event_type, keyword_hits = classify_event(text)
        hint = impact_hint(text)

        linked = []
        for mention in mentions:
            market = mention["market"]
            market_event = bool(market_events.get(market, False))
            score = event_score(
                source=str(item.get("source") or ""),
                published=published,
                mention_score=int(mention["mention_score"]),
                event_type=event_type,
                market_event_candidate=market_event,
            )
            linked.append({
                **mention,
                "market_event_candidate": market_event,
                "event_score": score,
            })

        linked.sort(key=lambda x: x["event_score"], reverse=True)

        events.append({
            "source": item.get("source"),
            "title": item.get("title"),
            "url": item.get("url"),
            "published_at": item.get("published_at"),
            "event_type": event_type,
            "impact_hint": hint,
            "keyword_hits": keyword_hits,
            "markets": linked,
            "top_event_score": linked[0]["event_score"] if linked else 0.0,
            "news_confirmed": True,
            "live_eligible": False,
            "research_only": True,
        })

    events.sort(key=lambda x: x["top_event_score"], reverse=True)

    per_market: Dict[str, List[Dict[str, Any]]] = {}
    for event in events:
        for market in event["markets"]:
            per_market.setdefault(market["market"], []).append({
                "source": event["source"],
                "title": event["title"],
                "url": event["url"],
                "published_at": event["published_at"],
                "event_type": event["event_type"],
                "impact_hint": event["impact_hint"],
                "mention_confidence": market["mention_confidence"],
                "market_event_candidate": market["market_event_candidate"],
                "event_score": market["event_score"],
            })

    market_summary = []
    for market, rows in per_market.items():
        rows.sort(key=lambda x: x["event_score"], reverse=True)
        market_summary.append({
            "market": market,
            "news_events": len(rows),
            "best_score": rows[0]["event_score"],
            "market_event_confirmed_by_news": any(
                row["market_event_candidate"] for row in rows
            ),
            "latest": rows[:5],
            "live_eligible": False,
            "research_only": True,
        })

    market_summary.sort(
        key=lambda x: (
            x["market_event_confirmed_by_news"],
            x["best_score"],
            x["news_events"],
        ),
        reverse=True,
    )

    return {
        "version": VERSION,
        "generated_at": now_iso(),
        "max_age_hours": max_age_hours,
        "research_only": True,
        "private_api_used": False,
        "orders_used": False,
        "automatic_live_change": False,
        "universe_assets": len(assets),
        "counts": {
            "feed_items_received": len(feed_items),
            "mapped_news_events": len(events),
            "markets_with_news": len(market_summary),
            "market_event_plus_news": sum(
                1 for row in market_summary
                if row["market_event_confirmed_by_news"]
            ),
            "skipped_old": skipped_old,
            "skipped_unmapped": skipped_unmapped,
        },
        "top_news_events": events[:40],
        "market_summary": market_summary[:80],
    }


def print_report(result: Dict[str, Any], feed_status: List[Dict[str, Any]]) -> None:
    c = result["counts"]

    print("=" * 78)
    print(f" DIAMOND BROAD CRYPTO NEWS / EVENT RADAR v{VERSION}")
    print("=" * 78)
    print(f"Universe assets      : {result['universe_assets']}")
    print(f"RSS items ontvangen  : {c['feed_items_received']}")
    print(f"Nieuws-events gemapt : {c['mapped_news_events']}")
    print(f"Markten met nieuws   : {c['markets_with_news']}")
    print(f"Market+news bevestigd: {c['market_event_plus_news']}")
    print(f"Venster               : laatste {result['max_age_hours']:.0f} uur")
    print("Research-only         : JA")

    print("\n=== BRONSTATUS ===")
    for row in feed_status:
        print(
            f"{row['source']:<15} "
            f"{row['status']:<5} "
            f"items={row['items']}"
        )

    print("\n=== TOP NEWS / EVENT KANDIDATEN ===")
    if not result["top_news_events"]:
        print("Geen gemapte recente events.")
    else:
        for event in result["top_news_events"][:12]:
            top = event["markets"][0]
            print(
                f"{top['market']:<12} "
                f"{event['event_type']:<12} "
                f"{event['impact_hint']:<17} "
                f"score={top['event_score']:>6.1f} "
                f"[{event['source']}]"
            )
            print(f"  {event['title'][:100]}")

    print("\n=== MARKET + NIEUWS BEVESTIGING ===")
    confirmed = [
        row for row in result["market_summary"]
        if row["market_event_confirmed_by_news"]
    ]
    if not confirmed:
        print("Geen overlap tussen markt-event en nieuws in dit venster.")
    else:
        for row in confirmed[:10]:
            latest = row["latest"][0]
            print(
                f"{row['market']:<12} "
                f"nieuws={row['news_events']:<2} "
                f"score={row['best_score']:>6.1f} "
                f"{latest['event_type']} | {latest['impact_hint']}"
            )

    print("\nImpact hint is alleen keyword-research, geen koop/verkoopadvies.")
    print("Live toelating      : NEE")
    print("Orders/private API  : NEE")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--universe",
        default=str(UNIVERSE_PATH),
    )
    parser.add_argument(
        "--output",
        default=str(OUTPUT_PATH),
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=DEFAULT_MAX_AGE_HOURS,
    )
    parser.add_argument(
        "--max-items-per-feed",
        type=int,
        default=DEFAULT_MAX_ITEMS_PER_FEED,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    universe = load_json(Path(args.universe))
    if not universe.get("all_active_eur_markets"):
        print("=" * 78)
        print(f" DIAMOND BROAD CRYPTO NEWS / EVENT RADAR v{VERSION}")
        print("=" * 78)
        print("STATUS : WAIT_UNIVERSE")
        print("Draai eerst diamond_dynamic_bitvavo_universe.py")
        print("Orders/private API : NEE")
        return 1

    try:
        asset_rows = fetch_assets()
    except Exception as exc:
        print("=" * 78)
        print(f" DIAMOND BROAD CRYPTO NEWS / EVENT RADAR v{VERSION}")
        print("=" * 78)
        print(f"STATUS : FAIL_ASSETS | {type(exc).__name__}")
        print("Orders/private API : NEE")
        return 2

    feed_items: List[Dict[str, Any]] = []
    feed_status: List[Dict[str, Any]] = []

    for source, url in DEFAULT_FEEDS:
        try:
            raw = http_get(url)
            rows = parse_feed(raw, source, url)
            rows = rows[:max(1, args.max_items_per_feed)]
            feed_items.extend(rows)
            feed_status.append({
                "source": source,
                "status": "PASS",
                "items": len(rows),
            })
        except Exception as exc:
            feed_status.append({
                "source": source,
                "status": "FAIL",
                "items": 0,
                "error": type(exc).__name__,
            })

    if not any(row["status"] == "PASS" for row in feed_status):
        print("=" * 78)
        print(f" DIAMOND BROAD CRYPTO NEWS / EVENT RADAR v{VERSION}")
        print("=" * 78)
        print("STATUS : FAIL_FEEDS")
        for row in feed_status:
            print(f"{row['source']}: {row.get('error','FAIL')}")
        print("Orders/private API : NEE")
        return 2

    result = build_radar(
        universe,
        asset_rows,
        feed_items,
        max_age_hours=max(1.0, args.max_age_hours),
    )
    result["feed_status"] = feed_status
    atomic_json(Path(args.output), result)
    print_report(result, feed_status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
