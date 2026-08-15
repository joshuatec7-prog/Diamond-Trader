#!/usr/bin/env python3
"""
Diamond Trader Entry Timing Prospective Tracker v1.0

Doel
----
Prospectief bewijzen of 1m-entrytiming na een geldig 15m SELECTIVE-signaal
werkelijk beter blijft dan de huidige directe entry.

Routes
------
IMMEDIATE
    Huidige SELECTIVE-entry als controle.

PULLBACK_030
    Maximaal 30 minuten wachten op 0.30% gunstiger entry.

CONFIRM_CLOSE
    Maximaal 30 minuten wachten op eerste 1m-candle die:
    LONG : groen sluit én boven oorspronkelijke entry sluit
    SHORT: rood sluit én onder oorspronkelijke entry sluit
    Entry op die candle-close.

Belangrijk
----------
- Eerste run maakt ALLEEN een baseline van reeds bestaande SELECTIVE-signalen.
- Alleen NIEUWE SELECTIVE-signalen na die baseline tellen mee.
- Bestaande 15m SELECTIVE/Execution wordt niet gewijzigd.
- Originele 15m TP/SL blijven staan.
- Fee + signaalspread worden meegerekend.
- Geen orders/private API/config/filter/stake/live wijziging.
- Publieke Bitvavo 1m-candles.
- State NIET verwijderen/resetten tijdens deze prospectieve test.

Gebruik
-------
python3 diamond_entry_timing_prospective_tracker.py --self-test
python3 diamond_entry_timing_prospective_tracker.py
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VERSION = "1.0"
MODE = "READ_ONLY_PROSPECTIVE_ENTRY_TIMING"

DATA = Path(os.getenv("DIAMOND_DATA_DIR", "/var/data"))
SIGNALS = DATA / "diamond_market_signals.csv"
STATE = DATA / "diamond_entry_timing_prospective_state.json"
REPORT = DATA / "diamond_entry_timing_prospective_report.json"

BASE_URL = "https://api.bitvavo.com/v2"
STAKE_EUR = 130.0
FEE_PCT_PER_SIDE = 0.25
WAIT_MINUTES = 30
HORIZON_MINUTES = 12 * 60
TARGET_NEW_SIGNALS = 20
TF_MS = 60_000

ROUTES = ("IMMEDIATE", "PULLBACK_030", "CONFIRM_CLOSE")

SAFETY = {
    "orders": False,
    "private_api": False,
    "api_keys": False,
    "config_change": False,
    "strategy_change": False,
    "filter_change": False,
    "stake_change": False,
    "live_change": False,
    "source_files_modified": False,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def to_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {
        "1", "true", "yes", "ja", "on"
    }


def parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def dt_ms(value: Any) -> int:
    dt = parse_dt(value)
    return int(dt.timestamp() * 1000) if dt else 0


def ceil_interval(ms: int, interval_ms: int = TF_MS) -> int:
    return ((ms + interval_ms - 1) // interval_ms) * interval_ms


def market_name(symbol: str) -> str:
    return str(symbol).upper().replace("/", "-")


def signal_key(row: Dict[str, Any]) -> str:
    return "|".join([
        str(row.get("symbol") or "").upper(),
        str(row.get("candle_timestamp") or ""),
        str(row.get("side") or "").upper(),
        str(row.get("strategy") or ""),
    ])


def atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as h:
            json.dump(payload, h, ensure_ascii=False, indent=2)
            h.write("\n")
            h.flush()
            os.fsync(h.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass


def selective_accepts(row: Dict[str, Any]) -> bool:
    if not to_bool(row.get("shadow_eligible")):
        return False

    side = str(row.get("side") or "").upper()
    strategy = str(row.get("strategy") or "")
    regime = str(row.get("market_regime") or "").upper()

    if side == "LONG" and strategy == "trend_breakout":
        return True
    if side == "SHORT" and regime == "BEARISH_WEAK":
        return True
    if side == "SHORT" and strategy in {"momentum", "pullback_retest"}:
        return True
    return False


def load_selective_signals() -> List[Dict[str, Any]]:
    if not SIGNALS.is_file():
        raise FileNotFoundError(SIGNALS)

    with SIGNALS.open("r", encoding="utf-8-sig", newline="") as h:
        reader = csv.DictReader(h)
        required = {
            "detected_at", "candle_timestamp", "symbol", "strategy", "side",
            "market_regime", "score", "entry_price", "take_profit",
            "stop_loss", "spread_pct", "reward_risk", "shadow_eligible",
        }
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise RuntimeError(
                "Signalenbestand mist kolommen: " + ", ".join(sorted(missing))
            )

        rows: List[Dict[str, Any]] = []
        for raw in reader:
            if not selective_accepts(raw):
                continue
            detected = dt_ms(raw.get("detected_at"))
            if detected <= 0:
                continue
            if min(
                f(raw.get("entry_price")),
                f(raw.get("take_profit")),
                f(raw.get("stop_loss")),
            ) <= 0:
                continue

            row = dict(raw)
            row["_detected_ms"] = detected
            row["_score"] = f(raw.get("score"))
            rows.append(row)

    # Eén kandidaat per symbol+candle; hoogste score wint.
    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row.get("symbol") or "").upper(),
            str(row.get("candle_timestamp") or ""),
        )
        old = grouped.get(key)
        if old is None or row["_score"] > old["_score"]:
            grouped[key] = row

    return sorted(
        grouped.values(),
        key=lambda r: int(r["_detected_ms"]),
    )


def default_state() -> Dict[str, Any]:
    return {
        "version": VERSION,
        "mode": MODE,
        "created_at": now_iso(),
        "baseline_at": None,
        "baseline_signal_count": 0,
        "baseline_keys": [],
        "seen_keys": [],
        "observations": {},
        "last_run_at": None,
        "run_count": 0,
    }


def load_state() -> Dict[str, Any]:
    if not STATE.is_file():
        return default_state()

    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state is geen dict")
    except Exception:
        raise RuntimeError(
            f"Prospective state ongeldig/onleesbaar: {STATE}"
        )

    data["version"] = VERSION
    data["mode"] = MODE
    data.setdefault("baseline_at", None)
    data.setdefault("baseline_signal_count", 0)
    data.setdefault("baseline_keys", [])
    data.setdefault("seen_keys", [])
    data.setdefault("observations", {})
    data.setdefault("run_count", 0)
    return data


def fetch_candles(
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> List[List[float]]:
    params = urllib.parse.urlencode({
        "interval": "1m",
        "start": start_ms,
        "end": end_ms,
        "limit": 1000,
    })
    url = (
        f"{BASE_URL}/{urllib.parse.quote(market_name(symbol), safe='-')}"
        f"/candles?{params}"
    )

    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "DiamondTraderProspectiveEntryTiming/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if not isinstance(payload, list):
                raise RuntimeError("Candles response is geen lijst")

            rows: List[List[float]] = []
            for item in payload:
                if not isinstance(item, list) or len(item) < 6:
                    continue
                try:
                    rows.append([
                        float(int(item[0])),
                        float(item[1]),
                        float(item[2]),
                        float(item[3]),
                        float(item[4]),
                        float(item[5]),
                    ])
                except Exception:
                    continue
            rows.sort(key=lambda x: x[0])
            return rows
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))

    raise RuntimeError(
        f"{symbol}: {type(last_exc).__name__}: {last_exc}"
    )


def entry_exec(raw_entry: float, side: str, spread_pct: float) -> float:
    half = max(0.0, spread_pct) / 200.0
    if side == "LONG":
        return raw_entry * (1.0 + half)
    return raw_entry * (1.0 - half)


def exit_exec(raw_exit: float, side: str, spread_pct: float) -> float:
    half = max(0.0, spread_pct) / 200.0
    if side == "LONG":
        return raw_exit * (1.0 - half)
    return raw_exit * (1.0 + half)


def trade_net_pnl(
    raw_entry: float,
    raw_exit: float,
    side: str,
    spread_pct: float,
) -> float:
    e = entry_exec(raw_entry, side, spread_pct)
    x = exit_exec(raw_exit, side, spread_pct)
    amount = STAKE_EUR / e
    entry_fee = STAKE_EUR * FEE_PCT_PER_SIDE / 100.0
    exit_fee = amount * x * FEE_PCT_PER_SIDE / 100.0

    if side == "LONG":
        gross = (x - e) * amount
    else:
        gross = (e - x) * amount

    return gross - entry_fee - exit_fee


def new_observation(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "key": signal_key(row),
        "detected_at": str(row.get("detected_at") or ""),
        "detected_ms": int(row["_detected_ms"]),
        "candle_timestamp": str(row.get("candle_timestamp") or ""),
        "symbol": str(row.get("symbol") or "").upper(),
        "side": str(row.get("side") or "").upper(),
        "strategy": str(row.get("strategy") or ""),
        "market_regime": str(row.get("market_regime") or ""),
        "score": f(row.get("score")),
        "reward_risk": f(row.get("reward_risk")),
        "spread_pct": max(0.0, f(row.get("spread_pct"))),
        "entry_price": f(row.get("entry_price")),
        "take_profit": f(row.get("take_profit")),
        "stop_loss": f(row.get("stop_loss")),
        "added_at": now_iso(),
        "routes": {
            route: {
                "status": "WAITING",
                "entry_raw": None,
                "entry_ms": None,
                "exit_raw": None,
                "exit_ms": None,
                "net_pnl_eur": None,
                "wait_minutes": None,
            }
            for route in ROUTES
        },
    }


def route_fill(
    obs: Dict[str, Any],
    candles: List[List[float]],
    route: str,
    current_ms: int,
) -> None:
    r = obs["routes"][route]
    if r["status"] != "WAITING":
        return

    side = obs["side"]
    original_entry = float(obs["entry_price"])
    detected_ms = int(obs["detected_ms"])
    start_ms = ceil_interval(detected_ms)
    wait_end = start_ms + WAIT_MINUTES * 60_000

    if route == "IMMEDIATE":
        r["status"] = "OPEN"
        r["entry_raw"] = original_entry
        r["entry_ms"] = start_ms
        r["wait_minutes"] = 0.0
        return

    waiting = [
        c for c in candles
        if start_ms <= int(c[0]) < min(wait_end, current_ms + 1)
    ]

    if route == "PULLBACK_030":
        pct = 0.003
        trigger = (
            original_entry * (1.0 - pct)
            if side == "LONG"
            else original_entry * (1.0 + pct)
        )

        for candle in waiting:
            high = float(candle[2])
            low = float(candle[3])
            hit = low <= trigger if side == "LONG" else high >= trigger
            if hit:
                ts = int(candle[0])
                r["status"] = "OPEN"
                r["entry_raw"] = trigger
                r["entry_ms"] = ts
                r["wait_minutes"] = (ts - start_ms) / 60_000.0
                return

    elif route == "CONFIRM_CLOSE":
        for candle in waiting:
            ts = int(candle[0])
            op = float(candle[1])
            close = float(candle[4])
            confirmed = (
                (close > op and close > original_entry)
                if side == "LONG"
                else (close < op and close < original_entry)
            )
            if confirmed:
                # Entry op candle close; exit-controle vanaf volgende candle.
                r["status"] = "OPEN"
                r["entry_raw"] = close
                r["entry_ms"] = ts + TF_MS
                r["wait_minutes"] = (
                    ts + TF_MS - start_ms
                ) / 60_000.0
                return

    if current_ms >= wait_end:
        r["status"] = "NO_FILL"


def route_exit(
    obs: Dict[str, Any],
    candles: List[List[float]],
    route: str,
    current_ms: int,
) -> None:
    r = obs["routes"][route]
    if r["status"] != "OPEN":
        return

    side = obs["side"]
    tp = float(obs["take_profit"])
    sl = float(obs["stop_loss"])
    spread = float(obs["spread_pct"])
    entry_raw = float(r["entry_raw"])
    entry_ms = int(r["entry_ms"])
    detected_ms = int(obs["detected_ms"])
    horizon_end = detected_ms + HORIZON_MINUTES * 60_000

    # Alleen candles die inmiddels volledig beschikbaar kunnen zijn.
    available_end = min(current_ms, horizon_end)
    post = [
        c for c in candles
        if entry_ms <= int(c[0]) < available_end
    ]
    if not post:
        return

    for candle in post:
        ts = int(candle[0])
        high = float(candle[2])
        low = float(candle[3])

        if side == "LONG":
            stop_hit = low <= sl
            tp_hit = high >= tp
        else:
            stop_hit = high >= sl
            tp_hit = low <= tp

        # Conservatief bij TP+SL in dezelfde 1m-candle.
        if stop_hit:
            r["status"] = "STOP"
            r["exit_raw"] = sl
            r["exit_ms"] = ts
            r["net_pnl_eur"] = round(
                trade_net_pnl(entry_raw, sl, side, spread), 6
            )
            return

        if tp_hit:
            r["status"] = "TP"
            r["exit_raw"] = tp
            r["exit_ms"] = ts
            r["net_pnl_eur"] = round(
                trade_net_pnl(entry_raw, tp, side, spread), 6
            )
            return

    if current_ms >= horizon_end:
        last = post[-1]
        raw_exit = float(last[4])
        r["status"] = "TIME"
        r["exit_raw"] = raw_exit
        r["exit_ms"] = int(last[0])
        r["net_pnl_eur"] = round(
            trade_net_pnl(entry_raw, raw_exit, side, spread), 6
        )


def update_observation(
    obs: Dict[str, Any],
    current_ms: int,
) -> Optional[str]:
    detected_ms = int(obs["detected_ms"])
    start_ms = ceil_interval(detected_ms)
    horizon_end = detected_ms + HORIZON_MINUTES * 60_000

    # Max 12h + klein beetje ruimte; Bitvavo limit 1000 is genoeg voor 1m.
    end_ms = min(current_ms, horizon_end + TF_MS)
    if end_ms <= start_ms:
        return None

    try:
        candles = fetch_candles(
            obs["symbol"],
            start_ms,
            end_ms,
        )
    except Exception as exc:
        return str(exc)

    for route in ROUTES:
        route_fill(obs, candles, route, current_ms)
        route_exit(obs, candles, route, current_ms)

    obs["last_updated_at"] = now_iso()
    return None


def pf(values: List[float]) -> Optional[float]:
    gp = sum(x for x in values if x > 0)
    gl = abs(sum(x for x in values if x < 0))
    if gl > 0:
        return gp / gl
    if gp > 0:
        return math.inf
    return None


def max_drawdown(values: List[float]) -> float:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def summarize_route(
    observations: List[Dict[str, Any]],
    route: str,
) -> Dict[str, Any]:
    rows = [o["routes"][route] for o in observations]
    filled = [
        r for r in rows
        if r["status"] in {"OPEN", "TP", "STOP", "TIME"}
    ]
    closed = [
        r for r in rows
        if r["status"] in {"TP", "STOP", "TIME"}
    ]
    pnl = [f(r.get("net_pnl_eur")) for r in closed]
    p = pf(pnl)

    return {
        "signals": len(rows),
        "waiting": sum(r["status"] == "WAITING" for r in rows),
        "no_fill": sum(r["status"] == "NO_FILL" for r in rows),
        "filled": len(filled),
        "open": sum(r["status"] == "OPEN" for r in rows),
        "closed": len(closed),
        "tp": sum(r["status"] == "TP" for r in rows),
        "stop": sum(r["status"] == "STOP" for r in rows),
        "time": sum(r["status"] == "TIME" for r in rows),
        "pnl_eur": round(sum(pnl), 4),
        "profit_factor": (
            None if p is None
            else "INF" if math.isinf(p)
            else round(p, 4)
        ),
        "max_drawdown_eur": round(max_drawdown(pnl), 4),
        "avg_closed_trade_eur": (
            round(sum(pnl) / len(pnl), 4) if pnl else None
        ),
        "avg_per_signal_eur": (
            round(sum(pnl) / len(rows), 4) if rows else None
        ),
    }


def build_report(
    state: Dict[str, Any],
    new_added: int,
    network_errors: List[str],
) -> Dict[str, Any]:
    observations = sorted(
        state["observations"].values(),
        key=lambda o: int(o["detected_ms"]),
    )
    route_summary = {
        route: summarize_route(observations, route)
        for route in ROUTES
    }

    return {
        "version": VERSION,
        "mode": MODE,
        "generated_at": now_iso(),
        "baseline_at": state["baseline_at"],
        "baseline_signal_count": state["baseline_signal_count"],
        "new_signals_total": len(observations),
        "new_signals_added_now": new_added,
        "target_new_signals": TARGET_NEW_SIGNALS,
        "route_summary": route_summary,
        "network_error_count": len(network_errors),
        "network_errors": network_errors[-20:],
        "state_file": str(STATE),
        "safety": SAFETY,
        "status": (
            "BASELINE_READY_WAIT_NEW_SIGNALS"
            if len(observations) == 0
            else "PROSPECTIVE_RUNNING"
        ),
        "rules": {
            "signal": "new SELECTIVE after fixed baseline only",
            "immediate": "current SELECTIVE entry",
            "pullback_030": "0.30% favorable touch, max 30m",
            "confirm_close": "directional 1m close beyond original entry, max 30m",
            "tp_sl": "original 15m signal TP/SL",
            "horizon_minutes": HORIZON_MINUTES,
            "fee_pct_per_side": FEE_PCT_PER_SIDE,
            "stake_eur": STAKE_EUR,
            "spread": "signal spread reused at entry and exit",
        },
    }


def fmt_pf(value: Any) -> str:
    if value is None:
        return "n/a"
    if str(value).upper() == "INF":
        return "INF"
    try:
        return f"{float(value):.3f}"
    except Exception:
        return "n/a"


def print_report(report: Dict[str, Any]) -> None:
    print("=" * 98)
    print(f" DIAMOND ENTRY TIMING PROSPECTIVE TRACKER v{VERSION}")
    print("=" * 98)
    print(
        f"Baseline signalen : {report['baseline_signal_count']} | "
        f"nieuw prospectief : {report['new_signals_total']}/{TARGET_NEW_SIGNALS} | "
        f"nieuw deze run: {report['new_signals_added_now']}"
    )
    print(f"Status            : {report['status']}")
    print()

    for route in ROUTES:
        s = report["route_summary"][route]
        print(
            f"{route:<14} "
            f"signals={s['signals']:>2}/{TARGET_NEW_SIGNALS} | "
            f"fill={s['filled']:>2} nofill={s['no_fill']:>2} wait={s['waiting']:>2} | "
            f"closed={s['closed']:>2} open={s['open']:>2} | "
            f"TP/S/T={s['tp']}/{s['stop']}/{s['time']} | "
            f"PnL=€{f(s['pnl_eur']):+8.3f} | "
            f"PF={fmt_pf(s['profit_factor'])} | "
            f"DD=€{f(s['max_drawdown_eur']):.2f}"
        )

    print()
    print("=== VEILIGHEID ===")
    print("Alleen NIEUWE signalen na baseline : JA")
    print("15m SELECTIVE/Execution gewijzigd  : NEE")
    print("Orders/private API                 : NEE")
    print("Config/filter/stake/live           : NEE")
    print("Publieke 1m candles                : JA")
    print(f"Netwerkfouten                       : {report['network_error_count']}")
    print(f"State NIET verwijderen              : {STATE}")


def run_once() -> Dict[str, Any]:
    state = load_state()
    signals = load_selective_signals()
    current_keys = {signal_key(r) for r in signals}

    # Eerste run: alleen baseline vastleggen.
    if state["baseline_at"] is None:
        state["baseline_at"] = now_iso()
        state["baseline_signal_count"] = len(signals)
        state["baseline_keys"] = sorted(current_keys)
        state["seen_keys"] = sorted(current_keys)
        state["last_run_at"] = now_iso()
        state["run_count"] = int(state.get("run_count", 0)) + 1
        atomic_json(STATE, state)

        report = build_report(state, 0, [])
        atomic_json(REPORT, report)
        return report

    seen = set(state.get("seen_keys") or [])
    observations = state.get("observations") or {}
    new_added = 0

    for row in signals:
        key = signal_key(row)
        if key in seen:
            continue
        observations[key] = new_observation(row)
        seen.add(key)
        new_added += 1

    state["observations"] = observations
    state["seen_keys"] = sorted(seen)

    current = now_ms()
    network_errors: List[str] = []

    for obs in state["observations"].values():
        # Afgerond als alle routes definitief zijn.
        statuses = {
            obs["routes"][route]["status"]
            for route in ROUTES
        }
        if statuses.issubset({"TP", "STOP", "TIME", "NO_FILL"}):
            continue

        err = update_observation(obs, current)
        if err:
            network_errors.append(err)
        time.sleep(0.04)

    state["last_run_at"] = now_iso()
    state["run_count"] = int(state.get("run_count", 0)) + 1
    atomic_json(STATE, state)

    report = build_report(state, new_added, network_errors)
    atomic_json(REPORT, report)
    return report


def self_test() -> None:
    base = {
        "shadow_eligible": "True",
        "side": "LONG",
        "strategy": "trend_breakout",
        "market_regime": "BULLISH",
        "entry_price": "100",
        "take_profit": "104",
        "stop_loss": "98",
        "spread_pct": "0.10",
        "score": "90",
        "reward_risk": "1.8",
        "symbol": "BTC/EUR",
        "candle_timestamp": "2026-08-15T00:00:00+00:00",
        "detected_at": "2026-08-15T00:00:10+00:00",
        "_detected_ms": dt_ms("2026-08-15T00:00:10+00:00"),
    }
    assert selective_accepts(base)
    assert signal_key(base).startswith("BTC/EUR|")

    obs = new_observation(base)
    assert set(obs["routes"]) == set(ROUTES)
    assert all(
        obs["routes"][r]["status"] == "WAITING"
        for r in ROUTES
    )

    start = ceil_interval(obs["detected_ms"])
    candles = [
        [float(start), 100.0, 100.1, 99.9, 100.0, 1.0],
        [float(start + TF_MS), 100.0, 100.1, 99.6, 99.7, 1.0],
        [float(start + 2*TF_MS), 99.7, 100.4, 99.7, 100.3, 1.0],
    ]
    current = start + 3 * TF_MS

    route_fill(obs, candles, "IMMEDIATE", current)
    assert obs["routes"]["IMMEDIATE"]["status"] == "OPEN"
    assert abs(obs["routes"]["IMMEDIATE"]["entry_raw"] - 100.0) < 1e-9

    route_fill(obs, candles, "PULLBACK_030", current)
    assert obs["routes"]["PULLBACK_030"]["status"] == "OPEN"
    assert abs(obs["routes"]["PULLBACK_030"]["entry_raw"] - 99.7) < 1e-9

    route_fill(obs, candles, "CONFIRM_CLOSE", current)
    assert obs["routes"]["CONFIRM_CLOSE"]["status"] == "OPEN"
    assert abs(obs["routes"]["CONFIRM_CLOSE"]["entry_raw"] - 100.3) < 1e-9

    assert trade_net_pnl(100, 104, "LONG", 0.10) > 0
    assert trade_net_pnl(100, 96, "SHORT", 0.10) > 0
    assert SAFETY["orders"] is False
    assert SAFETY["private_api"] is False
    assert SAFETY["live_change"] is False

    print("ENTRY_TIMING_PROSPECTIVE_SELF_TEST_OK")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diamond prospective entry timing tracker"
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0

    try:
        report = run_once()
        print_report(report)
        return 0
    except Exception as exc:
        print("=" * 98)
        print(f" DIAMOND ENTRY TIMING PROSPECTIVE TRACKER v{VERSION}")
        print("=" * 98)
        print(f"STATUS: FOUT | {type(exc).__name__}: {exc}")
        print("Orders/private API/config/live: NEE")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
