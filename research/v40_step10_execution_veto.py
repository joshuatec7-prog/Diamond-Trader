#!/usr/bin/env python3
"""Step 10: fixed cost/spread/liquidity/L2 execution-veto research.

OFFLINE / READ-ONLY RESEARCH ONLY.

This step does two separate things without changing any trading logic:
1. Re-price the already frozen Step 7 SHORT/RANGE selections under additional
   roundtrip cost haircuts. This asks whether the statistical edge is large
   enough to survive more friction than the original 0.78% cost model.
2. Take one current public Bitvavo EUR order-book snapshot and measure whether
   representative notionals fit inside the SAME frozen friction budget.

Historical L2 books are not available in the frozen 90-day datasets. Therefore
this script never pretends that the current L2 snapshot validates past trades.
The L2 rule produced here is only a candidate execution veto for later
prospective shadow/PAPER validation.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from bitvavo_public import BitvavoPublic
from research.v40_step7_state_overlap import _load_bear_selected, _load_range_selected

BASE_ROUNDTRIP_COST_PCT = 0.78
ROUNDTRIP_FEE_PCT = 0.50
MICROSTRUCTURE_BUDGET_PCT = BASE_ROUNDTRIP_COST_PCT - ROUNDTRIP_FEE_PCT
EXTRA_COST_STRESS_PCT = (0.0, 0.25, 0.50, 1.00, 1.50)
NOTIONALS_EUR = (200.0, 500.0, 1000.0)
DEPTH_BUFFER_MULTIPLE = 10.0
VOLUME_BUFFER_MULTIPLE = 1000.0
TOP_OF_BOOK_SPREAD_MAX_PCT = 0.25
STRATIFIED_DEPTH_SAMPLE = 100
HISTORICAL_ROBUST_EXTRA_COST_PCT = 0.50
HISTORICAL_ROBUST_PF_MIN = 1.20


def _pf(values: pd.Series) -> float | None:
    arr = values.to_numpy(dtype=float)
    pos = arr[arr > 0].sum()
    neg = -arr[arr < 0].sum()
    return float(pos / neg) if neg > 0 else None


def _summary(values: pd.Series) -> dict[str, Any]:
    arr = values.to_numpy(dtype=float)
    if not len(arr):
        return {"n": 0, "mean_pct": None, "pf": None, "win_pct": None}
    return {
        "n": int(len(arr)),
        "mean_pct": float(arr.mean()),
        "pf": _pf(values),
        "win_pct": float((arr > 0).mean() * 100.0),
    }


def _stress_table(df: pd.DataFrame) -> dict[str, Any]:
    rows = []
    for extra in EXTRA_COST_STRESS_PCT:
        stressed = df.outcome.astype(float) - float(extra)
        item = {"extra_roundtrip_cost_pct": float(extra), **_summary(stressed)}
        rows.append(item)
    robust_row = next(row for row in rows if row["extra_roundtrip_cost_pct"] == HISTORICAL_ROBUST_EXTRA_COST_PCT)
    robust = (
        robust_row["mean_pct"] is not None and robust_row["mean_pct"] > 0.0
        and robust_row["pf"] is not None and robust_row["pf"] >= HISTORICAL_ROBUST_PF_MIN
    )
    return {
        "stress": rows,
        "robustness_test": {
            "extra_roundtrip_cost_pct": HISTORICAL_ROBUST_EXTRA_COST_PCT,
            "mean_must_remain_positive": True,
            "pf_min": HISTORICAL_ROBUST_PF_MIN,
            "passed": bool(robust),
        },
    }


def _frozen_arbiter_selected(bear_dataset: str, range_dataset: str, short_result: str, range_result: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    short_frozen = json.loads(Path(short_result).read_text(encoding="utf-8"))
    range_frozen = json.loads(Path(range_result).read_text(encoding="utf-8"))
    short = _load_bear_selected(bear_dataset, short_frozen)
    rang = _load_range_selected(range_dataset, range_frozen)
    short_times = set(short.signal_ms.astype(int).tolist())
    range_only_short = rang[
        (~rang.signal_ms.isin(short_times)) & (rang.state_side == "RANGE_SHORT")
    ].copy()
    return short, range_only_short


def _stratified_markets(tickers: list[dict[str, float | str]], target: int) -> list[str]:
    rows = sorted(tickers, key=lambda row: float(row["volume_quote"]), reverse=True)
    if len(rows) <= target:
        return [str(row["market"]) for row in rows]
    # Always include the 20 most liquid markets, then cover the rest of the
    # volume distribution deterministically instead of sampling randomly.
    top_n = min(20, target)
    chosen = [str(row["market"]) for row in rows[:top_n]]
    remaining_n = target - top_n
    if remaining_n > 0 and len(rows) > top_n:
        idx = np.linspace(top_n, len(rows) - 1, num=remaining_n, dtype=int)
        chosen.extend(str(rows[i]["market"]) for i in np.unique(idx))
    return list(dict.fromkeys(chosen))[:target]


def _depth_snapshot(api: BitvavoPublic, market: str, volume_quote: float) -> dict[str, Any]:
    payload = api._get(f"/{market}/book", {"depth": 100})  # public GET, research only
    if not isinstance(payload, dict):
        raise RuntimeError(f"ongeldig orderboek voor {market}")
    bids = api._depth_levels(payload.get("bids"), reverse=True)
    asks = api._depth_levels(payload.get("asks"), reverse=False)
    bid = float(bids[0][0])
    ask = float(asks[0][0])
    if ask < bid:
        raise RuntimeError(f"gekruist orderboek voor {market}")
    mid = (bid + ask) / 2.0
    top_spread = (ask / bid - 1.0) * 100.0
    near_band = 0.005
    near_bid = sum(price * amount for price, amount in bids if price >= mid * (1.0 - near_band))
    near_ask = sum(price * amount for price, amount in asks if price <= mid * (1.0 + near_band))
    out: dict[str, Any] = {
        "market": market,
        "volume_quote_24h": float(volume_quote),
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "top_spread_pct": float(top_spread),
        "near_bid_depth_quote_0_5pct": float(near_bid),
        "near_ask_depth_quote_0_5pct": float(near_ask),
        "captured_at_ms": int(time.time() * 1000),
        "notionals": {},
    }
    for notional in NOTIONALS_EUR:
        try:
            buy_vwap, ask_depth_total = api._depth_vwap(asks, notional)
            sell_vwap, bid_depth_total = api._depth_vwap(bids, notional)
            execution_spread = (buy_vwap / sell_vwap - 1.0) * 100.0
            pass_top_spread = top_spread <= TOP_OF_BOOK_SPREAD_MAX_PCT
            pass_execution_budget = execution_spread <= MICROSTRUCTURE_BUDGET_PCT
            pass_near_depth = min(near_bid, near_ask) >= DEPTH_BUFFER_MULTIPLE * notional
            pass_volume = volume_quote >= VOLUME_BUFFER_MULTIPLE * notional
            passed = pass_top_spread and pass_execution_budget and pass_near_depth and pass_volume
            out["notionals"][str(int(notional))] = {
                "buy_vwap": float(buy_vwap),
                "sell_vwap": float(sell_vwap),
                "execution_spread_pct": float(execution_spread),
                "bid_depth_total_quote": float(bid_depth_total),
                "ask_depth_total_quote": float(ask_depth_total),
                "pass_top_spread": bool(pass_top_spread),
                "pass_execution_budget": bool(pass_execution_budget),
                "pass_near_depth": bool(pass_near_depth),
                "pass_volume": bool(pass_volume),
                "candidate_veto_pass": bool(passed),
            }
        except Exception as exc:
            out["notionals"][str(int(notional))] = {
                "candidate_veto_pass": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    return out


def _snapshot_summary(rows: list[dict[str, Any]], all_books_count: int, all_tickers_count: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "active_eur_tickers": int(all_tickers_count),
        "active_eur_top_books": int(all_books_count),
        "depth_sample_requested": STRATIFIED_DEPTH_SAMPLE,
        "depth_sample_usable": int(len(rows)),
        "fixed_rules": {
            "top_of_book_spread_max_pct": TOP_OF_BOOK_SPREAD_MAX_PCT,
            "execution_spread_budget_pct": MICROSTRUCTURE_BUDGET_PCT,
            "near_depth_multiple_of_notional_each_side": DEPTH_BUFFER_MULTIPLE,
            "volume_24h_multiple_of_notional": VOLUME_BUFFER_MULTIPLE,
        },
        "by_notional": {},
    }
    for notional in NOTIONALS_EUR:
        key = str(int(notional))
        vals = [row["notionals"].get(key, {}) for row in rows]
        usable = [v for v in vals if "execution_spread_pct" in v]
        passed = [v for v in vals if v.get("candidate_veto_pass") is True]
        execution = [float(v["execution_spread_pct"]) for v in usable]
        result["by_notional"][key] = {
            "usable": int(len(usable)),
            "pass_n": int(len(passed)),
            "pass_pct": float(len(passed) / len(usable) * 100.0) if usable else None,
            "median_execution_spread_pct": float(np.median(execution)) if execution else None,
            "p90_execution_spread_pct": float(np.quantile(execution, 0.90)) if execution else None,
        }
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bear_dataset")
    ap.add_argument("range_dataset")
    ap.add_argument("--short-result", default="research/v40_step5_bear_untouched_once_result.json")
    ap.add_argument("--range-result", default="research/v40_step6_range_dataset_analysis_result.json")
    ap.add_argument("--output", default="v40_step10_execution_veto_result.json")
    args = ap.parse_args()

    short, rang = _frozen_arbiter_selected(args.bear_dataset, args.range_dataset, args.short_result, args.range_result)
    cost_stress = {
        "SHORT": {"n": int(len(short)), **_stress_table(short)},
        "RANGE": {"n": int(len(rang)), **_stress_table(rang)},
    }

    api = BitvavoPublic("https://api.bitvavo.com/v2", timeout_seconds=15, retries=4)
    tickers = api.quote_market_tickers("EUR")
    ticker_map = {str(row["market"]): float(row["volume_quote"]) for row in tickers}
    books = api.market_books(list(ticker_map))
    markets = _stratified_markets(tickers, STRATIFIED_DEPTH_SAMPLE)

    depth_rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for i, market in enumerate(markets, 1):
        try:
            depth_rows.append(_depth_snapshot(api, market, ticker_map[market]))
        except Exception as exc:
            errors.append(f"{market}: {type(exc).__name__}: {exc}")
        if i % 20 == 0:
            print(f"L2 progress {i}/{len(markets)} usable={len(depth_rows)} errors={len(errors)}", flush=True)
        time.sleep(0.03)

    snapshot = _snapshot_summary(depth_rows, len(books), len(tickers))
    snapshot["errors"] = int(len(errors))
    snapshot["error_examples"] = errors[:20]

    historical_both_robust = all(cost_stress[state]["robustness_test"]["passed"] for state in ("SHORT", "RANGE"))
    l2_500 = snapshot["by_notional"]["500"]
    prospective_candidate = (
        historical_both_robust
        and l2_500["usable"] is not None and int(l2_500["usable"]) >= 80
        and l2_500["pass_pct"] is not None and float(l2_500["pass_pct"]) >= 50.0
    )

    result = {
        "version": "v40-step10-execution-veto-1",
        "mode": "OFFLINE_READ_ONLY_RESEARCH",
        "execution_enabled": False,
        "live_orders_possible": False,
        "active_paper_changed": False,
        "render_changed": False,
        "historical_l2_available": False,
        "warning": "Current L2 snapshot is point-in-time execution evidence only and is not attached retrospectively to historical signals.",
        "frozen_cost_model": {
            "original_roundtrip_cost_pct": BASE_ROUNDTRIP_COST_PCT,
            "roundtrip_fee_pct": ROUNDTRIP_FEE_PCT,
            "remaining_microstructure_budget_pct": MICROSTRUCTURE_BUDGET_PCT,
        },
        "historical_cost_stress": cost_stress,
        "current_bitvavo_eur_l2_snapshot": snapshot,
        "candidate_execution_veto": {
            "status": "PROSPECTIVE_SHADOW_CANDIDATE_ONLY" if prospective_candidate else "INSUFFICIENT_FOR_PROSPECTIVE_VETO_PROMOTION",
            "rules": {
                "top_spread_pct_max": TOP_OF_BOOK_SPREAD_MAX_PCT,
                "execution_spread_pct_max": MICROSTRUCTURE_BUDGET_PCT,
                "near_bid_and_ask_depth_min_multiple": DEPTH_BUFFER_MULTIPLE,
                "volume_24h_min_multiple": VOLUME_BUFFER_MULTIPLE,
            },
            "requires_future_repeated_l2_snapshots": True,
            "runtime_or_paper_change_allowed": False,
        },
        "short_execution_route": {
            "assessed_here": False,
            "status": "REQUIRES_SEPARATE_EXECUTION_VENUE_VALIDATION",
            "note": "This step measures public Bitvavo spot books only; it does not claim a borrow/derivatives short route exists.",
        },
        "decision": "KEEP_FIXED_EXECUTION_VETO_FOR_PROSPECTIVE_SHADOW_ONLY" if prospective_candidate else "DO_NOT_PROMOTE_EXECUTION_VETO_YET",
    }
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({
        "decision": result["decision"],
        "short_cost_robust": cost_stress["SHORT"]["robustness_test"]["passed"],
        "range_cost_robust": cost_stress["RANGE"]["robustness_test"]["passed"],
        "l2_depth_sample_usable": snapshot["depth_sample_usable"],
        "eur500_pass_pct": snapshot["by_notional"]["500"]["pass_pct"],
        "eur500_median_execution_spread_pct": snapshot["by_notional"]["500"]["median_execution_spread_pct"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
