#!/usr/bin/env python3
"""
Diamond Trader SELECTIVE Filter Candidate Compare v1.0

Read-only counterfactual vergelijking op bestaande gesloten SELECTIVE shadow-trades.

Doel:
- niet zomaar LONG/SHORT/RR gaan wijzigen;
- een paar EENVOUDIGE kandidaatregels vergelijken die rechtstreeks uit de
  robustness-analyse voortkomen;
- laten zien wanneer ogenschijnlijk verschillende sterke groepen feitelijk
  dezelfde trades bevatten;
- geen enkele regel toepassen op live/shadow/config.

Bron:
  /var/data/diamond_scanner_selective_shadow_trades.csv

Rapport:
  /var/data/diamond_selective_filter_candidate_compare.json
"""

from __future__ import annotations

import csv
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Tuple

VERSION = "1.0"
DATA = Path(os.getenv("DIAMOND_DATA_DIR", "/var/data"))
SOURCE = DATA / "diamond_scanner_selective_shadow_trades.csv"
OUTPUT = DATA / "diamond_selective_filter_candidate_compare.json"

MIN_SAMPLE = 8

SAFETY = {
    "historical_only": True,
    "orders": False,
    "private_api": False,
    "network": False,
    "strategy_change": False,
    "filter_change": False,
    "config_change": False,
    "stake_change": False,
    "live_change": False,
    "automatic_shadow_change": False,
}


def f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def load_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "variant", "candidate_key", "closed_at", "symbol", "strategy",
            "side", "market_regime", "reward_risk", "signal_score",
            "net_pnl_eur",
        }
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise RuntimeError(
                "CSV mist kolommen: " + ", ".join(sorted(missing))
            )

        rows = []
        fallback_index = 0
        for raw in reader:
            if str(raw.get("variant") or "").strip().upper() != "SELECTIVE":
                continue
            if not str(raw.get("closed_at") or "").strip():
                continue

            fallback_index += 1
            row = dict(raw)
            row["_id"] = (
                str(raw.get("candidate_key") or "").strip()
                or f"row-{fallback_index}"
            )
            row["side"] = str(raw.get("side") or "UNKNOWN").upper()
            row["strategy"] = str(raw.get("strategy") or "UNKNOWN")
            row["market_regime"] = str(raw.get("market_regime") or "UNKNOWN")
            row["symbol"] = str(raw.get("symbol") or "UNKNOWN")
            row["reward_risk"] = f(raw.get("reward_risk"))
            row["signal_score"] = f(raw.get("signal_score"))
            row["net_pnl_eur"] = f(raw.get("net_pnl_eur"))
            rows.append(row)

    return rows


def pf(rows: Iterable[Dict[str, Any]]) -> float | None:
    pnl = [f(r.get("net_pnl_eur")) for r in rows]
    gp = sum(x for x in pnl if x > 0)
    gl = abs(sum(x for x in pnl if x < 0))
    if gl > 0:
        return gp / gl
    if gp > 0:
        return math.inf
    return None


def pf_out(value: float | None) -> float | None:
    if value is None:
        return None
    if math.isinf(value):
        return math.inf
    return round(value, 4)


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    ordered = sorted(rows, key=lambda r: f(r.get("net_pnl_eur")), reverse=True)
    pnl = [f(r.get("net_pnl_eur")) for r in rows]
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x < 0]
    best = ordered[0] if ordered else None
    without_best = ordered[1:] if len(ordered) > 1 else []

    wb_pnl = sum(f(r.get("net_pnl_eur")) for r in without_best)
    p = pf(rows)
    wb_pf = pf(without_best)

    if len(rows) < MIN_SAMPLE:
        quality = "SMALL_SAMPLE"
    elif sum(pnl) <= 0:
        quality = "WEAK"
    elif wb_pnl <= 0:
        quality = "FRAGILE"
    elif wb_pf is not None and (math.isinf(wb_pf) or wb_pf >= 1.20):
        quality = "ROBUST_HISTORICAL"
    else:
        quality = "POSITIVE_THIN"

    return {
        "n": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(rows), 4) if rows else None,
        "pnl_eur": round(sum(pnl), 4),
        "profit_factor": pf_out(p),
        "avg_trade_eur": round(sum(pnl) / len(rows), 4) if rows else None,
        "best_trade_symbol": best.get("symbol") if best else None,
        "best_trade_pnl_eur": round(f(best.get("net_pnl_eur")), 4) if best else None,
        "without_best_pnl_eur": round(wb_pnl, 4),
        "without_best_profit_factor": pf_out(wb_pf),
        "quality": quality,
    }


def rr_between(row: Dict[str, Any], low: float, high: float) -> bool:
    rr = f(row.get("reward_risk"))
    return low <= rr < high


def rule_definitions() -> List[Tuple[str, str, Callable[[Dict[str, Any]], bool]]]:
    return [
        (
            "CURRENT_ALL",
            "Huidige SELECTIVE dataset; referentie.",
            lambda r: True,
        ),
        (
            "LONG_ALL",
            "Alle LONG SELECTIVE trades.",
            lambda r: r["side"] == "LONG",
        ),
        (
            "SHORT_ALL",
            "Alle SHORT SELECTIVE trades.",
            lambda r: r["side"] == "SHORT",
        ),
        (
            "LONG_TREND_BREAKOUT",
            "LONG + trend_breakout.",
            lambda r: (
                r["side"] == "LONG"
                and r["strategy"] == "trend_breakout"
            ),
        ),
        (
            "BULLISH_FAMILY",
            "BULLISH of BULLISH_WEAK.",
            lambda r: r["market_regime"] in {"BULLISH", "BULLISH_WEAK"},
        ),
        (
            "LONG_RR_160_199",
            "LONG met R/R 1.60-1.99.",
            lambda r: (
                r["side"] == "LONG"
                and rr_between(r, 1.60, 2.00)
            ),
        ),
        (
            "SHORT_RR_140_159",
            "SHORT met R/R 1.40-1.59.",
            lambda r: (
                r["side"] == "SHORT"
                and rr_between(r, 1.40, 1.60)
            ),
        ),
        (
            "GUARDED_MIX",
            "Alle LONG + alleen SHORT met R/R 1.40-1.59.",
            lambda r: (
                r["side"] == "LONG"
                or (
                    r["side"] == "SHORT"
                    and rr_between(r, 1.40, 1.60)
                )
            ),
        ),
        (
            "RR_GE_160",
            "Alle trades met R/R >=1.60.",
            lambda r: f(r.get("reward_risk")) >= 1.60,
        ),
        (
            "RR_GE_140",
            "Alle trades met R/R >=1.40.",
            lambda r: f(r.get("reward_risk")) >= 1.40,
        ),
    ]


def ids(rows: List[Dict[str, Any]]) -> set[str]:
    return {str(r["_id"]) for r in rows}


def overlap(a: List[Dict[str, Any]], b: List[Dict[str, Any]]) -> Dict[str, Any]:
    a_ids = ids(a)
    b_ids = ids(b)
    inter = len(a_ids & b_ids)
    union = len(a_ids | b_ids)
    jaccard = inter / union if union else 1.0
    return {
        "intersection": inter,
        "union": union,
        "jaccard": round(jaccard, 4),
        "identical": a_ids == b_ids,
    }


def build(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    selected: Dict[str, List[Dict[str, Any]]] = {}
    results = []

    for name, description, predicate in rule_definitions():
        subset = [row for row in rows if predicate(row)]
        selected[name] = subset
        results.append({
            "rule": name,
            "description": description,
            **summarize(subset),
        })

    # Belangrijk: sterke labels niet dubbel tellen wanneer dezelfde trades
    # meerdere namen hebben.
    compare_pairs = [
        ("LONG_ALL", "LONG_TREND_BREAKOUT"),
        ("LONG_ALL", "BULLISH_FAMILY"),
        ("LONG_TREND_BREAKOUT", "BULLISH_FAMILY"),
        ("LONG_ALL", "LONG_RR_160_199"),
        ("SHORT_ALL", "SHORT_RR_140_159"),
    ]
    overlaps = []
    for left, right in compare_pairs:
        overlaps.append({
            "left": left,
            "right": right,
            **overlap(selected[left], selected[right]),
        })

    return {
        "version": VERSION,
        "source": str(SOURCE),
        "closed_selective_rows": len(rows),
        "minimum_sample": MIN_SAMPLE,
        "rules": results,
        "overlap_checks": overlaps,
        "safety": SAFETY,
    }


def atomic_write(path: Path, payload: Dict[str, Any]) -> None:
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
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


def pf_text(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        x = float(value)
    except Exception:
        return "n/a"
    return "INF" if math.isinf(x) else f"{x:.4f}"


def main() -> int:
    try:
        rows = load_rows(SOURCE)
    except Exception as exc:
        print("=" * 104)
        print(f" DIAMOND SELECTIVE FILTER CANDIDATE COMPARE v{VERSION}")
        print("=" * 104)
        print(f"STATUS: BRONFOUT | {type(exc).__name__}: {exc}")
        print("Filters/live/config/orders/private API: ONGEWIJZIGD")
        return 2

    report = build(rows)
    atomic_write(OUTPUT, report)

    print("=" * 104)
    print(f" DIAMOND SELECTIVE FILTER CANDIDATE COMPARE v{VERSION}")
    print("=" * 104)

    print("=== HISTORISCHE KANDIDAATREGELS ===")
    for row in report["rules"]:
        print(
            f"{row['rule']:<24} "
            f"n={row['n']:>2} W/L={row['wins']}/{row['losses']} "
            f"PnL=€{row['pnl_eur']:+.4f} "
            f"PF={pf_text(row['profit_factor'])} | "
            f"zonder beste=€{row['without_best_pnl_eur']:+.4f} "
            f"PF={pf_text(row['without_best_profit_factor'])} "
            f"[{row['quality']}]"
        )

    print("\n=== OVERLAP / DUBBELE BEWIJSWAARDE ===")
    for row in report["overlap_checks"]:
        print(
            f"{row['left']:<22} vs {row['right']:<22} "
            f"overlap={row['intersection']}/{row['union']} "
            f"J={row['jaccard']:.3f} "
            f"identiek={'JA' if row['identical'] else 'NEE'}"
        )

    print("\n=== INTERPRETATIEBEVEILIGING ===")
    print("ROBUST_HISTORICAL is GEEN live-goedkeuring.")
    print("Regels met dezelfde trades tellen niet als onafhankelijk bewijs.")
    print("Nieuwe filterregels mogen alleen prospectief in shadow worden getest.")

    print("\n=== VEILIGHEID ===")
    print("Filters gewijzigd      : NEE")
    print("Strategie gewijzigd    : NEE")
    print("Auto shadow gewijzigd  : NEE")
    print("Stake/config/live      : NEE")
    print("Orders/private API     : NEE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
