#!/usr/bin/env python3
"""Technical preflight for Step 8 historical 1m candle timing.

OFFLINE/READ-ONLY research diagnostic. No PAPER/live/runtime config changes.
It compares Bitvavo's raw aligned 1m candle request with the repository helper
before Step 8 is allowed to run again.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bitvavo_public import BitvavoPublic

MINUTE_MS = 60_000
SIGNAL_MS = 1788068100000  # 2026-08-30 05:35:00 UTC, exact 5m boundary


def compact(rows):
    return [int(row[0]) for row in rows if isinstance(row, (list, tuple)) and len(row) >= 6]


def main() -> int:
    api = BitvavoPublic("https://api.bitvavo.com/v2", timeout_seconds=20, retries=2)
    start = SIGNAL_MS - 5 * MINUTE_MS
    cutoff = SIGNAL_MS + 3 * MINUTE_MS
    required = {
        "baseline": SIGNAL_MS - MINUTE_MS,
        "current_0": SIGNAL_MS,
        "current_1": SIGNAL_MS + MINUTE_MS,
        "current_2": SIGNAL_MS + 2 * MINUTE_MS,
    }

    result = {
        "version": "v40-step8-candle-preflight-1",
        "mode": "OFFLINE_MEASUREMENT_ONLY",
        "execution_enabled": False,
        "live_orders_possible": False,
        "active_paper_changed": False,
        "market": "BTC-EUR",
        "interval": "1m",
        "signal_ms": SIGNAL_MS,
        "start_ms": start,
        "cutoff_ms": cutoff,
        "required_timestamps": required,
        "raw_aligned": {},
        "raw_end_minus_1": {},
        "helper": {},
    }

    try:
        raw = api._get("/BTC-EUR/candles", {
            "interval": "1m", "limit": 30, "start": start, "end": cutoff,
        })
        ts = compact(raw)
        result["raw_aligned"] = {"ok": True, "timestamps": ts, "required_present": {k: v in ts for k, v in required.items()}}
    except Exception as exc:
        result["raw_aligned"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        raw = api._get("/BTC-EUR/candles", {
            "interval": "1m", "limit": 30, "start": start, "end": cutoff - 1,
        })
        ts = compact(raw)
        result["raw_end_minus_1"] = {"ok": True, "timestamps": ts, "required_present": {k: v in ts for k, v in required.items()}}
    except Exception as exc:
        result["raw_end_minus_1"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        rows = api.closed_candles_between("BTC-EUR", "1m", start, cutoff, now_ms=cutoff, page_limit=30, max_pages=2)
        ts = [int(c.timestamp_ms) for c in rows]
        result["helper"] = {"ok": True, "timestamps": ts, "required_present": {k: v in ts for k, v in required.items()}}
    except Exception as exc:
        result["helper"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    aligned = result["raw_aligned"]
    result["passed"] = bool(
        aligned.get("ok")
        and all((aligned.get("required_present") or {}).values())
    )
    result["diagnosis"] = (
        "RAW_ALIGNED_WORKS_HELPER_NEEDS_STEP8_BYPASS"
        if result["passed"] and not result["helper"].get("ok")
        else "RAW_ALIGNED_AND_HELPER_WORK"
        if result["passed"] and result["helper"].get("ok")
        else "HISTORICAL_1M_NOT_CONFIRMED"
    )
    Path("v40_step8_candle_preflight_result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
