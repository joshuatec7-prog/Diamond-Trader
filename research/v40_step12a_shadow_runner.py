#!/usr/bin/env python3
"""Step 12A prospective human-like shadow runner.

OBSERVE ONLY. PUBLIC BITVAVO DATA ONLY. NO AUTHENTICATED API. NO ORDERS.

Every closed 5m bar:
1. scan all active EUR markets with the exact frozen Step 5/6 feature logic;
2. score the frozen SHORT and RANGE selectors;
3. use the frozen Step 7 arbiter: SHORT > RANGE_SHORT > NO_TRADE;
4. if a candidate exists, focus for 3 L2 snapshots before deciding;
5. write ALLOW / REDUCE / BLOCK / NO_TRADE with uncertainty reasons;
6. simulate TP10/STOP15 only, using EUR300 baseline or EUR200 reduced shadow size;
7. keep at most four simultaneous shadow positions.

This is prospective measurement. It cannot place PAPER or live orders.
"""
from __future__ import annotations

import argparse
import json
import math
import pickle
import time
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any

import numpy as np
import pandas as pd

from bitvavo_public import BitvavoPublic
from research.v40_bear_research_dataset import (
    MAX_CANDIDATES_PER_MOMENT as BEAR_MAX,
    attach_cross_section as bear_attach_cross_section,
    btc_context_map,
    point_in_time_features as bear_features,
)
from research.v40_range_research_dataset import (
    MAX_CANDIDATES_PER_MOMENT as RANGE_MAX,
    MIN_EDGE_STRETCH,
    MIN_RANGE_WIDTH_4H_PCT,
    attach_cross_section as range_attach_cross_section,
    point_in_time_features as range_features,
)
from v40_master_research_dataset import (
    FIVE_MINUTE_MS,
    accumulate_early_market_context,
    finalize_early_market_context,
)

API_URL = "https://api.bitvavo.com/v2"
BASELINE_STAKE_EUR = 300.0
REDUCED_STAKE_EUR = 200.0
MAX_OPEN = 4
TAKE_PROFIT_PCT = 10.0
STOP_PCT = 15.0
ROUNDTRIP_COST_PCT = 0.78

# Frozen Step 10 prospective L2 candidate-veto rules.
TOP_SPREAD_MAX_PCT = 0.25
EXECUTION_SPREAD_MAX_PCT = 0.28
NEAR_DEPTH_MULTIPLE = 10.0
VOLUME_MULTIPLE = 1000.0
L2_SAMPLES = 3
L2_PASS_FOR_REDUCE = 2
L2_SPREAD_RANGE_MAX_PCT = 0.10

# Prospective meta-decider rules are frozen before seeing shadow outcomes.
MODEL_MARGIN_ALLOW_RATIO = 1.20
REGIME_CONFIRMATIONS = 2

SHORT_MAX_HOLD_MIN = 1440
RANGE_MAX_HOLD_MIN = 720


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return v if math.isfinite(v) else default


def _initial_state() -> dict[str, Any]:
    return {
        "version": "v40-step12a-shadow-state-1",
        "last_signal_ms": 0,
        "stable_regime": "",
        "pending_regime": "",
        "pending_count": 0,
        "positions": [],
        "closed_positions": [],
        "cycles": 0,
        "qualified_candidates": 0,
        "meta_counts": {},
        "execution_enabled": False,
        "live_orders_possible": False,
        "active_paper_changed": False,
    }


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _initial_state()
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("execution_enabled") is not False:
        raise RuntimeError("shadow state execution guard invalid")
    return data


def _save_state(path: Path, state: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        f.flush()


def _flatten_bear(candidate: dict[str, Any]) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "market": candidate["market"],
        "signal_ms": int(candidate["signal_ms"]),
        "relative_weakness_vs_btc_1h_pct": candidate["relative_weakness_vs_btc_1h_pct"],
    }
    for k, v in candidate["entry_features"].items():
        rec["ef_" + k] = v
    for k, v in candidate["market_context"].items():
        rec["mc_" + k] = v
    for k, v in candidate["cross_section"].items():
        rec["cs_" + k] = v
    return rec


def _flatten_range(candidate: dict[str, Any]) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "market": candidate["market"],
        "signal_ms": int(candidate["signal_ms"]),
        "side_num": 1.0 if candidate["side"] == "RANGE_LONG" else -1.0,
    }
    for k, v in candidate["entry_features"].items():
        if k != "side":
            rec["ef_" + k] = v
    for k, v in candidate["market_context"].items():
        rec["mc_" + k] = v
    for k, v in candidate["cross_section"].items():
        rec["cs_" + k] = v
    return rec


def _predict_one(section: dict[str, Any], row: dict[str, Any]) -> float:
    features = list(section["features"])
    medians = dict(section["medians"])
    values = {
        f: (_finite(row.get(f), float(medians.get(f, 0.0))) if row.get(f) is not None else float(medians.get(f, 0.0)))
        for f in features
    }
    frame = pd.DataFrame([values], columns=features).replace([np.inf, -np.inf], np.nan)
    frame = frame.fillna(pd.Series(medians))
    return float(section["model"].predict(frame)[0])


def _raw_regime(context: dict[str, Any]) -> str:
    markets = int(context.get("markets_used", 0) or 0)
    breadth = _finite(context.get("breadth_positive_1h_pct"))
    btc = _finite(context.get("btc_return_1h_pct"))
    if markets < 20:
        return "DATA_UNCERTAIN"
    if btc <= -2.0 or breadth < 35.0:
        return "BEAR"
    if btc > 0.0 and breadth >= 60.0:
        return "BULL"
    return "SIDEWAYS"


def _resolve_regime(state: dict[str, Any], raw: str) -> dict[str, Any]:
    previous = str(state.get("stable_regime", "")).upper()
    pending = str(state.get("pending_regime", "")).upper()
    pending_count = int(state.get("pending_count", 0) or 0)
    stable_values = {"BULL", "SIDEWAYS", "BEAR", "DATA_UNCERTAIN"}
    if previous not in stable_values:
        stable, visible, next_pending, next_count = raw, raw, "", 0
    elif raw == previous:
        stable, visible, next_pending, next_count = previous, previous, "", 0
    else:
        count = pending_count + 1 if pending == raw else 1
        if count >= REGIME_CONFIRMATIONS:
            stable, visible, next_pending, next_count = raw, raw, "", 0
        else:
            stable, visible, next_pending, next_count = previous, "TRANSITION", raw, count
    state["stable_regime"] = stable
    state["pending_regime"] = next_pending
    state["pending_count"] = next_count
    return {"raw": raw, "visible": visible, "stable": stable}


def _l2_rule(snapshot: dict[str, Any], volume_quote: float, stake: float) -> dict[str, Any]:
    checks = {
        "top_spread": _finite(snapshot.get("spread_pct"), 999.0) <= TOP_SPREAD_MAX_PCT,
        "execution_spread": _finite(snapshot.get("execution_spread_pct"), 999.0) <= EXECUTION_SPREAD_MAX_PCT,
        "near_depth": min(
            _finite(snapshot.get("near_bid_depth_quote")),
            _finite(snapshot.get("near_ask_depth_quote")),
        ) >= NEAR_DEPTH_MULTIPLE * stake,
        "volume": volume_quote >= VOLUME_MULTIPLE * stake,
    }
    return {"passed": all(checks.values()), "checks": checks}


def _focus_l2(
    api: BitvavoPublic,
    market: str,
    volume_quote: float,
    *,
    stake: float,
    delay_seconds: float,
) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    errors: list[str] = []
    for i in range(L2_SAMPLES):
        try:
            snap = api.depth_book(market, stake, depth=100)
            rule = _l2_rule(snap, volume_quote, stake)
            samples.append({**snap, "candidate_veto": rule})
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        if i + 1 < L2_SAMPLES and delay_seconds > 0:
            time.sleep(delay_seconds)
    passed = sum(1 for s in samples if s["candidate_veto"]["passed"])
    spreads = [_finite(s.get("spread_pct")) for s in samples]
    stable = bool(spreads) and max(spreads) - min(spreads) <= L2_SPREAD_RANGE_MAX_PCT
    return {
        "samples": samples,
        "errors": errors,
        "pass_count": passed,
        "sample_count": len(samples),
        "spread_range_pct": (max(spreads) - min(spreads)) if spreads else None,
        "stable": stable,
    }


def _meta_decision(
    *,
    state_name: str,
    prediction: float,
    threshold: float,
    regime: str,
    l2: dict[str, Any],
    market: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    blockers: list[str] = []
    uncertainty: list[str] = []

    open_positions = list(state.get("positions", []))
    if any(str(p.get("market")) == market for p in open_positions):
        blockers.append("market_al_open_in_shadow")
    if len(open_positions) >= MAX_OPEN:
        blockers.append("shadow_capaciteit_max4")

    if l2["sample_count"] < L2_PASS_FOR_REDUCE:
        blockers.append("onvoldoende_l2_metingen")
    elif l2["pass_count"] < L2_PASS_FOR_REDUCE:
        blockers.append("l2_fixed_veto_faalt")
    if l2["sample_count"] and not l2["stable"]:
        blockers.append("l2_spread_instabiel")

    margin_ratio = prediction / threshold if threshold > 0 else 0.0
    if margin_ratio < MODEL_MARGIN_ALLOW_RATIO:
        uncertainty.append("modelscore_minder_dan_20pct_boven_drempel")
    if state_name == "SHORT" and regime in {"BULL", "TRANSITION"}:
        uncertainty.append("short_tegen_brede_marktcontext")
    if state_name == "RANGE" and regime != "SIDEWAYS":
        uncertainty.append("range_buiten_stabiel_sideways_regime")
    if l2["pass_count"] == L2_PASS_FOR_REDUCE:
        uncertainty.append("l2_slechts_twee_van_drie_pass")

    if blockers:
        action = "BLOCK"
        stake = 0.0
    elif uncertainty:
        action = "REDUCE"
        stake = REDUCED_STAKE_EUR
    else:
        action = "ALLOW"
        stake = BASELINE_STAKE_EUR

    return {
        "action": action,
        "shadow_stake_eur": stake,
        "blockers": blockers,
        "uncertainty": uncertainty,
        "model_margin_ratio": margin_ratio,
        "regime": regime,
        "execution_enabled": False,
    }


def _update_positions(
    state: dict[str, Any],
    candles_by_market: dict[str, list[Any]],
    signal_ms: int,
) -> list[dict[str, Any]]:
    remaining: list[dict[str, Any]] = []
    exits: list[dict[str, Any]] = []
    for pos in list(state.get("positions", [])):
        market = str(pos["market"])
        rows = candles_by_market.get(market, [])
        exact = next((c for c in rows if int(c.timestamp_ms) == signal_ms), None)
        latest = rows[-1] if rows else None
        held_min = max(0, int((signal_ms - int(pos["opened_signal_ms"])) // 60_000))
        exit_price: float | None = None
        reason: str | None = None

        # Conservative: STOP before TP when both occur in the same 5m candle.
        if exact is not None and float(exact.high) >= float(pos["stop_price"]):
            exit_price = float(pos["stop_price"])
            reason = "STOP15"
        elif exact is not None and float(exact.low) <= float(pos["take_price"]):
            exit_price = float(pos["take_price"])
            reason = "TP10"
        elif held_min >= int(pos["max_hold_minutes"]) and latest is not None:
            exit_price = float(latest.close)
            reason = "TIME"

        if exit_price is None:
            remaining.append(pos)
            continue

        entry = float(pos["entry_price"])
        gross = (entry - exit_price) / entry * 100.0
        net = gross - ROUNDTRIP_COST_PCT
        stake = float(pos["stake_eur"])
        closed = {
            **pos,
            "closed_signal_ms": signal_ms,
            "exit_price": exit_price,
            "exit_reason": reason,
            "gross_pct": gross,
            "net_pct": net,
            "pnl_eur": stake * net / 100.0,
            "held_minutes": held_min,
        }
        exits.append(closed)
        state.setdefault("closed_positions", []).append(closed)
    state["positions"] = remaining
    return exits


def _open_position(
    state: dict[str, Any],
    *,
    selection: dict[str, Any],
    meta: dict[str, Any],
    l2: dict[str, Any],
    signal_ms: int,
) -> dict[str, Any] | None:
    if meta["action"] not in {"ALLOW", "REDUCE"}:
        return None
    sell_prices = [
        _finite(s.get("sell_vwap"))
        for s in l2.get("samples", [])
        if _finite(s.get("sell_vwap")) > 0
    ]
    if not sell_prices:
        return None
    entry = float(median(sell_prices))
    state_name = str(selection["state"])
    pos = {
        "market": str(selection["market"]),
        "state": state_name,
        "opened_signal_ms": int(signal_ms),
        "entry_price": entry,
        "take_price": entry * (1.0 - TAKE_PROFIT_PCT / 100.0),
        "stop_price": entry * (1.0 + STOP_PCT / 100.0),
        "stake_eur": float(meta["shadow_stake_eur"]),
        "max_hold_minutes": SHORT_MAX_HOLD_MIN if state_name == "SHORT" else RANGE_MAX_HOLD_MIN,
        "prediction": float(selection["prediction"]),
        "threshold": float(selection["threshold"]),
        "meta_action": str(meta["action"]),
        "execution_route_confirmed": False,
        "synthetic_short_only": True,
    }
    state.setdefault("positions", []).append(pos)
    return pos


def _current_signal_ms(now_ms: int) -> int:
    return (now_ms // FIVE_MINUTE_MS) * FIVE_MINUTE_MS - FIVE_MINUTE_MS


def scan_once(
    api: BitvavoPublic,
    bundle: dict[str, Any],
    state: dict[str, Any],
    *,
    now_ms: int,
    focus_delay_seconds: float,
) -> dict[str, Any]:
    signal_ms = _current_signal_ms(now_ms)
    tickers = api.quote_market_tickers("EUR")
    volume_map = {str(x["market"]): float(x["volume_quote"]) for x in tickers}
    markets = sorted(volume_map)
    if "BTC-EUR" not in markets:
        markets.append("BTC-EUR")
        markets.sort()

    candles_by_market: dict[str, list[Any]] = {}
    context_aggregates: dict[int, dict[str, float]] = {}
    bear_candidates: list[dict[str, Any]] = []
    range_candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    exact_markets = 0

    for market in markets:
        try:
            rows = api.closed_candles(market, "5m", 90, now_ms=now_ms)
            rows = sorted((c for c in rows if c.is_valid), key=lambda c: c.timestamp_ms)
            candles_by_market[market] = rows
            index_map = {int(c.timestamp_ms): i for i, c in enumerate(rows)}
            idx = index_map.get(signal_ms)
            if idx is None or idx < 48:
                continue
            exact_markets += 1
            accumulate_early_market_context(
                rows, signal_ms, signal_ms + FIVE_MINUTE_MS, context_aggregates
            )

            bf = bear_features(rows, idx)
            if bf is not None and _finite(bf.get("weakness_score")) >= 1.0:
                bear_candidates.append({
                    "market": market,
                    "signal_ms": signal_ms,
                    "entry_reference": float(rows[idx].close),
                    "entry_features": bf,
                    "relative_weakness_vs_btc_1h_pct": None,
                    "market_context": {},
                    "cross_section": {},
                })

            rf = range_features(rows, idx)
            if (
                rf is not None
                and _finite(rf.get("range_width_4h_pct")) >= MIN_RANGE_WIDTH_4H_PCT
                and _finite(rf.get("stretch_score")) >= MIN_EDGE_STRETCH
            ):
                range_candidates.append({
                    "market": market,
                    "signal_ms": signal_ms,
                    "entry_reference": float(rows[idx].close),
                    "side": str(rf["side"]),
                    "entry_features": rf,
                    "market_context": {},
                    "cross_section": {},
                })
        except Exception as exc:
            errors.append(f"{market}: {type(exc).__name__}: {exc}")

    exits = _update_positions(state, candles_by_market, signal_ms)

    context_all = finalize_early_market_context(context_aggregates)
    context = dict(context_all.get(signal_ms, {}))
    btc_rows = candles_by_market.get("BTC-EUR", [])
    btc_map = btc_context_map(btc_rows, signal_ms, signal_ms + FIVE_MINUTE_MS)
    btc = dict(btc_map.get(signal_ms, {}))
    if context and btc:
        context.update(btc)

    if not context or not btc:
        regime = _resolve_regime(state, "DATA_UNCERTAIN")
        selection = None
        meta = {
            "action": "NO_TRADE",
            "shadow_stake_eur": 0.0,
            "blockers": ["marktcontext_onvolledig"],
            "uncertainty": [],
            "execution_enabled": False,
        }
        l2 = {"samples": [], "errors": [], "pass_count": 0, "sample_count": 0, "stable": False}
    else:
        btc1h = _finite(btc["btc_return_1h_pct"])

        bear_candidates.sort(
            key=lambda x: (_finite(x["entry_features"]["weakness_score"]), str(x["market"])),
            reverse=True,
        )
        bear_candidates = bear_candidates[:BEAR_MAX]
        for c in bear_candidates:
            c["relative_weakness_vs_btc_1h_pct"] = btc1h - _finite(c["entry_features"]["return_1h_pct"])
            c["market_context"] = dict(context)
        bear_attach_cross_section(bear_candidates)

        range_candidates.sort(
            key=lambda x: (_finite(x["entry_features"]["stretch_score"]), str(x["market"])),
            reverse=True,
        )
        range_candidates = range_candidates[:RANGE_MAX]
        for c in range_candidates:
            c["market_context"] = dict(context)
        range_attach_cross_section(range_candidates)

        short_scored: list[dict[str, Any]] = []
        for c in bear_candidates:
            p = _predict_one(bundle["SHORT"], _flatten_bear(c))
            short_scored.append({**c, "prediction": p})
        range_scored: list[dict[str, Any]] = []
        for c in range_candidates:
            p = _predict_one(bundle["RANGE"], _flatten_range(c))
            range_scored.append({**c, "prediction": p})

        short_best = max(short_scored, key=lambda x: x["prediction"]) if short_scored else None
        range_best = max(range_scored, key=lambda x: x["prediction"]) if range_scored else None
        short_threshold = float(bundle["SHORT"]["threshold"])
        range_threshold = float(bundle["RANGE"]["threshold"])

        selection = None
        if short_best is not None and float(short_best["prediction"]) >= short_threshold:
            selection = {
                "state": "SHORT",
                "market": short_best["market"],
                "prediction": float(short_best["prediction"]),
                "threshold": short_threshold,
                "entry_reference": short_best["entry_reference"],
                "features": short_best["entry_features"],
            }
        elif (
            range_best is not None
            and float(range_best["prediction"]) >= range_threshold
            and str(range_best["side"]) == "RANGE_SHORT"
        ):
            selection = {
                "state": "RANGE",
                "market": range_best["market"],
                "prediction": float(range_best["prediction"]),
                "threshold": range_threshold,
                "entry_reference": range_best["entry_reference"],
                "features": range_best["entry_features"],
            }

        raw = _raw_regime(context)
        regime = _resolve_regime(state, raw)

        if selection is None:
            meta = {
                "action": "NO_TRADE",
                "shadow_stake_eur": 0.0,
                "blockers": [],
                "uncertainty": [],
                "execution_enabled": False,
            }
            l2 = {"samples": [], "errors": [], "pass_count": 0, "sample_count": 0, "stable": False}
        else:
            state["qualified_candidates"] = int(state.get("qualified_candidates", 0)) + 1
            market = str(selection["market"])
            l2 = _focus_l2(
                api,
                market,
                float(volume_map.get(market, 0.0)),
                stake=BASELINE_STAKE_EUR,
                delay_seconds=focus_delay_seconds,
            )
            meta = _meta_decision(
                state_name=str(selection["state"]),
                prediction=float(selection["prediction"]),
                threshold=float(selection["threshold"]),
                regime=str(regime["visible"]),
                l2=l2,
                market=market,
                state=state,
            )

    opened = None
    if selection is not None:
        opened = _open_position(
            state,
            selection=selection,
            meta=meta,
            l2=l2,
            signal_ms=signal_ms,
        )

    counts = state.setdefault("meta_counts", {})
    counts[meta["action"]] = int(counts.get(meta["action"], 0)) + 1
    state["cycles"] = int(state.get("cycles", 0)) + 1
    state["last_signal_ms"] = signal_ms

    return {
        "version": "v40-step12a-shadow-cycle-1",
        "captured_at_ms": int(time.time() * 1000),
        "signal_ms": signal_ms,
        "markets_requested": len(markets),
        "markets_exact_signal": exact_markets,
        "market_errors": len(errors),
        "error_sample": errors[:10],
        "context": context,
        "regime": regime,
        "bear_attention_n": len(bear_candidates),
        "range_attention_n": len(range_candidates),
        "selection": selection,
        "meta": meta,
        "l2": l2,
        "opened": opened,
        "exits": exits,
        "open_positions": list(state.get("positions", [])),
        "closed_positions_total": len(state.get("closed_positions", [])),
        "execution_enabled": False,
        "live_orders_possible": False,
        "active_paper_changed": False,
    }


def _sleep_to_next_boundary(extra_seconds: float = 8.0) -> None:
    now = time.time()
    next_boundary = (math.floor(now / 300.0) + 1) * 300.0 + extra_seconds
    time.sleep(max(1.0, next_boundary - now))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bundle")
    ap.add_argument("--state", default="v40_step12a_shadow_state.json")
    ap.add_argument("--jsonl", default="v40_step12a_shadow_cycles.jsonl")
    ap.add_argument("--duration-seconds", type=int, default=0)
    ap.add_argument("--focus-delay-seconds", type=float, default=20.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    raw = Path(args.bundle).read_bytes()
    bundle = pickle.loads(raw)
    if bundle.get("execution_enabled") is not False:
        raise RuntimeError("selector bundle execution guard invalid")
    if bundle["arbiter"]["LONG"] != "DISABLED":
        raise RuntimeError("LONG must remain disabled")

    state_path = Path(args.state)
    jsonl_path = Path(args.jsonl)
    state = _load_state(state_path)
    api = BitvavoPublic(API_URL, timeout_seconds=12, retries=3)

    deadline = time.time() + max(0, args.duration_seconds)
    ran = 0
    while True:
        now_ms = int(time.time() * 1000)
        signal_ms = _current_signal_ms(now_ms)
        if signal_ms > int(state.get("last_signal_ms", 0)):
            record = scan_once(
                api, bundle, state, now_ms=now_ms,
                focus_delay_seconds=max(0.0, args.focus_delay_seconds),
            )
            _append_jsonl(jsonl_path, record)
            _save_state(state_path, state)
            ran += 1
            sel = record.get("selection") or {}
            print(json.dumps({
                "signal_ms": record["signal_ms"],
                "regime": record["regime"]["visible"],
                "state": sel.get("state", "NO_TRADE"),
                "market": sel.get("market"),
                "meta": record["meta"]["action"],
                "open": len(record["open_positions"]),
                "closed_total": record["closed_positions_total"],
                "market_errors": record["market_errors"],
            }), flush=True)

        if args.once:
            break
        if args.duration_seconds <= 0:
            break
        if time.time() >= deadline:
            break
        _sleep_to_next_boundary()

    summary = {
        "version": "v40-step12a-shadow-block-summary-1",
        "cycles_this_process": ran,
        "cycles_total": int(state.get("cycles", 0)),
        "qualified_candidates_total": int(state.get("qualified_candidates", 0)),
        "meta_counts": state.get("meta_counts", {}),
        "open_positions": len(state.get("positions", [])),
        "closed_positions": len(state.get("closed_positions", [])),
        "closed_pnl_eur": sum(_finite(x.get("pnl_eur")) for x in state.get("closed_positions", [])),
        "execution_enabled": False,
        "live_orders_possible": False,
        "active_paper_changed": False,
    }
    Path("v40_step12a_shadow_block_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
