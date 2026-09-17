#!/usr/bin/env python3
"""Step 8: test 1m/3m as an EARLY ATTENTION layer for frozen Step 7 states.

OFFLINE RESEARCH ONLY. This is not a trade rule and does not change PAPER/live.
The diagnostic asks a narrow question: among opportunities that the frozen
5m-based Step 7 arbiter eventually selected, does closed 1m information by
minute +3 of the current 5m candle (two minutes before the 5m close) reliably
prioritize the stronger outcomes?

No threshold sweep. One fixed confirmation rule:
    SHORT/RANGE-short attention confirm = 3m price move < 0 AND last 1m move < 0

Step 5/6 final windows were already opened in their own research steps. This
therefore is a diagnostic precursor study, not a new untouched test.
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

MINUTE_MS = 60_000
SAMPLE_PER_STATE = 150
PASS = {
    "usable_n_min": 240,
    "confirm_n_min": 80,
    "mean_uplift_pp_min": 0.50,
    "both_halves_positive_uplift": True,
    "confirm_pf_must_exceed_nonconfirm": True,
}


def pct(new: float, old: float) -> float:
    return (new / old - 1.0) * 100.0 if new > 0 and old > 0 else 0.0


def pf(values: pd.Series) -> float | None:
    arr = values.to_numpy(dtype=float)
    pos = arr[arr > 0].sum()
    neg = -arr[arr < 0].sum()
    return float(pos / neg) if neg > 0 else None


def summary(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty:
        return {"n": 0, "mean_pct": None, "pf": None, "win_pct": None}
    return {
        "n": int(len(df)),
        "mean_pct": float(df.outcome.mean()),
        "pf": pf(df.outcome),
        "win_pct": float((df.outcome > 0).mean() * 100.0),
    }


def even_sample(df: pd.DataFrame, n: int) -> pd.DataFrame:
    ordered = df.sort_values(["signal_ms", "market"]).reset_index(drop=True)
    if len(ordered) <= n:
        return ordered.copy()
    idx = np.linspace(0, len(ordered) - 1, num=n, dtype=int)
    idx = np.unique(idx)
    return ordered.iloc[idx].copy().reset_index(drop=True)


def frozen_arbiter_events(bear_dataset: str, range_dataset: str, short_result: str, range_result: str) -> pd.DataFrame:
    short_frozen = json.loads(Path(short_result).read_text(encoding="utf-8"))
    range_frozen = json.loads(Path(range_result).read_text(encoding="utf-8"))
    short = _load_bear_selected(bear_dataset, short_frozen)
    rang = _load_range_selected(range_dataset, range_frozen)

    short_times = set(short.signal_ms.astype(int).tolist())
    short_state = short[["signal_ms", "market", "outcome"]].copy()
    short_state["state"] = "SHORT"

    range_state = rang[
        (~rang.signal_ms.isin(short_times)) & (rang.state_side == "RANGE_SHORT")
    ][["signal_ms", "market", "outcome"]].copy()
    range_state["state"] = "RANGE"

    return pd.concat([short_state, range_state], ignore_index=True)


def fast_features(api: BitvavoPublic, market: str, signal_ms: int) -> dict[str, float]:
    # The 5m candle begins at signal_ms and closes at signal_ms+5m.
    # We stop at +3m, so the attention layer is two minutes earlier.
    cutoff = int(signal_ms) + 3 * MINUTE_MS
    start = int(signal_ms) - 5 * MINUTE_MS
    candles = api.closed_candles_between(
        market, "1m", start, cutoff,
        now_ms=cutoff, page_limit=30, max_pages=2,
    )
    by_ts = {int(c.timestamp_ms): c for c in candles if c.is_valid}
    baseline = by_ts.get(int(signal_ms) - MINUTE_MS)  # closes exactly at 5m candle start
    previous = by_ts.get(cutoff - 2 * MINUTE_MS)      # closes at cutoff-1m
    latest = by_ts.get(cutoff - MINUTE_MS)            # closes exactly at cutoff
    if baseline is None or previous is None or latest is None:
        raise RuntimeError("onvolledige exact-timed 1m candles rond aandachtspunt")

    r1 = pct(float(latest.close), float(previous.close))
    r3 = pct(float(latest.close), float(baseline.close))

    prior = [by_ts.get(int(signal_ms) - n * MINUTE_MS) for n in (4, 3, 2)]
    first3 = [by_ts.get(int(signal_ms) + n * MINUTE_MS) for n in (0, 1, 2)]
    volume_ratio = None
    if all(x is not None for x in prior + first3):
        pv = sum(float(x.volume) for x in prior if x is not None)
        fv = sum(float(x.volume) for x in first3 if x is not None)
        if pv > 0:
            volume_ratio = fv / pv

    return {
        "return_1m_pct": float(r1),
        "return_3m_pct": float(r3),
        "short_pressure_1m_pct": float(-r1),
        "short_pressure_3m_pct": float(-r3),
        "volume_ratio_first3_vs_prior3": float(volume_ratio) if volume_ratio is not None else math.nan,
    }


def half_uplift(df: pd.DataFrame, first: bool) -> dict[str, Any]:
    ordered = df.sort_values("signal_ms")
    split = len(ordered) // 2
    part = ordered.iloc[:split] if first else ordered.iloc[split:]
    yes = part[part.fast_confirm]
    no = part[~part.fast_confirm]
    uplift = None if yes.empty or no.empty else float(yes.outcome.mean() - no.outcome.mean())
    return {"confirm": summary(yes), "nonconfirm": summary(no), "uplift_pp": uplift}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bear_dataset")
    ap.add_argument("range_dataset")
    ap.add_argument("--short-result", default="research/v40_step5_bear_untouched_once_result.json")
    ap.add_argument("--range-result", default="research/v40_step6_range_dataset_analysis_result.json")
    ap.add_argument("--output", default="v40_step8_fast_attention_result.json")
    args = ap.parse_args()

    events = frozen_arbiter_events(args.bear_dataset, args.range_dataset, args.short_result, args.range_result)
    sampled_parts = []
    for state in ("SHORT", "RANGE"):
        sampled_parts.append(even_sample(events[events.state == state], SAMPLE_PER_STATE))
    sampled = pd.concat(sampled_parts, ignore_index=True).sort_values(["signal_ms", "state", "market"]).reset_index(drop=True)

    api = BitvavoPublic("https://api.bitvavo.com/v2", timeout_seconds=15, retries=4)
    measured: list[dict[str, Any]] = []
    errors: list[str] = []
    for i, row in sampled.iterrows():
        try:
            ff = fast_features(api, str(row.market), int(row.signal_ms))
            measured.append({
                "signal_ms": int(row.signal_ms),
                "market": str(row.market),
                "state": str(row.state),
                "outcome": float(row.outcome),
                **ff,
            })
        except Exception as exc:
            errors.append(f"{row.state} {row.market} {int(row.signal_ms)}: {type(exc).__name__}: {exc}")
        if (i + 1) % 50 == 0:
            print(f"1m attention progress {i + 1}/{len(sampled)} usable={len(measured)} errors={len(errors)}", flush=True)
        time.sleep(0.02)

    df = pd.DataFrame(measured)
    if df.empty:
        raise RuntimeError("geen bruikbare 1m-metingen")
    df["fast_confirm"] = (df.return_3m_pct < 0.0) & (df.return_1m_pct < 0.0)

    confirmed = df[df.fast_confirm].copy()
    nonconfirmed = df[~df.fast_confirm].copy()
    uplift = float(confirmed.outcome.mean() - nonconfirmed.outcome.mean()) if len(confirmed) and len(nonconfirmed) else None
    first_half = half_uplift(df, True)
    second_half = half_uplift(df, False)
    confirm_pf = pf(confirmed.outcome) if len(confirmed) else None
    nonconfirm_pf = pf(nonconfirmed.outcome) if len(nonconfirmed) else None

    passed = (
        len(df) >= PASS["usable_n_min"]
        and len(confirmed) >= PASS["confirm_n_min"]
        and uplift is not None and uplift >= PASS["mean_uplift_pp_min"]
        and confirm_pf is not None and nonconfirm_pf is not None and confirm_pf > nonconfirm_pf
        and first_half["uplift_pp"] is not None and first_half["uplift_pp"] > 0
        and second_half["uplift_pp"] is not None and second_half["uplift_pp"] > 0
    )

    by_state = {}
    for state in ("SHORT", "RANGE"):
        sub = df[df.state == state]
        yes = sub[sub.fast_confirm]
        no = sub[~sub.fast_confirm]
        by_state[state] = {
            "all": summary(sub),
            "confirm": summary(yes),
            "nonconfirm": summary(no),
            "uplift_pp": float(yes.outcome.mean() - no.outcome.mean()) if len(yes) and len(no) else None,
        }

    corr1 = float(df[["short_pressure_1m_pct", "outcome"]].corr().iloc[0, 1]) if len(df) > 2 else None
    corr3 = float(df[["short_pressure_3m_pct", "outcome"]].corr().iloc[0, 1]) if len(df) > 2 else None

    result = {
        "version": "v40-step8-fast-attention-1",
        "mode": "OFFLINE_MEASUREMENT_ONLY",
        "execution_enabled": False,
        "live_orders_possible": False,
        "active_paper_changed": False,
        "new_untouched_test": False,
        "source_states": "Frozen Step 7 SHORT priority, then RANGE_SHORT as RANGE; LONG and RANGE_LONG excluded.",
        "purpose": "Conditional precursor diagnostic only; tests whether closed 1m data can prioritize attention 2 minutes before current 5m signal close.",
        "fixed_rule": "FAST_CONFIRM iff first 3m move of current 5m candle is down AND last 1m move at +3m is down.",
        "attention_lead_minutes": 2,
        "sample": {
            "target_per_state": SAMPLE_PER_STATE,
            "requested": int(len(sampled)),
            "usable": int(len(df)),
            "errors": int(len(errors)),
            "error_examples": errors[:20],
            "method": "deterministic evenly-spaced sample across frozen evaluation events",
        },
        "pass_criteria": PASS,
        "overall": {
            "all": summary(df),
            "confirm": summary(confirmed),
            "nonconfirm": summary(nonconfirmed),
            "confirm_share_pct": float(len(confirmed) / len(df) * 100.0),
            "uplift_pp": uplift,
            "corr_short_pressure_1m_vs_outcome": corr1,
            "corr_short_pressure_3m_vs_outcome": corr3,
            "first_half": first_half,
            "second_half": second_half,
        },
        "by_state": by_state,
        "passed": bool(passed),
        "decision": "KEEP_1M_3M_AS_FAST_ATTENTION_LAYER" if passed else "REJECT_1M_3M_FAST_ATTENTION_UPLIFT",
        "notes": [
            "Geen parameter- of drempelsweep.",
            "Alle 1m-features zijn gesloten vóór attention cutoff signal+3m; geen latere 1m-data gebruikt.",
            "De uiteindelijke 5m-selector is conditioneel achteraf bekend; dit bewijst dus niet dat 1m/3m zelfstandig kandidaten kan genereren.",
            "Geen wijziging aan handelslogica, Render, PAPER of live-orders.",
        ],
    }
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({
        "decision": result["decision"],
        "usable": result["sample"]["usable"],
        "confirm_n": result["overall"]["confirm"]["n"],
        "uplift_pp": result["overall"]["uplift_pp"],
        "confirm_pf": result["overall"]["confirm"]["pf"],
        "nonconfirm_pf": result["overall"]["nonconfirm"]["pf"],
        "first_half_uplift": result["overall"]["first_half"]["uplift_pp"],
        "second_half_uplift": result["overall"]["second_half"]["uplift_pp"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
