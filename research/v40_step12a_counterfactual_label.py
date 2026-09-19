#!/usr/bin/env python3
"""Step 12A counterfactual labeler for the prospective shadow day.

READ-ONLY RESEARCH. PUBLIC BITVAVO CANDLES ONLY. NO ORDERS.

For every mature qualified Step 12A candidate, replay the already-frozen
TP10/STOP15 short exit path from the actual L2 sell VWAP when available.
Candidates are not re-selected and L2 thresholds are not changed.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from bitvavo_public import BitvavoPublic
from v40_master_research_dataset import FIVE_MINUTE_MS

API_URL = "https://api.bitvavo.com/v2"
TP_PCT = 10.0
STOP_PCT = 15.0
ROUNDTRIP_COST_PCT = 0.78
HOLD_MIN = {"SHORT": 1440, "RANGE": 720}


def _finite(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError, OverflowError):
        return default
    return x if math.isfinite(x) else default


def _pf(values: list[float]) -> float:
    gp = sum(x for x in values if x > 0)
    gl = -sum(x for x in values if x < 0)
    if gl <= 0:
        return float("inf") if gp > 0 else 0.0
    return gp / gl


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    vals = [_finite(x.get("net_pct")) for x in rows]
    return {
        "n": len(rows),
        "wins": sum(x > 0 for x in vals),
        "losses": sum(x <= 0 for x in vals),
        "win_pct": (sum(x > 0 for x in vals) / len(vals) * 100.0) if vals else None,
        "mean_net_pct": statistics.mean(vals) if vals else None,
        "median_net_pct": statistics.median(vals) if vals else None,
        "profit_factor": _pf(vals) if vals else None,
        "tp": sum(x.get("exit_reason") == "TP10" for x in rows),
        "stop": sum(x.get("exit_reason") == "STOP15" for x in rows),
        "time": sum(x.get("exit_reason") == "TIME" for x in rows),
    }


def _entry_from_l2(cycle: dict[str, Any]) -> tuple[float, str]:
    prices = []
    for s in (cycle.get("l2") or {}).get("samples", []):
        v = _finite(s.get("sell_vwap"))
        if v > 0:
            prices.append(v)
    if prices:
        return float(statistics.median(prices)), "median_l2_sell_vwap"
    sel = cycle.get("selection") or {}
    v = _finite(sel.get("entry_reference"))
    if v <= 0:
        raise RuntimeError("geen bruikbare entryprijs")
    return v, "signal_entry_reference_fallback"


def _label(entry: float, rows: list[Any], horizon_min: int) -> dict[str, Any]:
    take = entry * (1.0 - TP_PCT / 100.0)
    stop = entry * (1.0 + STOP_PCT / 100.0)
    horizon_ms = horizon_min * 60_000
    exit_price = None
    reason = None
    exit_ms = None
    last = None
    for c in rows:
        if int(c.timestamp_ms) <= 0:
            continue
        last = c
        # Same conservative convention as Step 12A runner: STOP first.
        if float(c.high) >= stop:
            exit_price, reason, exit_ms = stop, "STOP15", int(c.timestamp_ms)
            break
        if float(c.low) <= take:
            exit_price, reason, exit_ms = take, "TP10", int(c.timestamp_ms)
            break
    if exit_price is None:
        if last is None:
            raise RuntimeError("geen post-entry candles")
        exit_price = float(last.close)
        reason = "TIME"
        exit_ms = int(last.timestamp_ms)
    gross = (entry - exit_price) / entry * 100.0
    net = gross - ROUNDTRIP_COST_PCT
    return {
        "exit_price": exit_price,
        "exit_reason": reason,
        "exit_candle_ms": exit_ms,
        "gross_pct": gross,
        "net_pct": net,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cycles_jsonl")
    ap.add_argument("--output", default="v40_step12a_counterfactual_result.json")
    ap.add_argument("--now-ms", type=int, default=0)
    args = ap.parse_args()

    cycles = [json.loads(x) for x in Path(args.cycles_jsonl).read_text(encoding="utf-8").splitlines() if x.strip()]
    qualified = [x for x in cycles if x.get("selection")]
    now_ms = args.now_ms or int(time.time() * 1000)

    pending = []
    mature = []
    for c in qualified:
        state = str(c["selection"]["state"])
        horizon_min = HOLD_MIN[state]
        # Candle at signal+horizon must itself be closed: + one 5m candle.
        mature_at = int(c["signal_ms"]) + horizon_min * 60_000 + FIVE_MINUTE_MS
        item = {
            "cycle": c,
            "state": state,
            "market": str(c["selection"]["market"]),
            "horizon_min": horizon_min,
            "mature_at_ms": mature_at,
        }
        (mature if now_ms >= mature_at else pending).append(item)

    # One public candle fetch per market across its complete mature span.
    by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in mature:
        by_market[item["market"]].append(item)

    api = BitvavoPublic(API_URL, timeout_seconds=15, retries=4)
    cache: dict[str, list[Any]] = {}
    errors: list[str] = []
    for market, items in sorted(by_market.items()):
        start = min(int(x["cycle"]["signal_ms"]) + FIVE_MINUTE_MS for x in items)
        end = max(int(x["cycle"]["signal_ms"]) + x["horizon_min"] * 60_000 + FIVE_MINUTE_MS for x in items)
        try:
            cache[market] = api.closed_candles_between(
                market, "5m", start, end,
                now_ms=now_ms, page_limit=1440, max_pages=20,
            )
        except Exception as exc:
            errors.append(f"{market}: {type(exc).__name__}: {exc}")

    labeled = []
    for item in mature:
        c = item["cycle"]
        market = item["market"]
        try:
            entry, entry_source = _entry_from_l2(c)
            start = int(c["signal_ms"]) + FIVE_MINUTE_MS
            last_ts = int(c["signal_ms"]) + item["horizon_min"] * 60_000
            rows = [x for x in cache.get(market, []) if start <= int(x.timestamp_ms) <= last_ts]
            lab = _label(entry, rows, item["horizon_min"])
            labeled.append({
                "signal_ms": int(c["signal_ms"]),
                "market": market,
                "state": item["state"],
                "meta_action": str(c["meta"]["action"]),
                "blockers": list(c["meta"].get("blockers", [])),
                "uncertainty": list(c["meta"].get("uncertainty", [])),
                "entry_price": entry,
                "entry_source": entry_source,
                "prediction": _finite(c["selection"].get("prediction")),
                "threshold": _finite(c["selection"].get("threshold")),
                **lab,
            })
        except Exception as exc:
            errors.append(f"{market} {c['signal_ms']}: {type(exc).__name__}: {exc}")

    blocked = [x for x in labeled if x["meta_action"] == "BLOCK"]
    allowed = [x for x in labeled if x["meta_action"] in {"ALLOW", "REDUCE"}]
    by_state = {s: _summary([x for x in labeled if x["state"] == s]) for s in ("SHORT", "RANGE")}
    blocker_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for x in blocked:
        for b in x["blockers"]:
            blocker_rows[str(b)].append(x)

    report = {
        "version": "v40-step12a-counterfactual-1",
        "mode": "PUBLIC_CANDLE_READ_ONLY_RESEARCH",
        "now_ms": now_ms,
        "qualified_total": len(qualified),
        "mature_total": len(mature),
        "pending_total": len(pending),
        "pending": [{
            "market": x["market"],
            "state": x["state"],
            "signal_ms": int(x["cycle"]["signal_ms"]),
            "mature_at_ms": int(x["mature_at_ms"]),
        } for x in pending],
        "labeled_total": len(labeled),
        "errors": errors,
        "frozen_policy": {
            "SHORT_hold_min": 1440,
            "RANGE_hold_min": 720,
            "take_profit_pct": TP_PCT,
            "stop_pct": STOP_PCT,
            "roundtrip_cost_pct": ROUNDTRIP_COST_PCT,
            "same_candle_rule": "STOP_FIRST",
            "l2_thresholds_retuned": False,
        },
        "overall": _summary(labeled),
        "blocked": _summary(blocked),
        "allowed_or_reduced": _summary(allowed),
        "by_state": by_state,
        "by_blocker": {k: _summary(v) for k, v in sorted(blocker_rows.items())},
        "rows": labeled,
        "execution_enabled": False,
        "authenticated_api_used": False,
        "order_actions": 0,
        "active_paper_changed": False,
        "live_orders_possible": False,
    }
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in (
        "version","qualified_total","mature_total","pending_total","labeled_total",
        "overall","blocked","allowed_or_reduced","by_state","by_blocker"
    )}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
