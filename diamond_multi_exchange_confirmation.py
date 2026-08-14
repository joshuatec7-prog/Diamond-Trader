#!/usr/bin/env python3
# Diamond Trader Multi-Exchange Confirmation v1.0
#
# Publieke, read-only marktbevestiging via Binance + Coinbase naast Bitvavo.
# Bitvavo blijft de enige beoogde execution-route; dit script plaatst nergens orders.
#
# Vergelijkt vooral 24h RICHTING en beweging. Absolute prijzen tussen EUR/USD/
# stablecoin-quotes worden bewust niet gebruikt als harde trading-trigger.

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


VERSION = "1.0"
ROOT = Path(__file__).resolve().parent
DATA = Path("/var/data")

SCHEDULE_PATH = DATA / "diamond_deep_scan_schedule.json"
OUTPUT_PATH = DATA / "diamond_multi_exchange_confirmation.json"
SCHEDULER_HELPER = ROOT / "diamond_dynamic_deep_scan_scheduler.py"

BINANCE_BASE = "https://data-api.binance.vision"
COINBASE_BASE = "https://api.exchange.coinbase.com"

MAX_CANDIDATES = 15
DIRECTION_DEADBAND_PCT = 0.50
MOVE_DIFF_WARNING_PP = 5.0

BINANCE_QUOTES = ("EUR", "USDT", "USDC", "FDUSD")
COINBASE_QUOTES = ("EUR", "USD", "USDC")


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


def http_json(url: str, timeout: int = 12) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Diamond-Trader-Multi-Exchange/1.0",
            "Cache-Control": "no-cache",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def run_helper(path: Path) -> Tuple[int, str]:
    if not path.exists():
        return 127, f"ONTBREEKT:{path.name}"
    try:
        result = subprocess.run(
            ["python3", str(path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        combined = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        return result.returncode, combined
    except Exception as exc:
        return 126, f"{type(exc).__name__}:{exc}"


def direction(change_pct: float) -> str:
    if change_pct > DIRECTION_DEADBAND_PCT:
        return "UP"
    if change_pct < -DIRECTION_DEADBAND_PCT:
        return "DOWN"
    return "FLAT"


def candidate_rows(schedule: Dict[str, Any]) -> List[Dict[str, Any]]:
    # DEEP eerst, daarna WATCH op priority_score. BASE wordt niet zwaar
    # bevraagd op externe exchanges; die blijft via punt 5 licht gevolgd.
    rows = []
    seen = set()

    for section in ("deep_markets", "watch_markets"):
        for row in schedule.get(section) or []:
            if not isinstance(row, dict):
                continue
            market = str(row.get("market") or "")
            if not market or market in seen:
                continue
            seen.add(market)
            rows.append(row)

    rows.sort(
        key=lambda row: (
            str(row.get("tier") or "") == "DEEP",
            to_float(row.get("priority_score"), 0.0),
        ),
        reverse=True,
    )
    return rows[:MAX_CANDIDATES]


def choose_pair(
    products: List[Dict[str, Any]],
    base: str,
    quote_priority: Tuple[str, ...],
    *,
    base_key: str,
    quote_key: str,
    id_key: str,
    status_key: Optional[str] = None,
    allowed_status: Optional[set[str]] = None,
) -> Optional[Dict[str, Any]]:
    candidates = []
    for product in products:
        if not isinstance(product, dict):
            continue
        if str(product.get(base_key) or "").upper() != base:
            continue

        quote = str(product.get(quote_key) or "").upper()
        if quote not in quote_priority:
            continue

        if status_key and allowed_status is not None:
            status = str(product.get(status_key) or "").upper()
            if status not in allowed_status:
                continue

        candidates.append(product)

    if not candidates:
        return None

    rank = {quote: index for index, quote in enumerate(quote_priority)}
    candidates.sort(
        key=lambda product: rank.get(
            str(product.get(quote_key) or "").upper(),
            999,
        )
    )
    return candidates[0]


def fetch_binance_snapshot() -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    status = {
        "exchange": "binance",
        "status": "FAIL",
        "products": 0,
        "tickers": 0,
        "error": None,
    }
    try:
        info = http_json(f"{BINANCE_BASE}/api/v3/exchangeInfo")
        tickers = http_json(f"{BINANCE_BASE}/api/v3/ticker/24hr")

        symbols = [
            row for row in (info.get("symbols") or [])
            if isinstance(row, dict)
        ]
        ticker_rows = [
            row for row in tickers
            if isinstance(row, dict)
        ]

        ticker_by_symbol = {
            str(row.get("symbol") or ""): row
            for row in ticker_rows
            if row.get("symbol")
        }

        products = []
        for row in symbols:
            if str(row.get("status") or "").upper() != "TRADING":
                continue
            products.append({
                "id": str(row.get("symbol") or ""),
                "base": str(row.get("baseAsset") or "").upper(),
                "quote": str(row.get("quoteAsset") or "").upper(),
                "status": "TRADING",
            })

        by_base = {}
        bases = sorted({row["base"] for row in products if row["base"]})
        for base in bases:
            product = choose_pair(
                products,
                base,
                BINANCE_QUOTES,
                base_key="base",
                quote_key="quote",
                id_key="id",
                status_key="status",
                allowed_status={"TRADING"},
            )
            if not product:
                continue

            ticker = ticker_by_symbol.get(product["id"])
            if not ticker:
                continue

            by_base[base] = {
                "exchange": "binance",
                "pair": product["id"],
                "quote": product["quote"],
                "last": to_float(ticker.get("lastPrice")),
                "change_24h_pct": to_float(ticker.get("priceChangePercent")),
                "quote_volume_24h": to_float(ticker.get("quoteVolume")),
            }

        status.update({
            "status": "PASS",
            "products": len(products),
            "tickers": len(ticker_rows),
        })
        return by_base, status

    except Exception as exc:
        status["error"] = type(exc).__name__
        return {}, status


def fetch_coinbase_products() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    status = {
        "exchange": "coinbase",
        "status": "FAIL",
        "products": 0,
        "stats_ok": 0,
        "stats_fail": 0,
        "error": None,
    }
    try:
        payload = http_json(f"{COINBASE_BASE}/products")
        products = [
            row for row in payload
            if isinstance(row, dict)
            and str(row.get("status") or "").lower() == "online"
        ]
        status["products"] = len(products)
        status["status"] = "PASS"
        return products, status
    except Exception as exc:
        status["error"] = type(exc).__name__
        return [], status


def fetch_coinbase_stat(product_id: str) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
    try:
        encoded = urllib.parse.quote(product_id, safe="-")
        payload = http_json(
            f"{COINBASE_BASE}/products/{encoded}/stats",
            timeout=10,
        )
        open_price = to_float(payload.get("open"))
        last = to_float(payload.get("last"))
        if open_price <= 0 or last <= 0:
            return product_id, None, "ONGELDIGE_PRIJS"

        change = ((last - open_price) / open_price) * 100.0
        return product_id, {
            "last": last,
            "change_24h_pct": change,
            "base_volume_24h": to_float(payload.get("volume")),
        }, None
    except Exception as exc:
        return product_id, None, type(exc).__name__


def build_coinbase_snapshot(
    candidate_bases: List[str],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    products, status = fetch_coinbase_products()
    if not products:
        return {}, status

    selected: Dict[str, Dict[str, Any]] = {}
    for base in candidate_bases:
        product = choose_pair(
            products,
            base,
            COINBASE_QUOTES,
            base_key="base_currency",
            quote_key="quote_currency",
            id_key="id",
            status_key="status",
            allowed_status={"ONLINE"},
        )
        if product:
            selected[base] = product

    results: Dict[str, Dict[str, Any]] = {}

    # Kleine begrensde paralleliteit: alleen top-kandidaten, publieke stats.
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(fetch_coinbase_stat, str(product["id"])): base
            for base, product in selected.items()
        }
        for future in concurrent.futures.as_completed(futures):
            base = futures[future]
            product = selected[base]
            product_id, stat, error = future.result()
            if stat is None:
                status["stats_fail"] += 1
                continue

            status["stats_ok"] += 1
            results[base] = {
                "exchange": "coinbase",
                "pair": product_id,
                "quote": str(product.get("quote_currency") or "").upper(),
                **stat,
            }

    if status["stats_ok"] == 0:
        status["status"] = "PARTIAL" if status["products"] else "FAIL"

    return results, status


def compare_market(
    row: Dict[str, Any],
    binance: Optional[Dict[str, Any]],
    coinbase: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    market = str(row.get("market") or "")
    base = market.removesuffix("-EUR")
    bitvavo_change = to_float(row.get("change_24h_pct"), 0.0)
    bitvavo_direction = direction(bitvavo_change)

    external = []
    for snapshot in (binance, coinbase):
        if not snapshot:
            continue
        change = to_float(snapshot.get("change_24h_pct"), 0.0)
        ext_direction = direction(change)
        external.append({
            **snapshot,
            "direction": ext_direction,
            "aligned_with_bitvavo": (
                bitvavo_direction in {"UP", "DOWN"}
                and ext_direction == bitvavo_direction
            ),
            "move_difference_pp": round(
                abs(change - bitvavo_change),
                4,
            ),
            "move_difference_warning": (
                abs(change - bitvavo_change) > MOVE_DIFF_WARNING_PP
            ),
        })

    available = len(external)
    aligned = sum(1 for item in external if item["aligned_with_bitvavo"])
    disagree = sum(
        1 for item in external
        if bitvavo_direction in {"UP", "DOWN"}
        and item["direction"] in {"UP", "DOWN"}
        and item["direction"] != bitvavo_direction
    )

    if bitvavo_direction == "FLAT":
        status = "BITVAVO_FLAT"
    elif available >= 2 and aligned == available:
        status = "CONFIRMED_2X"
    elif available >= 1 and aligned >= 1 and disagree == 0:
        status = "CONFIRMED_PARTIAL"
    elif disagree >= 1 and aligned >= 1:
        status = "MIXED"
    elif disagree >= 1:
        status = "OPPOSED"
    else:
        status = "NO_EXTERNAL_COVERAGE"

    return {
        "market": market,
        "base": base,
        "tier": row.get("tier"),
        "priority_score": to_float(row.get("priority_score"), 0.0),
        "fusion_status": row.get("fusion_status"),
        "bitvavo_change_24h_pct": bitvavo_change,
        "bitvavo_direction": bitvavo_direction,
        "external_exchanges_available": available,
        "external_aligned": aligned,
        "external_opposed": disagree,
        "confirmation_status": status,
        "external": external,
        "research_confirmation_only": True,
        "shadow_eligible_changed": False,
        "live_eligible": False,
    }


def build_report(
    schedule: Dict[str, Any],
    binance_data: Dict[str, Dict[str, Any]],
    coinbase_data: Dict[str, Dict[str, Any]],
    exchange_status: List[Dict[str, Any]],
) -> Dict[str, Any]:
    candidates = candidate_rows(schedule)
    rows = []

    for row in candidates:
        market = str(row.get("market") or "")
        base = market.removesuffix("-EUR")
        rows.append(
            compare_market(
                row,
                binance_data.get(base),
                coinbase_data.get(base),
            )
        )

    status_order = {
        "CONFIRMED_2X": 6,
        "CONFIRMED_PARTIAL": 5,
        "MIXED": 4,
        "OPPOSED": 3,
        "BITVAVO_FLAT": 2,
        "NO_EXTERNAL_COVERAGE": 1,
    }
    rows.sort(
        key=lambda row: (
            status_order.get(row["confirmation_status"], 0),
            row["priority_score"],
        ),
        reverse=True,
    )

    counts: Dict[str, int] = {}
    for row in rows:
        key = row["confirmation_status"]
        counts[key] = counts.get(key, 0) + 1

    return {
        "version": VERSION,
        "generated_at": now_iso(),
        "research_only": True,
        "bitvavo_execution_route_unchanged": True,
        "external_execution_allowed": False,
        "private_api_used": False,
        "orders_used": False,
        "config_changed": False,
        "symbols_changed": False,
        "automatic_shadow_promotion": False,
        "automatic_live_change": False,
        "live_eligible_markets": 0,
        "thresholds": {
            "direction_deadband_pct": DIRECTION_DEADBAND_PCT,
            "move_difference_warning_pp": MOVE_DIFF_WARNING_PP,
            "max_candidates": MAX_CANDIDATES,
        },
        "exchange_status": exchange_status,
        "counts": {
            "candidates_checked": len(rows),
            "confirmed_2x": counts.get("CONFIRMED_2X", 0),
            "confirmed_partial": counts.get("CONFIRMED_PARTIAL", 0),
            "mixed": counts.get("MIXED", 0),
            "opposed": counts.get("OPPOSED", 0),
            "flat": counts.get("BITVAVO_FLAT", 0),
            "no_external_coverage": counts.get("NO_EXTERNAL_COVERAGE", 0),
        },
        "markets": rows,
    }


def print_report(result: Dict[str, Any]) -> None:
    c = result["counts"]

    print("=" * 78)
    print(f" DIAMOND MULTI-EXCHANGE CONFIRMATION v{VERSION}")
    print("=" * 78)

    print("=== BRONSTATUS ===")
    for source in result["exchange_status"]:
        extra = ""
        if source["exchange"] == "coinbase":
            extra = (
                f" stats_ok={source.get('stats_ok',0)}"
                f" stats_fail={source.get('stats_fail',0)}"
            )
        print(
            f"{source['exchange']:<10} "
            f"{source['status']:<7} "
            f"products={source.get('products',0)}"
            f"{extra}"
        )

    print("\n=== SAMENVATTING ===")
    print(f"Kandidaten gecontroleerd : {c['candidates_checked']}")
    print(f"CONFIRMED_2X            : {c['confirmed_2x']}")
    print(f"CONFIRMED_PARTIAL       : {c['confirmed_partial']}")
    print(f"MIXED                   : {c['mixed']}")
    print(f"OPPOSED                 : {c['opposed']}")
    print(f"BITVAVO_FLAT            : {c['flat']}")
    print(f"GEEN EXTERNE DEKKING    : {c['no_external_coverage']}")

    print("\n=== KANDIDATEN ===")
    if not result["markets"]:
        print("Geen DEEP/WATCH-kandidaten.")
    else:
        for row in result["markets"]:
            print(
                f"{row['market']:<12} "
                f"BV={row['bitvavo_change_24h_pct']:>+7.2f}% "
                f"{row['bitvavo_direction']:<4} "
                f"[{row['confirmation_status']}]"
            )
            if row["external"]:
                parts = []
                for ext in row["external"]:
                    flag = "OK" if ext["aligned_with_bitvavo"] else ext["direction"]
                    diff = " !DIFF" if ext["move_difference_warning"] else ""
                    parts.append(
                        f"{ext['exchange']} {ext['pair']} "
                        f"{ext['change_24h_pct']:+.2f}% {flag}{diff}"
                    )
                print("  " + " | ".join(parts))

    print("\nExterne beursdata is alleen extra research/bevestiging.")
    print("Bitvavo execution-route : ONGEWIJZIGD")
    print("Externe orders          : NEE")
    print("Auto shadow promotie    : NEE")
    print("Live toelating          : NEE")
    print("Private API             : NEE")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--schedule",
        default=str(SCHEDULE_PATH),
    )
    parser.add_argument(
        "--output",
        default=str(OUTPUT_PATH),
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Gebruik bestaand deep-scan schema zonder punt 5 opnieuw te draaien.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.no_refresh:
        rc, helper_text = run_helper(SCHEDULER_HELPER)
        if rc != 0:
            print("=" * 78)
            print(f" DIAMOND MULTI-EXCHANGE CONFIRMATION v{VERSION}")
            print("=" * 78)
            print("STATUS : WAIT_SCHEDULER")
            if helper_text:
                print(helper_text.splitlines()[-1])
            print("Orders/private API : NEE")
            return 1

    schedule = load_json(Path(args.schedule))
    if not (
        schedule.get("deep_markets") is not None
        and schedule.get("watch_markets") is not None
    ):
        print("STATUS : WAIT_SCHEDULE_DATA")
        print("Orders/private API : NEE")
        return 1

    candidates = candidate_rows(schedule)
    bases = [
        str(row.get("market") or "").removesuffix("-EUR")
        for row in candidates
        if row.get("market")
    ]

    binance_data, binance_status = fetch_binance_snapshot()
    coinbase_data, coinbase_status = build_coinbase_snapshot(bases)

    report = build_report(
        schedule,
        binance_data,
        coinbase_data,
        [binance_status, coinbase_status],
    )
    atomic_json(Path(args.output), report)
    print_report(report)

    # Fail-soft: één externe bron mag ontbreken; geen bron = warning exit.
    sources_ok = sum(
        1 for row in report["exchange_status"]
        if row.get("status") in {"PASS", "PARTIAL"}
    )
    return 0 if sources_ok >= 1 else 2


if __name__ == "__main__":
    raise SystemExit(main())
