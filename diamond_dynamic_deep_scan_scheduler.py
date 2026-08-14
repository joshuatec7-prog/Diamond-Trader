#!/usr/bin/env python3
# Diamond Trader Dynamic Deep-Scan Scheduler v1.0
#
# Maakt een dynamisch scanplan voor ALLE actieve Bitvavo EUR-markten.
# De hele markt blijft licht gevolgd; alleen kansrijke munten krijgen
# extra deep-scan capaciteit.
#
# Dit script plant alleen. Het haalt zelf geen candles/trades op, plaatst
# geen orders en wijzigt geen live/config/symbolen.

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


VERSION = "1.0"
ROOT = Path(__file__).resolve().parent
DATA = Path("/var/data")

FUSION_PATH = DATA / "diamond_event_market_fusion.json"
ADMISSION_PATH = DATA / "diamond_shadow_admission_queue.json"
OUTPUT_PATH = DATA / "diamond_deep_scan_schedule.json"

FUSION_HELPER = ROOT / "diamond_event_market_fusion.py"

# Conservatieve research-capaciteit.
MAX_DEEP_MARKETS = 20
MAX_WATCH_MARKETS = 60

# De cadence bepaalt hoe vaak een markt voor een zwaardere per-market scan
# in aanmerking komt. De universe scanner blijft daarnaast ALLE markten
# via batch/public discovery volgen.
DEEP_CADENCE_MIN = 5
WATCH_CADENCE_MIN = 15
BASE_CADENCE_MIN = 60

# Maximum aantal per-market deep/watch requests dat we in één 5-minuten-slot
# willen plannen. Dit is een intern veiligheidsbudget, geen exchange-limietclaim.
MAX_HEAVY_REQUESTS_PER_SLOT = 40

CORE_MARKETS = {
    "BTC-EUR",
    "ETH-EUR",
    "SOL-EUR",
    "XRP-EUR",
    "ADA-EUR",
}

DEEP_STATUSES = {
    "FUSED_STRONG",
    "FUSED",
}

WATCH_STATUSES = {
    "FUSED_CONFLICT",
    "NEWS_WATCH",
    "MARKET_WATCH",
}


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
            timeout=240,
            check=False,
        )
        text = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        return result.returncode, text
    except Exception as exc:
        return 126, f"{type(exc).__name__}:{exc}"


def stable_slot(market: str, cadence_min: int) -> int:
    """
    Verdeel markten deterministisch over cadence-slots.
    Hierdoor hoeft niet alles tegelijk een zware scan te krijgen.
    """
    slots = max(1, cadence_min // 5)
    digest = hashlib.sha256(market.encode("utf-8")).digest()
    value = int.from_bytes(digest[:4], "big")
    return value % slots


def current_five_minute_slot(epoch: int | None = None) -> int:
    if epoch is None:
        epoch = int(time.time())
    return epoch // 300


def due_this_slot(
    market: str,
    cadence_min: int,
    slot_5m: int,
) -> bool:
    slots = max(1, cadence_min // 5)
    return (slot_5m % slots) == stable_slot(market, cadence_min)


def admission_markets(admission: Dict[str, Any]) -> set[str]:
    return {
        str(row.get("market") or "")
        for row in admission.get("shadow_admissions") or []
        if isinstance(row, dict) and row.get("market")
    }


def priority_score(
    row: Dict[str, Any],
    admitted: set[str],
) -> float:
    market = str(row.get("market") or "")
    score = to_float(row.get("fusion_score"), 0.0)
    status = str(row.get("fusion_status") or "").upper()
    liquidity = str(row.get("liquidity_status") or "").upper()

    if market in admitted:
        score += 50.0

    if status == "FUSED_STRONG":
        score += 35.0
    elif status == "FUSED":
        score += 25.0
    elif status == "FUSED_CONFLICT":
        score += 12.0
    elif status in {"NEWS_WATCH", "MARKET_WATCH"}:
        score += 8.0

    if bool(row.get("market_event_candidate")):
        score += 8.0

    if bool(row.get("news_present")):
        score += 6.0

    if str(row.get("direction_relation") or "").upper() == "ALIGNED":
        score += 5.0
    elif str(row.get("direction_relation") or "").upper() == "CONFLICT":
        score -= 8.0

    if liquidity == "PASS":
        score += 10.0
    elif liquidity == "LOW":
        score -= 30.0

    # Core blijft zichtbaar, maar krijgt geen automatische extra admission-bonus.
    if market in CORE_MARKETS:
        score += 2.0

    return round(score, 4)


def tier_for(
    row: Dict[str, Any],
    admitted: set[str],
) -> str:
    market = str(row.get("market") or "")
    status = str(row.get("fusion_status") or "").upper()
    liquidity = str(row.get("liquidity_status") or "").upper()

    if liquidity == "LOW":
        return "BASE"

    if market in admitted:
        return "DEEP"

    if status in DEEP_STATUSES and liquidity == "PASS":
        return "DEEP"

    if status in WATCH_STATUSES:
        return "WATCH"

    if bool(row.get("market_event_candidate")) or bool(row.get("news_present")):
        return "WATCH"

    return "BASE"


def compact_row(
    row: Dict[str, Any],
    tier: str,
    score: float,
    cadence: int,
) -> Dict[str, Any]:
    return {
        "market": str(row.get("market") or ""),
        "tier": tier,
        "priority_score": score,
        "fusion_status": str(row.get("fusion_status") or ""),
        "fusion_score": to_float(row.get("fusion_score"), 0.0),
        "liquidity_status": str(row.get("liquidity_status") or ""),
        "spread_pct": to_float(row.get("spread_pct"), 0.0),
        "volume_quote_24h": to_float(row.get("volume_quote_24h"), 0.0),
        "change_24h_pct": to_float(row.get("change_24h_pct"), 0.0),
        "market_event_candidate": bool(row.get("market_event_candidate")),
        "news_present": bool(row.get("news_present")),
        "direction_relation": str(row.get("direction_relation") or "NEUTRAL"),
        "cadence_minutes": cadence,
        "research_only": True,
        "live_eligible": False,
    }


def build_schedule(
    fusion: Dict[str, Any],
    admission: Dict[str, Any],
    *,
    epoch: int | None = None,
) -> Dict[str, Any]:
    rows = [
        row for row in (fusion.get("all_markets") or [])
        if isinstance(row, dict) and row.get("market")
    ]
    admitted = admission_markets(admission)

    ranked = []
    for row in rows:
        tier = tier_for(row, admitted)
        score = priority_score(row, admitted)
        ranked.append((row, tier, score))

    # Eerst bepalen welke markten überhaupt de DEEP/WATCH-capaciteit krijgen.
    deep_pool = sorted(
        [
            (row, tier, score)
            for row, tier, score in ranked
            if tier == "DEEP"
        ],
        key=lambda x: (x[2], to_float(x[0].get("volume_quote_24h"), 0.0)),
        reverse=True,
    )

    watch_pool = sorted(
        [
            (row, tier, score)
            for row, tier, score in ranked
            if tier == "WATCH"
        ],
        key=lambda x: (x[2], to_float(x[0].get("volume_quote_24h"), 0.0)),
        reverse=True,
    )

    deep_selected = deep_pool[:MAX_DEEP_MARKETS]
    deep_markets = {str(row.get("market")) for row, _, _ in deep_selected}

    # Overflow uit DEEP blijft minimaal WATCH zodat kansen niet verdwijnen.
    overflow_deep = [
        (row, "WATCH", score)
        for row, _, score in deep_pool[MAX_DEEP_MARKETS:]
    ]

    watch_combined = watch_pool + overflow_deep
    watch_combined.sort(
        key=lambda x: (x[2], to_float(x[0].get("volume_quote_24h"), 0.0)),
        reverse=True,
    )
    watch_selected = watch_combined[:MAX_WATCH_MARKETS]
    watch_markets = {str(row.get("market")) for row, _, _ in watch_selected}

    # Alles dat niet DEEP/WATCH is blijft BASE. Dus ALLE markten blijven gedekt.
    deep = [
        compact_row(row, "DEEP", score, DEEP_CADENCE_MIN)
        for row, _, score in deep_selected
    ]

    watch = [
        compact_row(row, "WATCH", score, WATCH_CADENCE_MIN)
        for row, _, score in watch_selected
        if str(row.get("market")) not in deep_markets
    ]

    base_rows = []
    for row, _, score in ranked:
        market = str(row.get("market"))
        if market in deep_markets or market in watch_markets:
            continue
        base_rows.append(
            compact_row(row, "BASE", score, BASE_CADENCE_MIN)
        )

    # Bepaal welke per-market zware scans nu in dit 5m-slot gepland worden.
    slot_5m = current_five_minute_slot(epoch)

    due_deep = [
        row for row in deep
        if due_this_slot(
            row["market"],
            DEEP_CADENCE_MIN,
            slot_5m,
        )
    ]

    due_watch = [
        row for row in watch
        if due_this_slot(
            row["market"],
            WATCH_CADENCE_MIN,
            slot_5m,
        )
    ]

    due_heavy = sorted(
        due_deep + due_watch,
        key=lambda row: row["priority_score"],
        reverse=True,
    )

    due_now = due_heavy[:MAX_HEAVY_REQUESTS_PER_SLOT]
    deferred = due_heavy[MAX_HEAVY_REQUESTS_PER_SLOT:]

    return {
        "version": VERSION,
        "generated_at": now_iso(),
        "research_only": True,
        "private_api_used": False,
        "orders_used": False,
        "config_changed": False,
        "symbols_changed": False,
        "automatic_live_change": False,
        "automatic_shadow_trade_start": False,
        "all_markets_still_covered": len(rows) == (
            len(deep) + len(watch) + len(base_rows)
        ),
        "coverage_model": {
            "base_discovery": (
                "ALLE markten blijven via de publieke universe/batch-scan gevolgd; "
                "dit schema bepaalt alleen extra per-market deep/watch capaciteit."
            ),
            "deep_cadence_minutes": DEEP_CADENCE_MIN,
            "watch_cadence_minutes": WATCH_CADENCE_MIN,
            "base_cadence_minutes": BASE_CADENCE_MIN,
            "max_heavy_requests_per_5m_slot": MAX_HEAVY_REQUESTS_PER_SLOT,
            "max_deep_markets": MAX_DEEP_MARKETS,
            "max_watch_markets": MAX_WATCH_MARKETS,
        },
        "counts": {
            "markets_total": len(rows),
            "deep": len(deep),
            "watch": len(watch),
            "base": len(base_rows),
            "due_heavy_now": len(due_now),
            "deferred_by_budget": len(deferred),
        },
        "deep_markets": deep,
        "watch_markets": watch,
        "base_markets": base_rows,
        "due_heavy_now": due_now,
        "deferred_by_budget": deferred,
    }


def print_report(result: Dict[str, Any]) -> None:
    c = result["counts"]

    print("=" * 78)
    print(f" DIAMOND DYNAMIC DEEP-SCAN SCHEDULER v{VERSION}")
    print("=" * 78)
    print(f"Markten totaal      : {c['markets_total']}")
    print(f"DEEP                : {c['deep']}")
    print(f"WATCH               : {c['watch']}")
    print(f"BASE                : {c['base']}")
    print(f"Heavy scans nu      : {c['due_heavy_now']}")
    print(f"Budget-uitgesteld   : {c['deferred_by_budget']}")
    print(
        "Alle markten gedekt : "
        f"{'JA' if result['all_markets_still_covered'] else 'NEE'}"
    )
    print("Research-only       : JA")
    print("Live eligible       : 0")

    print("\n=== DEEP ===")
    if not result["deep_markets"]:
        print("Geen DEEP-kandidaten. Niets wordt geforceerd.")
    else:
        for row in result["deep_markets"][:12]:
            print(
                f"{row['market']:<12} "
                f"prio={row['priority_score']:>6.1f} "
                f"{row['fusion_status']:<15} "
                f"spr={row['spread_pct']:>6.3f}%"
            )

    print("\n=== WATCH ===")
    if not result["watch_markets"]:
        print("Geen WATCH-kandidaten.")
    else:
        for row in result["watch_markets"][:12]:
            print(
                f"{row['market']:<12} "
                f"prio={row['priority_score']:>6.1f} "
                f"{row['fusion_status']:<15} "
                f"24h={row['change_24h_pct']:>+7.2f}%"
            )

    print("\n=== ZWARE SCANS DIE NU AAN DE BEURT ZIJN ===")
    if not result["due_heavy_now"]:
        print("Geen in dit 5-minuten-slot.")
    else:
        for row in result["due_heavy_now"][:15]:
            print(
                f"{row['market']:<12} "
                f"{row['tier']:<5} "
                f"prio={row['priority_score']:>6.1f}"
            )

    print("\nBASE blijft alle overige markten licht volgen.")
    print("Dit script haalt zelf GEEN candles/trades op.")
    print("Auto shadow trade    : NEE")
    print("Live toelating       : NEE")
    print("Orders/private API   : NEE")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fusion",
        default=str(FUSION_PATH),
    )
    parser.add_argument(
        "--admission",
        default=str(ADMISSION_PATH),
    )
    parser.add_argument(
        "--output",
        default=str(OUTPUT_PATH),
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Gebruik bestaande fusion/admission JSON zonder fusion-helper opnieuw te draaien.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.no_refresh:
        rc, text = run_helper(FUSION_HELPER)
        if rc != 0:
            print("=" * 78)
            print(f" DIAMOND DYNAMIC DEEP-SCAN SCHEDULER v{VERSION}")
            print("=" * 78)
            print("STATUS : WAIT_FUSION_HELPER")
            if text:
                print(text.splitlines()[-1])
            print("Orders/private API : NEE")
            return 1

    fusion = load_json(Path(args.fusion))
    admission = load_json(Path(args.admission))

    if not fusion.get("all_markets"):
        print("STATUS : WAIT_FUSION_DATA")
        print("Orders/private API : NEE")
        return 1

    # Admission queue mag ontbreken/oud zijn; zonder admissions werkt de
    # scheduler nog steeds op de fusion-data.
    result = build_schedule(fusion, admission)
    atomic_json(Path(args.output), result)
    print_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
