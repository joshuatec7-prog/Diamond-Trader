#!/usr/bin/env python3
"""Read-only bearish/sideways market research for Diamond Trader.

Compares already-existing scanner signal families over recent public Bitvavo data.
No orders, no private API, no config writes, no LIVE changes.

Variants:
- SHORT_BEARISH_WEAK      : SHORT in BEARISH_WEAK regime
- SHORT_MOMENTUM          : SHORT momentum in BEARISH/BEARISH_WEAK
- SHORT_PULLBACK          : SHORT pullback_retest in BEARISH/BEARISH_WEAK
- SHORT_RANGE_BREAKOUT    : SHORT range_breakout
- SIDE_MEANREV_LONG       : LONG mean_reversion in NEUTRAL
- SIDE_RANGE_LONG         : LONG range_breakout in NEUTRAL
- SIDE_RANGE_SHORT        : SHORT range_breakout in NEUTRAL

Only signals already marked shadow_eligible=True are admitted, so current scanner
score/spread/RR/expected-profit filters stay intact. Replay uses 15m candles,
current configured stake/fees/max-hold, conservative same-candle SL precedence,
and reuses the signal spread on both entry and exit.

Important: SHORT results are a strategy-quality proxy only. Real short trading
would later need a suitable venue/product and separate funding/borrow/liquidation
cost modelling before any LIVE consideration.
"""

from __future__ import annotations

import argparse
import csv
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import ccxt
import yaml

SIGNALS_FILE = Path("/var/data/diamond_market_signals.csv")
CONFIG_FILE = Path("/opt/render/project/src/config.yaml")
TIMEFRAME_MS = 15 * 60 * 1000
DEFAULT_DAYS = 7
DEFAULT_MAX_HOLD_MINUTES = 2880

VARIANTS = (
    "SHORT_BEARISH_WEAK",
    "SHORT_MOMENTUM",
    "SHORT_PULLBACK",
    "SHORT_RANGE_BREAKOUT",
    "SIDE_MEANREV_LONG",
    "SIDE_RANGE_LONG",
    "SIDE_RANGE_SHORT",
)


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
        result["stake"] = max(
            5.0,
            f(scanner.get("stake_eur", risk.get("fixed_stake_quote", 130)), 130.0),
        )
        result["fee_pct"] = max(
            0.0,
            f(scanner.get("fee_pct_per_side", fees.get("taker_fee_pct", 0.25)), 0.25),
        )
        result["max_hold_minutes"] = float(
            max(60, int(f(scanner.get("max_hold_minutes", DEFAULT_MAX_HOLD_MINUTES), DEFAULT_MAX_HOLD_MINUTES)))
        )
    except Exception:
        pass
    return result


def candidate_key(row: Dict[str, str]) -> str:
    return "|".join([
        str(row.get("symbol") or "").upper(),
        str(row.get("strategy") or ""),
        str(row.get("side") or "").upper(),
        str(row.get("candle_timestamp") or ""),
    ])


def load_signals(days: int) -> List[Dict[str, str]]:
    if not SIGNALS_FILE.is_file():
        raise FileNotFoundError(SIGNALS_FILE)

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    rows: List[Dict[str, str]] = []
    seen = set()

    with SIGNALS_FILE.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            detected = dt(raw.get("detected_at"))
            if detected is None or detected < cutoff:
                continue
            if not b(raw.get("shadow_eligible")):
                continue
            if str(raw.get("side") or "").upper() not in {"LONG", "SHORT"}:
                continue
            if f(raw.get("entry_price")) <= 0 or f(raw.get("take_profit")) <= 0 or f(raw.get("stop_loss")) <= 0:
                continue
            key = candidate_key(raw)
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append(dict(raw))

    rows.sort(key=lambda row: dt(row.get("detected_at")) or datetime.min.replace(tzinfo=timezone.utc))
    return rows


def accepts(name: str, row: Dict[str, str]) -> bool:
    side = str(row.get("side") or "").upper()
    strategy = str(row.get("strategy") or "")
    regime = str(row.get("market_regime") or "").upper()

    if name == "SHORT_BEARISH_WEAK":
        return side == "SHORT" and regime == "BEARISH_WEAK"
    if name == "SHORT_MOMENTUM":
        return side == "SHORT" and strategy == "momentum" and regime in {"BEARISH", "BEARISH_WEAK"}
    if name == "SHORT_PULLBACK":
        return side == "SHORT" and strategy == "pullback_retest" and regime in {"BEARISH", "BEARISH_WEAK"}
    if name == "SHORT_RANGE_BREAKOUT":
        return side == "SHORT" and strategy == "range_breakout"
    if name == "SIDE_MEANREV_LONG":
        return side == "LONG" and strategy == "mean_reversion" and regime == "NEUTRAL"
    if name == "SIDE_RANGE_LONG":
        return side == "LONG" and strategy == "range_breakout" and regime == "NEUTRAL"
    if name == "SIDE_RANGE_SHORT":
        return side == "SHORT" and strategy == "range_breakout" and regime == "NEUTRAL"
    return False


def fetch_candles(exchange: ccxt.Exchange, symbol: str, since_ms: int, until_ms: int) -> List[List[Any]]:
    rows: List[List[Any]] = []
    cursor = max(0, since_ms)
    while cursor <= until_ms:
        batch = exchange.fetch_ohlcv(symbol, timeframe="15m", since=cursor, limit=500) or []
        if not batch:
            break
        for candle in batch:
            if candle and len(candle) >= 6 and int(candle[0]) <= until_ms:
                rows.append(candle)
        last_ms = int(batch[-1][0])
        next_cursor = last_ms + TIMEFRAME_MS
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(batch) < 500:
            break
        time.sleep(max(0.0, float(getattr(exchange, "rateLimit", 0) or 0) / 1000.0))

    unique: Dict[int, List[Any]] = {}
    for candle in rows:
        unique[int(candle[0])] = candle
    return [unique[k] for k in sorted(unique)]


def make_position(row: Dict[str, str], cfg: Dict[str, float]) -> Optional[Dict[str, Any]]:
    side = str(row.get("side") or "").upper()
    raw_entry = f(row.get("entry_price"))
    raw_tp = f(row.get("take_profit"))
    raw_sl = f(row.get("stop_loss"))
    spread = max(0.0, f(row.get("spread_pct")))
    candle_dt = dt(row.get("candle_timestamp"))

    if side == "LONG":
        valid = raw_tp > raw_entry > raw_sl > 0
    elif side == "SHORT":
        valid = raw_sl > raw_entry > raw_tp > 0
    else:
        valid = False
    if not valid or candle_dt is None:
        return None

    half = spread / 200.0
    if side == "LONG":
        entry = raw_entry * (1.0 + half)
        tp = entry + (raw_tp - raw_entry)
        sl = entry - (raw_entry - raw_sl)
    else:
        entry = raw_entry * (1.0 - half)
        tp = entry - (raw_entry - raw_tp)
        sl = entry + (raw_sl - raw_entry)

    stake = cfg["stake"]
    amount = stake / entry
    return {
        "symbol": str(row.get("symbol") or ""),
        "side": side,
        "strategy": str(row.get("strategy") or ""),
        "regime": str(row.get("market_regime") or ""),
        "score": f(row.get("score")),
        "rr": f(row.get("reward_risk")),
        "spread": spread,
        "entry": entry,
        "tp": tp,
        "sl": sl,
        "stake": stake,
        "amount": amount,
        "entry_ms": int(candle_dt.timestamp() * 1000),
    }


def close_trade(position: Dict[str, Any], raw_exit: float, reason: str, exit_ms: int, cfg: Dict[str, float]) -> Dict[str, Any]:
    half = position["spread"] / 200.0
    if position["side"] == "LONG":
        exit_price = raw_exit * (1.0 - half)
        gross = (exit_price - position["entry"]) * position["amount"]
    else:
        exit_price = raw_exit * (1.0 + half)
        gross = (position["entry"] - exit_price) * position["amount"]

    entry_fee = position["stake"] * cfg["fee_pct"] / 100.0
    exit_notional = exit_price * position["amount"]
    exit_fee = exit_notional * cfg["fee_pct"] / 100.0
    net = gross - entry_fee - exit_fee

    return {
        **position,
        "exit_price": exit_price,
        "exit_reason": reason,
        "exit_ms": exit_ms,
        "net_pnl": net,
        "fees": entry_fee + exit_fee,
    }


def evaluate(position: Dict[str, Any], candles: Iterable[List[Any]], cfg: Dict[str, float], now_ms: int) -> Optional[Dict[str, Any]]:
    start_ms = position["entry_ms"] + TIMEFRAME_MS
    maturity_ms = position["entry_ms"] + int(cfg["max_hold_minutes"] * 60000)
    last_close: Optional[Tuple[int, float]] = None

    for candle in candles:
        stamp = int(candle[0])
        if stamp < start_ms:
            continue
        if stamp > maturity_ms:
            break
        high = f(candle[2])
        low = f(candle[3])
        close = f(candle[4])
        if close > 0:
            last_close = (stamp + TIMEFRAME_MS, close)

        if position["side"] == "LONG":
            hit_sl = low > 0 and low <= position["sl"]
            hit_tp = high > 0 and high >= position["tp"]
        else:
            hit_sl = high > 0 and high >= position["sl"]
            hit_tp = low > 0 and low <= position["tp"]

        if hit_sl:
            return close_trade(position, position["sl"], "stop_loss", stamp + TIMEFRAME_MS, cfg)
        if hit_tp:
            return close_trade(position, position["tp"], "take_profit", stamp + TIMEFRAME_MS, cfg)

    if now_ms < maturity_ms or last_close is None:
        return None
    return close_trade(position, last_close[1], "time_exit", last_close[0], cfg)


def pf(trades: List[Dict[str, Any]]) -> Optional[float]:
    gp = sum(max(0.0, t["net_pnl"]) for t in trades)
    gl = abs(sum(min(0.0, t["net_pnl"]) for t in trades))
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


def run(days: int) -> None:
    cfg = settings()
    signals = load_signals(days)
    exchange = ccxt.bitvavo({"enableRateLimit": True})
    exchange.load_markets()
    now_ms = int(exchange.milliseconds())

    relevant = [r for r in signals if any(accepts(name, r) for name in VARIANTS)]
    symbols = sorted({str(r.get("symbol") or "") for r in relevant if str(r.get("symbol") or "")})

    if relevant:
        stamps = [dt(r.get("candle_timestamp")) for r in relevant]
        stamps = [x for x in stamps if x is not None]
        since_ms = int(min(stamps).timestamp() * 1000) - TIMEFRAME_MS if stamps else now_ms - days * 86400000
    else:
        since_ms = now_ms - days * 86400000

    candles: Dict[str, List[List[Any]]] = {}
    errors: Dict[str, str] = {}
    for i, symbol in enumerate(symbols, 1):
        try:
            candles[symbol] = fetch_candles(exchange, symbol, since_ms, now_ms)
        except Exception as exc:
            errors[symbol] = f"{type(exc).__name__}: {exc}"
        if i < len(symbols):
            time.sleep(max(0.0, float(getattr(exchange, "rateLimit", 0) or 0) / 1000.0))

    print("=" * 108)
    print(" DIAMOND BEARISH + SIDEWAYS MARKET RESEARCH")
    print("=" * 108)
    print(f"Periode             : laatste {days} dagen")
    print(f"Eligible signalen   : {len(signals)}")
    print(f"Relevante signalen  : {len(relevant)}")
    print(f"Onderzochte markten : {len(symbols)}")
    print(f"Stake               : €{cfg['stake']:.2f}")
    print(f"Fee per kant        : {cfg['fee_pct']:.3f}%")
    print(f"Max hold            : {cfg['max_hold_minutes']/60:.1f} uur")
    print("SHORT = research-proxy; funding/borrow/liquidationkosten NIET gemodelleerd.")

    all_results: Dict[str, Dict[str, Any]] = {}

    for name in VARIANTS:
        accepted_rows = [r for r in signals if accepts(name, r)]
        trades: List[Dict[str, Any]] = []
        overlap = 0
        pending = 0
        last_exit_by_symbol: Dict[str, int] = {}

        for row in accepted_rows:
            symbol = str(row.get("symbol") or "")
            position = make_position(row, cfg)
            if position is None or symbol not in candles:
                continue
            if position["entry_ms"] < last_exit_by_symbol.get(symbol, -1):
                overlap += 1
                continue

            trade = evaluate(position, candles[symbol], cfg, now_ms)
            if trade is None:
                pending += 1
                last_exit_by_symbol[symbol] = now_ms + 1
                continue

            trades.append(trade)
            last_exit_by_symbol[symbol] = int(trade["exit_ms"])

        wins = sum(t["net_pnl"] > 0 for t in trades)
        losses = sum(t["net_pnl"] < 0 for t in trades)
        pnl = sum(t["net_pnl"] for t in trades)
        value = pf(trades)
        all_results[name] = {
            "accepted": len(accepted_rows),
            "closed": len(trades),
            "pending": pending,
            "overlap": overlap,
            "wins": wins,
            "losses": losses,
            "pnl": pnl,
            "pf": value,
            "avg": pnl / len(trades) if trades else 0.0,
            "fees": sum(t["fees"] for t in trades),
            "tp": sum(t["exit_reason"] == "take_profit" for t in trades),
            "sl": sum(t["exit_reason"] == "stop_loss" for t in trades),
            "time": sum(t["exit_reason"] == "time_exit" for t in trades),
            "trades": trades,
        }

    print("\n=== RESULTAAT ===")
    for name in VARIANTS:
        r = all_results[name]
        wr = (100.0 * r["wins"] / r["closed"]) if r["closed"] else 0.0
        print(
            f"{name:<24} accepted={r['accepted']:>3} closed={r['closed']:>3} pending={r['pending']:>3} overlap={r['overlap']:>3} | "
            f"W/L={r['wins']}/{r['losses']} WR={wr:5.1f}% | PnL=€{r['pnl']:+8.3f} PF={pf_text(r['pf']):>6} AVG=€{r['avg']:+.3f}"
        )
        print(f"{'':24} TP/SL/TIME={r['tp']}/{r['sl']}/{r['time']} | fees=€{r['fees']:.3f}")

    ranked = sorted(
        VARIANTS,
        key=lambda name: (
            all_results[name]["pf"] if all_results[name]["pf"] is not None and not math.isinf(all_results[name]["pf"]) else (999.0 if all_results[name]["pf"] == math.inf else -1.0),
            all_results[name]["pnl"],
        ),
        reverse=True,
    )

    print("\n=== RANGORDE OP PF ===")
    for i, name in enumerate(ranked, 1):
        r = all_results[name]
        print(f"{i}. {name:<24} closed={r['closed']:>3} PnL=€{r['pnl']:+.3f} PF={pf_text(r['pf'])}")

    print("\n=== LAATSTE 5 AFGERONDE TRADES PER VARIANT ===")
    for name in VARIANTS:
        print(f"\n{name}:")
        trades = all_results[name]["trades"][-5:]
        if not trades:
            print("  GEEN AFGERONDE TRADES")
            continue
        for t in trades:
            print(
                f"  {t['symbol']:<12} {t['side']:<5} {t['strategy']:<16} regime={t['regime']:<13} "
                f"spread={t['spread']:.3f}% RR={t['rr']:.3f} {t['exit_reason']:<11} PnL=€{t['net_pnl']:+.3f}"
            )

    if errors:
        print("\n=== DATAFOUTEN ===")
        for symbol, error in sorted(errors.items()):
            print(f"{symbol}: {error}")

    print("\n=== VEILIGHEID ===")
    print("Orders/private API : NEE")
    print("Config/strategie    : ONGEWIJZIGD")
    print("LIVE                : ONGEWIJZIGD")


def self_test() -> None:
    base = {
        "side": "SHORT",
        "strategy": "momentum",
        "market_regime": "BEARISH",
        "shadow_eligible": "True",
        "entry_price": "100",
        "take_profit": "95",
        "stop_loss": "103",
        "spread_pct": "0.08",
        "reward_risk": "1.5",
        "score": "90",
        "symbol": "TEST/EUR",
        "candle_timestamp": "2026-08-23T00:00:00+00:00",
    }
    assert accepts("SHORT_MOMENTUM", base)
    assert not accepts("SIDE_MEANREV_LONG", base)
    cfg = {"stake": 130.0, "fee_pct": 0.25, "max_hold_minutes": 120.0}
    pos = make_position(base, cfg)
    assert pos and pos["side"] == "SHORT" and pos["sl"] > pos["entry"] > pos["tp"]
    candles = [[int(datetime(2026, 8, 23, 0, 15, tzinfo=timezone.utc).timestamp()*1000), 100, 101, 94, 96, 1]]
    trade = evaluate(pos, candles, cfg, int(datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc).timestamp()*1000))
    assert trade and trade["exit_reason"] == "take_profit"
    print("DIAMOND_BEARISH_SIDEWAYS_RESEARCH_SELF_TEST_OK")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    run(max(1, args.days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
