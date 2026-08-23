#!/usr/bin/env python3
"""
Diamond Trader Dynamic Cost Gate Research v1.0

Doel
----
Vergelijk de huidige vaste spreadlimiet (<= 0.10%) met een kostenbewuste gate
voor bestaande LONG trend_breakout, LONG momentum en SHORT momentum signalen.

Belangrijk
---------
- Alleen signalen die door ALLE andere scannerfilters komen worden meegenomen.
  Een alternatief mag uitsluitend een afwijzing op spread herstellen.
- Dynamic gate accepteert maximaal de bestaande brede scanner-spread van 0.25%.
- Historische orderboekslippage is niet beschikbaar. Daarom gebruiken we een
  vaste, expliciete stress-aanname van 0.05% per kant plus 0.10% veiligheidsmarge.
- Gate:
    bruto TP-beweging >= 2x fee + volledige spread + 2x slippage + veiligheidsmarge
- Replay gebruikt publieke Bitvavo 15m candles en rekent fee, spread en dezelfde
  slippage-stress in de gerealiseerde PnL mee.
- Eén virtuele positie per symbool per variant tegelijk.
- Geen orders, private API, config- of LIVE-wijzigingen.
"""

from __future__ import annotations

import argparse
import csv
import math
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import ccxt
import yaml

VERSION = "1.0"
SIGNALS_FILE = Path("/var/data/diamond_market_signals.csv")
CONFIG_FILE = Path("/opt/render/project/src/config.yaml")
TIMEFRAME_MS = 15 * 60 * 1000
DEFAULT_DAYS = 7
DEFAULT_MAX_HOLD_MINUTES = 2880
HARD_SPREAD_PCT = 0.10
BROAD_SPREAD_PCT = 0.25
SLIPPAGE_PER_SIDE_PCT = 0.05
SAFETY_MARGIN_PCT = 0.10

ROUTES = {
    "LONG_TREND": ("LONG", "trend_breakout"),
    "LONG_MOM": ("LONG", "momentum"),
    "SHORT_MOM": ("SHORT", "momentum"),
}


def f(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def b(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "ja", "on"}


def dt(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def settings() -> Dict[str, float]:
    result = {
        "stake": 130.0,
        "fee_pct": 0.25,
        "max_hold_minutes": float(DEFAULT_MAX_HOLD_MINUTES),
    }
    try:
        cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        risk = cfg.get("risk") if isinstance(cfg.get("risk"), dict) else {}
        fees = cfg.get("fees") if isinstance(cfg.get("fees"), dict) else {}
        scanner = cfg.get("market_scanner") if isinstance(cfg.get("market_scanner"), dict) else {}
        result["stake"] = max(5.0, f(scanner.get("stake_eur", risk.get("fixed_stake_quote", 130)), 130.0))
        result["fee_pct"] = max(0.0, f(scanner.get("fee_pct_per_side", fees.get("taker_fee_pct", 0.25)), 0.25))
        result["max_hold_minutes"] = float(max(60, int(f(scanner.get("max_hold_minutes", DEFAULT_MAX_HOLD_MINUTES), DEFAULT_MAX_HOLD_MINUTES))))
    except Exception:
        pass
    return result


def rejection_parts(row: Dict[str, str]) -> List[str]:
    raw = str(row.get("shadow_rejection_reasons") or "").strip()
    if not raw:
        return []
    return [part.strip() for part in raw.split("|") if part.strip()]


def other_filters_pass(row: Dict[str, str]) -> bool:
    if b(row.get("shadow_eligible")):
        return True
    parts = rejection_parts(row)
    return bool(parts) and all(part.lower().startswith("spread ") for part in parts)


def candidate_key(row: Dict[str, str]) -> str:
    return "|".join([
        str(row.get("symbol") or "").upper(),
        str(row.get("strategy") or ""),
        str(row.get("side") or "").upper(),
        str(row.get("candle_timestamp") or ""),
    ])


def route_matches(route: str, row: Dict[str, str]) -> bool:
    side, strategy = ROUTES[route]
    return (
        str(row.get("side") or "").upper() == side
        and str(row.get("strategy") or "") == strategy
    )


def gross_target_pct(row: Dict[str, str]) -> float:
    entry = f(row.get("entry_price"))
    tp = f(row.get("take_profit"))
    if entry <= 0 or tp <= 0:
        return 0.0
    return abs(tp - entry) / entry * 100.0


def required_cost_pct(row: Dict[str, str], cfg: Dict[str, float]) -> float:
    spread = max(0.0, f(row.get("spread_pct")))
    return (
        2.0 * cfg["fee_pct"]
        + spread
        + 2.0 * SLIPPAGE_PER_SIDE_PCT
        + SAFETY_MARGIN_PCT
    )


def accepts_current(route: str, row: Dict[str, str]) -> bool:
    return route_matches(route, row) and b(row.get("shadow_eligible"))


def accepts_dynamic(route: str, row: Dict[str, str], cfg: Dict[str, float]) -> bool:
    if not route_matches(route, row) or not other_filters_pass(row):
        return False
    spread = f(row.get("spread_pct"), 999.0)
    if spread > BROAD_SPREAD_PCT + 1e-12:
        return False
    return gross_target_pct(row) + 1e-12 >= required_cost_pct(row, cfg)


def load_signals(days: int) -> List[Dict[str, str]]:
    if not SIGNALS_FILE.is_file():
        raise FileNotFoundError(SIGNALS_FILE)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    rows: List[Dict[str, str]] = []
    seen = set()
    with SIGNALS_FILE.open("r", encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            detected = dt(raw.get("detected_at"))
            if detected is None or detected < cutoff:
                continue
            if not any(route_matches(route, raw) for route in ROUTES):
                continue
            if f(raw.get("entry_price")) <= 0 or f(raw.get("take_profit")) <= 0 or f(raw.get("stop_loss")) <= 0:
                continue
            key = candidate_key(raw)
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append(dict(raw))
    rows.sort(key=lambda r: dt(r.get("detected_at")) or datetime.min.replace(tzinfo=timezone.utc))
    return rows


def fetch_candles(exchange: ccxt.Exchange, symbol: str, since_ms: int, until_ms: int) -> List[List[Any]]:
    rows: List[List[Any]] = []
    cursor = max(0, since_ms)
    while cursor <= until_ms:
        batch = exchange.fetch_ohlcv(symbol, timeframe="15m", since=cursor, limit=500) or []
        if not batch:
            break
        for candle in batch:
            if len(candle) < 6:
                continue
            stamp = int(candle[0])
            if stamp > until_ms:
                break
            rows.append(candle)
        last_ms = int(batch[-1][0])
        nxt = last_ms + TIMEFRAME_MS
        if nxt <= cursor:
            break
        cursor = nxt
        if len(batch) < 500:
            break
        time.sleep(max(0.0, float(getattr(exchange, "rateLimit", 0) or 0) / 1000.0))
    unique = {int(c[0]): c for c in rows}
    return [unique[k] for k in sorted(unique)]


def make_position(row: Dict[str, str], cfg: Dict[str, float]) -> Optional[Dict[str, Any]]:
    side = str(row.get("side") or "").upper()
    raw_entry = f(row.get("entry_price"))
    raw_tp = f(row.get("take_profit"))
    raw_sl = f(row.get("stop_loss"))
    spread = max(0.0, f(row.get("spread_pct")))
    candle_dt = dt(row.get("candle_timestamp"))
    if side not in {"LONG", "SHORT"} or raw_entry <= 0 or raw_tp <= 0 or raw_sl <= 0 or candle_dt is None:
        return None
    if side == "LONG" and not (raw_tp > raw_entry > raw_sl):
        return None
    if side == "SHORT" and not (raw_tp < raw_entry < raw_sl):
        return None

    half_spread = spread / 200.0
    slip = SLIPPAGE_PER_SIDE_PCT / 100.0
    if side == "LONG":
        entry_exec = raw_entry * (1.0 + half_spread + slip)
    else:
        entry_exec = raw_entry * (1.0 - half_spread - slip)

    stake = cfg["stake"]
    amount = stake / raw_entry
    return {
        "symbol": str(row.get("symbol") or ""),
        "side": side,
        "strategy": str(row.get("strategy") or ""),
        "regime": str(row.get("market_regime") or ""),
        "spread": spread,
        "raw_entry": raw_entry,
        "raw_tp": raw_tp,
        "raw_sl": raw_sl,
        "entry_exec": entry_exec,
        "stake": stake,
        "amount": amount,
        "entry_ms": int(candle_dt.timestamp() * 1000),
        "gross_target_pct": gross_target_pct(row),
        "required_cost_pct": required_cost_pct(row, cfg),
    }


def close_trade(pos: Dict[str, Any], raw_exit: float, reason: str, exit_ms: int, cfg: Dict[str, float]) -> Dict[str, Any]:
    half_spread = pos["spread"] / 200.0
    slip = SLIPPAGE_PER_SIDE_PCT / 100.0
    if pos["side"] == "LONG":
        exit_exec = raw_exit * (1.0 - half_spread - slip)
        gross = (exit_exec - pos["entry_exec"]) * pos["amount"]
    else:
        exit_exec = raw_exit * (1.0 + half_spread + slip)
        gross = (pos["entry_exec"] - exit_exec) * pos["amount"]
    entry_fee = abs(pos["entry_exec"] * pos["amount"]) * cfg["fee_pct"] / 100.0
    exit_fee = abs(exit_exec * pos["amount"]) * cfg["fee_pct"] / 100.0
    return {
        **pos,
        "exit_exec": exit_exec,
        "exit_reason": reason,
        "exit_ms": exit_ms,
        "net_pnl": gross - entry_fee - exit_fee,
        "fees": entry_fee + exit_fee,
    }


def evaluate(pos: Dict[str, Any], candles: Iterable[List[Any]], cfg: Dict[str, float], now_ms: int) -> Optional[Dict[str, Any]]:
    start_ms = pos["entry_ms"] + TIMEFRAME_MS
    maturity_ms = pos["entry_ms"] + int(cfg["max_hold_minutes"] * 60000)
    last_close: Optional[Tuple[int, float]] = None
    for candle in candles:
        stamp = int(candle[0])
        if stamp < start_ms:
            continue
        if stamp > maturity_ms:
            break
        high, low, close = f(candle[2]), f(candle[3]), f(candle[4])
        if close > 0:
            last_close = (stamp + TIMEFRAME_MS, close)
        if pos["side"] == "LONG":
            hit_sl = low > 0 and low <= pos["raw_sl"]
            hit_tp = high > 0 and high >= pos["raw_tp"]
        else:
            hit_sl = high > 0 and high >= pos["raw_sl"]
            hit_tp = low > 0 and low <= pos["raw_tp"]
        if hit_sl:
            return close_trade(pos, pos["raw_sl"], "stop_loss", stamp + TIMEFRAME_MS, cfg)
        if hit_tp:
            return close_trade(pos, pos["raw_tp"], "take_profit", stamp + TIMEFRAME_MS, cfg)
    if now_ms < maturity_ms or last_close is None:
        return None
    return close_trade(pos, last_close[1], "time_exit", last_close[0], cfg)


def pf(trades: List[Dict[str, Any]]) -> Optional[float]:
    gp = sum(max(0.0, x["net_pnl"]) for x in trades)
    gl = abs(sum(min(0.0, x["net_pnl"]) for x in trades))
    if gl > 0:
        return gp / gl
    if gp > 0:
        return math.inf
    return None


def pf_text(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    if math.isinf(value):
        return "INF"
    return f"{value:.3f}"


def summarize(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    wins = sum(t["net_pnl"] > 0 for t in trades)
    losses = sum(t["net_pnl"] < 0 for t in trades)
    pnl = sum(t["net_pnl"] for t in trades)
    return {
        "closed": len(trades),
        "wins": wins,
        "losses": losses,
        "pnl": pnl,
        "pf": pf(trades),
        "avg": pnl / len(trades) if trades else 0.0,
        "fees": sum(t["fees"] for t in trades),
        "tp": sum(t["exit_reason"] == "take_profit" for t in trades),
        "sl": sum(t["exit_reason"] == "stop_loss" for t in trades),
        "time": sum(t["exit_reason"] == "time_exit" for t in trades),
    }


def run_variant(rows: List[Dict[str, str]], route: str, dynamic: bool, candles: Dict[str, List[List[Any]]], cfg: Dict[str, float], now_ms: int) -> Tuple[int, int, int, List[Dict[str, Any]], int]:
    accepted = 0
    overlap = 0
    pending = 0
    rescued = 0
    trades: List[Dict[str, Any]] = []
    busy_until: Dict[str, int] = defaultdict(int)
    for row in rows:
        ok = accepts_dynamic(route, row, cfg) if dynamic else accepts_current(route, row)
        if not ok:
            continue
        accepted += 1
        if dynamic and f(row.get("spread_pct")) > HARD_SPREAD_PCT + 1e-12:
            rescued += 1
        pos = make_position(row, cfg)
        if pos is None:
            continue
        symbol = pos["symbol"]
        if pos["entry_ms"] < busy_until[symbol]:
            overlap += 1
            continue
        trade = evaluate(pos, candles.get(symbol, []), cfg, now_ms)
        if trade is None:
            pending += 1
            busy_until[symbol] = now_ms + 1
            continue
        trades.append(trade)
        busy_until[symbol] = int(trade["exit_ms"])
    return accepted, overlap, pending, trades, rescued


def self_test() -> int:
    cfg = {"stake": 130.0, "fee_pct": 0.25, "max_hold_minutes": 2880.0}
    good = {
        "side": "LONG", "strategy": "momentum", "entry_price": "100",
        "take_profit": "101.5", "stop_loss": "99", "spread_pct": "0.15",
        "shadow_eligible": "False", "shadow_rejection_reasons": "spread 0.1500% hoger dan 0.1000%",
    }
    bad_cost = dict(good, take_profit="100.70")
    bad_other = dict(good, shadow_rejection_reasons="score 60 lager dan 70 | spread 0.1500% hoger dan 0.1000%")
    assert accepts_dynamic("LONG_MOM", good, cfg)
    assert not accepts_dynamic("LONG_MOM", bad_cost, cfg)
    assert not accepts_dynamic("LONG_MOM", bad_other, cfg)
    assert abs(required_cost_pct(good, cfg) - 0.85) < 1e-9
    print("DIAMOND_DYNAMIC_COST_GATE_RESEARCH_SELF_TEST_OK")
    return 0


def run(days: int) -> int:
    cfg = settings()
    rows = load_signals(days)
    selected = [r for r in rows if any(accepts_current(route, r) or accepts_dynamic(route, r, cfg) for route in ROUTES)]
    symbols = sorted({str(r.get("symbol") or "") for r in selected if str(r.get("symbol") or "")})

    exchange = ccxt.bitvavo({"enableRateLimit": True})
    exchange.load_markets()
    now_ms = int(exchange.milliseconds())
    if selected:
        stamps = [dt(r.get("candle_timestamp")) for r in selected]
        valid = [x for x in stamps if x is not None]
        since_ms = int(min(valid).timestamp() * 1000) - TIMEFRAME_MS if valid else now_ms - days * 86400000
    else:
        since_ms = now_ms - days * 86400000

    candles: Dict[str, List[List[Any]]] = {}
    errors: Dict[str, str] = {}
    for i, symbol in enumerate(symbols):
        try:
            candles[symbol] = fetch_candles(exchange, symbol, since_ms, now_ms)
        except Exception as exc:
            errors[symbol] = f"{type(exc).__name__}: {exc}"
        if i + 1 < len(symbols):
            time.sleep(max(0.0, float(getattr(exchange, "rateLimit", 0) or 0) / 1000.0))

    print("=" * 112)
    print(f" DIAMOND DYNAMIC COST GATE RESEARCH v{VERSION}")
    print("=" * 112)
    print(f"Periode              : laatste {days} dagen")
    print(f"Signalen bron        : {len(rows)}")
    print(f"Onderzochte markten  : {len(symbols)}")
    print(f"Stake                : €{cfg['stake']:.2f}")
    print(f"Fee per kant         : {cfg['fee_pct']:.3f}%")
    print(f"Slippage stress      : {SLIPPAGE_PER_SIDE_PCT:.3f}% per kant")
    print(f"Veiligheidsmarge     : {SAFETY_MARGIN_PCT:.3f}%")
    print(f"Dynamic spread cap   : {BROAD_SPREAD_PCT:.3f}%")
    print("Gate                 : bruto TP% >= 2x fee + spread + 2x slippage + marge")
    print()

    for route in ROUTES:
        cur = run_variant(rows, route, False, candles, cfg, now_ms)
        dyn = run_variant(rows, route, True, candles, cfg, now_ms)
        print(f"=== {route} ===")
        for name, data in (("CURRENT_010", cur), ("DYNAMIC_COST", dyn)):
            accepted, overlap, pending, trades, rescued = data
            s = summarize(trades)
            wr = (100.0 * s["wins"] / s["closed"]) if s["closed"] else 0.0
            extra = f" | spread>0.10 gered={rescued}" if name == "DYNAMIC_COST" else ""
            print(
                f"{name:12} accepted={accepted:3d} closed={s['closed']:3d} pending={pending:3d} overlap={overlap:3d}{extra} | "
                f"W/L={s['wins']}/{s['losses']} WR={wr:5.1f}% | PnL=€{s['pnl']:+8.3f} PF={pf_text(s['pf']):>6} AVG=€{s['avg']:+.3f}"
            )
            print(f"             TP/SL/TIME={s['tp']}/{s['sl']}/{s['time']} | fees=€{s['fees']:.3f}")
        csum = summarize(cur[3]); dsum = summarize(dyn[3])
        print(
            f"DELTA        closed={dsum['closed']-csum['closed']:+d} | PnL=€{dsum['pnl']-csum['pnl']:+.3f} | "
            f"PF {pf_text(csum['pf'])} -> {pf_text(dsum['pf'])}"
        )
        if dyn[3]:
            rescued_trades = [t for t in dyn[3] if t["spread"] > HARD_SPREAD_PCT + 1e-12]
            print("Laatste geredde afgeronde trades:")
            if not rescued_trades:
                print("  GEEN")
            else:
                for t in rescued_trades[-5:]:
                    print(
                        f"  {t['symbol']:12} {t['side']:5} spread={t['spread']:.3f}% "
                        f"TPmove={t['gross_target_pct']:.3f}% req={t['required_cost_pct']:.3f}% "
                        f"{t['exit_reason']:11} PnL=€{t['net_pnl']:+.3f}"
                    )
        print()

    if errors:
        print("API-fouten:")
        for symbol, err in sorted(errors.items()):
            print(f"  {symbol}: {err}")
    print("=== VEILIGHEID ===")
    print("Orders/private API : NEE")
    print("Config/strategie    : ONGEWIJZIGD")
    print("LIVE                : ONGEWIJZIGD")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    return run(max(1, args.days))


if __name__ == "__main__":
    raise SystemExit(main())
