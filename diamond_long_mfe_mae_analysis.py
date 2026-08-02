#!/usr/bin/env python3
# Diamond Trader LONG Entry Timing Analyse v1.0 - ALLEEN-LEZEN
# Doel: dezelfde historische LONG-signalen vergelijken met alternatieve entries.
# Wijzigt GEEN config, state, strategie of orders.

import csv
import math
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

import ccxt
import pandas as pd

from diamond_bot import load_yaml, get_cfg, enrich_indicators

C = load_yaml("config.yaml")
TRADES = Path(get_cfg(C, "files.trades_file", "/var/data/diamond_transactions.csv"))
OUT = Path("/var/data/diamond_long_entry_timing.csv")
TF_MS = 15 * 60 * 1000

ATR_SL = float(get_cfg(C, "signals.atr_sl_mult", 1.2))
ATR_TP = float(get_cfg(C, "signals.atr_tp_mult", 2.6))
SMA_FAST = int(get_cfg(C, "signals.sma_fast", 20))
SMA_SLOW = int(get_cfg(C, "signals.sma_slow", 60))
RSI_LEN = int(get_cfg(C, "signals.rsi_len", 14))
ATR_LEN = int(get_cfg(C, "signals.atr_len", 14))


def num(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def utc_dt(v):
    x = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if x.tzinfo is None:
        x = x.replace(tzinfo=timezone.utc)
    return x.astimezone(timezone.utc)


def load_long_entries():
    rows = []
    with TRADES.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            try:
                r["_dt"] = utc_dt(r["ts"])
                rows.append(r)
            except Exception:
                pass

    rows.sort(key=lambda r: r["_dt"])
    open_buys = defaultdict(deque)
    longs = []

    for r in rows:
        symbol = r.get("market", "").upper()
        side = r.get("side", "").upper()
        if side == "BUY":
            x = {"buy": r, "sell": None, "symbol": symbol}
            longs.append(x)
            open_buys[symbol].append(x)
        elif side == "SELL" and open_buys[symbol]:
            open_buys[symbol].popleft()["sell"] = r

    return longs


def fetch_data(exchange, symbol, signal_ms):
    # Genoeg historie voor indicatoren + 24 uur na het signaal.
    since = signal_ms - 24 * 60 * 60 * 1000
    all_rows = []
    cursor = since
    end_ms = signal_ms + 26 * 60 * 60 * 1000

    while cursor < end_ms:
        batch = exchange.fetch_ohlcv(symbol, "15m", since=cursor, limit=200) or []
        if not batch:
            break
        all_rows.extend(batch)
        last = int(batch[-1][0])
        nxt = last + TF_MS
        if nxt <= cursor:
            break
        cursor = nxt
        if last >= end_ms:
            break
        time.sleep(0.05)

    if not all_rows:
        raise RuntimeError("geen candles")

    # Duplicaten verwijderen bij eventuele overlap tussen API-batches.
    uniq = {int(r[0]): r for r in all_rows}
    data = [uniq[k] for k in sorted(uniq)]
    f = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "volume"])
    for c in ["open", "high", "low", "close", "volume"]:
        f[c] = pd.to_numeric(f[c], errors="coerce")
    f = f.dropna().sort_values("ts").reset_index(drop=True)

    return enrich_indicators(f, SMA_FAST, SMA_SLOW, RSI_LEN, ATR_LEN)


def signal_context(frame, signal_ms):
    # Laatste volledig gesloten candle vóór de BUY-timestamp.
    pre = frame[(frame.ts + TF_MS) <= signal_ms]
    if pre.empty:
        raise RuntimeError("geen gesloten candle vóór entry")
    idx = pre.index[-1]
    row = frame.loc[idx]
    atr = num(row.atr, math.nan)
    if not math.isfinite(atr) or atr <= 0:
        raise RuntimeError("ATR ontbreekt")
    return idx, row, atr


def first_post_candle(frame, signal_ms):
    start = ((signal_ms + TF_MS - 1) // TF_MS) * TF_MS
    post = frame[frame.ts >= start]
    if post.empty:
        return None
    return post.iloc[0]


def make_current_variant(frame, signal_ms, original_entry, atr, sma20):
    return {
        "variant": "CURRENT",
        "entry_ms": signal_ms,
        "entry": original_entry,
        "atr": atr,
        "entry_improvement_atr": 0.0,
        "entry_improvement_pct": 0.0,
        "wait_min": 0,
    }


def make_delay_variant(frame, signal_ms, original_entry, atr, minutes):
    target_ms = signal_ms + minutes * 60 * 1000
    candidates = frame[frame.ts >= target_ms]
    if candidates.empty:
        return None
    c = candidates.iloc[0]
    entry = num(c.open)
    return {
        "variant": f"WAIT_{minutes}M",
        "entry_ms": int(c.ts),
        "entry": entry,
        "atr": atr,
        "entry_improvement_atr": (original_entry - entry) / atr,
        "entry_improvement_pct": (original_entry / entry - 1) * 100 if entry else 0,
        "wait_min": max(0.0, (int(c.ts) - signal_ms) / 60000),
    }


def make_pullback_variant(frame, signal_ms, original_entry, atr, pullback_atr, max_wait_min=240):
    target = original_entry - pullback_atr * atr
    start = ((signal_ms + TF_MS - 1) // TF_MS) * TF_MS
    end = signal_ms + max_wait_min * 60 * 1000
    candidates = frame[(frame.ts >= start) & (frame.ts <= end) & (frame.low <= target)]
    if candidates.empty:
        return None
    c = candidates.iloc[0]
    return {
        "variant": f"PULLBACK_{pullback_atr:.2f}ATR",
        "entry_ms": int(c.ts),
        "entry": target,
        "atr": atr,
        "entry_improvement_atr": pullback_atr,
        "entry_improvement_pct": (original_entry / target - 1) * 100 if target else 0,
        "wait_min": max(0.0, (int(c.ts) - signal_ms) / 60000),
    }


def make_sma20_variant(frame, signal_ms, original_entry, atr, sma20, max_wait_min=240):
    if not math.isfinite(sma20) or sma20 <= 0 or sma20 >= original_entry:
        return None
    start = ((signal_ms + TF_MS - 1) // TF_MS) * TF_MS
    end = signal_ms + max_wait_min * 60 * 1000
    candidates = frame[(frame.ts >= start) & (frame.ts <= end) & (frame.low <= sma20)]
    if candidates.empty:
        return None
    c = candidates.iloc[0]
    return {
        "variant": "PULLBACK_SMA20",
        "entry_ms": int(c.ts),
        "entry": sma20,
        "atr": atr,
        "entry_improvement_atr": (original_entry - sma20) / atr,
        "entry_improvement_pct": (original_entry / sma20 - 1) * 100 if sma20 else 0,
        "wait_min": max(0.0, (int(c.ts) - signal_ms) / 60000),
    }


def evaluate(frame, entry_ms, entry, atr):
    # Vanaf de entry-candle tot maximaal 24 uur erna.
    post = frame[(frame.ts >= entry_ms) & (frame.ts < entry_ms + 24 * 60 * 60 * 1000)].copy()
    if post.empty:
        return None

    sl = entry - ATR_SL * atr
    plus1 = entry + 1.0 * atr
    tp = entry + ATR_TP * atr

    # Conservatief: als high en low in dezelfde candle beide niveaus raken,
    # tellen we de stop als eerste omdat intrabar-volgorde onbekend is.
    outcome_1atr = "NONE"
    outcome_tp = "NONE"
    t_1atr = None
    t_tp = None

    for _, c in post.iterrows():
        low = num(c.low)
        high = num(c.high)
        ts = int(c.ts)

        if outcome_1atr == "NONE":
            if low <= sl:
                outcome_1atr = "SL"
                t_1atr = ts
            elif high >= plus1:
                outcome_1atr = "+1ATR"
                t_1atr = ts

        if outcome_tp == "NONE":
            if low <= sl:
                outcome_tp = "SL"
                t_tp = ts
            elif high >= tp:
                outcome_tp = "TP"
                t_tp = ts

        if outcome_1atr != "NONE" and outcome_tp != "NONE":
            break

    w4 = post[post.ts < entry_ms + 240 * 60 * 1000]
    if w4.empty:
        mfe4 = mae4 = 0.0
    else:
        mfe4 = max(0.0, (num(w4.high.max()) - entry) / atr)
        mae4 = max(0.0, (entry - num(w4.low.min())) / atr)

    return {
        "sl": sl,
        "plus1": plus1,
        "tp": tp,
        "outcome_1atr": outcome_1atr,
        "outcome_tp": outcome_tp,
        "mfe4h_atr": mfe4,
        "mae4h_atr": mae4,
        "time_to_1atr_min": ((t_1atr - entry_ms) / 60000) if outcome_1atr == "+1ATR" and t_1atr is not None else None,
        "time_to_tp_min": ((t_tp - entry_ms) / 60000) if outcome_tp == "TP" and t_tp is not None else None,
    }


def avg(rows, key):
    vals = [num(r.get(key), math.nan) for r in rows]
    vals = [v for v in vals if math.isfinite(v)]
    return sum(vals) / len(vals) if vals else 0.0


longs = load_long_entries()
print(f"LONG-signalen gevonden: {len(longs)} (verwacht 18)")
print(f"Analyse gebruikt huidige ATR SL={ATR_SL:.2f} en TP={ATR_TP:.2f} ATR")
print("Geen orders, config- of statewijzigingen.\n")

exchange = ccxt.bitvavo({
    "enableRateLimit": True,
    "options": {"fetchMarkets": {"types": ["spot"]}},
})
exchange.load_markets()

results = []

for i, x in enumerate(longs, 1):
    b = x["buy"]
    symbol = x["symbol"]
    signal_ms = int(b["_dt"].timestamp() * 1000)
    original_entry = num(b.get("price"))

    try:
        frame = fetch_data(exchange, symbol, signal_ms)
        _, sig, atr = signal_context(frame, signal_ms)
        sma20 = num(sig.sma_fast, math.nan)

        variants = [
            make_current_variant(frame, signal_ms, original_entry, atr, sma20),
            make_delay_variant(frame, signal_ms, original_entry, atr, 15),
            make_delay_variant(frame, signal_ms, original_entry, atr, 30),
            make_delay_variant(frame, signal_ms, original_entry, atr, 60),
            make_pullback_variant(frame, signal_ms, original_entry, atr, 0.25),
            make_pullback_variant(frame, signal_ms, original_entry, atr, 0.50),
            make_pullback_variant(frame, signal_ms, original_entry, atr, 0.75),
            make_pullback_variant(frame, signal_ms, original_entry, atr, 1.00),
            make_sma20_variant(frame, signal_ms, original_entry, atr, sma20),
        ]

        filled = 0
        for v in variants:
            if v is None:
                continue
            ev = evaluate(frame, int(v["entry_ms"]), num(v["entry"]), atr)
            if ev is None:
                continue
            filled += 1
            row = {
                "trade": i,
                "symbol": symbol,
                "signal_utc": b["_dt"].isoformat(),
                "original_entry": original_entry,
                "atr": atr,
                "sma20": sma20,
                **v,
                **ev,
            }
            results.append(row)

        print(f"{i:02d} {symbol:7s} ATR={atr/original_entry*100:5.3f}% varianten gevuld={filled}/9")

    except Exception as exc:
        print(f"{i:02d} {symbol:7s} FOUT: {exc}")

    time.sleep(0.10)

if not results:
    raise SystemExit("Geen resultaten")

with OUT.open("w", encoding="utf-8", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
    w.writeheader()
    w.writerows(results)

print("\n===== ENTRY-TIMING SAMENVATTING =====")
order = [
    "CURRENT",
    "WAIT_15M",
    "WAIT_30M",
    "WAIT_60M",
    "PULLBACK_0.25ATR",
    "PULLBACK_0.50ATR",
    "PULLBACK_0.75ATR",
    "PULLBACK_1.00ATR",
    "PULLBACK_SMA20",
]

summary = []
for name in order:
    g = [r for r in results if r["variant"] == name]
    if not g:
        continue
    filled = len(g)
    one_wins = sum(r["outcome_1atr"] == "+1ATR" for r in g)
    one_sl = sum(r["outcome_1atr"] == "SL" for r in g)
    tp_wins = sum(r["outcome_tp"] == "TP" for r in g)
    tp_sl = sum(r["outcome_tp"] == "SL" for r in g)
    none1 = filled - one_wins - one_sl
    none_tp = filled - tp_wins - tp_sl
    row = {
        "variant": name,
        "filled": filled,
        "one_wins": one_wins,
        "one_sl": one_sl,
        "tp_wins": tp_wins,
        "tp_sl": tp_sl,
        "mfe": avg(g, "mfe4h_atr"),
        "mae": avg(g, "mae4h_atr"),
        "improve": avg(g, "entry_improvement_atr"),
        "wait": avg(g, "wait_min"),
        "none1": none1,
        "none_tp": none_tp,
    }
    summary.append(row)

    print(
        f"{name:18s} filled={filled:2d}/18  "
        f"+1ATR vóór SL={one_wins:2d}  SL eerst={one_sl:2d}  "
        f"TP vóór SL={tp_wins:2d}  "
        f"MFE4h={row['mfe']:.2f}ATR MAE4h={row['mae']:.2f}ATR  "
        f"prijsverbetering={row['improve']:+.2f}ATR"
    )

print("\n===== INTERPRETATIEHULP =====")
current = next((x for x in summary if x["variant"] == "CURRENT"), None)
if current:
    print(f"Baseline CURRENT: +1ATR vóór SL {current['one_wins']}/{current['filled']}, TP vóór SL {current['tp_wins']}/{current['filled']}.")

eligible = [x for x in summary if x["variant"] != "CURRENT" and x["filled"] >= 9]
if eligible:
    # Eerst +1ATR-before-SL, daarna TP-before-SL, daarna lagere MAE.
    best = sorted(eligible, key=lambda x: (x["one_wins"], x["tp_wins"], -x["mae"]), reverse=True)[0]
    print(
        f"Beste kandidaat op ruwe entrykwaliteit met minimaal 9 fills: {best['variant']} "
        f"(+1ATR vóór SL {best['one_wins']}/{best['filled']}, TP vóór SL {best['tp_wins']}/{best['filled']})."
    )
    print("Dit is GEEN advies om hem al in de bot te zetten; eerst beoordelen en daarna eventueel shadow/dry-run testen.")

print(f"\nDetails: {OUT}")
print("Geen config/state/strategie gewijzigd."
