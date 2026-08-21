#!/usr/bin/env python3
# Diamond Trader LONG Exact Entry/Exit Vergelijking v1.0 - ALLEEN-LEZEN
#
# Vergelijkt dezelfde 18 historische LONG-signalen:
#   CURRENT, WAIT_15M, WAIT_30M
#
# Exitlogica volgt diamond_bot.py:
# - initiële ATR-stop + harde stop
# - profit trailing vanaf ingestelde trigger
# - ATR trailing
# - trailing mag pas winstgebied in als min_profit_eur netto wordt veiliggesteld
# - vaste take-profit wordt niet gebruikt zodra profit trailing actief is
# - trend-break
# - stop/hard-stop altijd toegestaan
# - take-profit/trailing/trend-break alleen als netto >= min_profit_eur
#
# Historische bid/ask is niet beschikbaar in OHLCV.
# Daarom wordt per trade de geregistreerde spread uit BUY/SELL als proxy gebruikt.
# CURRENT gebruikt de echte geregistreerde BUY-fill.
# WAIT-varianten gebruiken candle-open + halve spread als gesimuleerde ask.
# Exits gebruiken candle-close - halve spread als gesimuleerde bid.
#
# Dit bestand wijzigt GEEN config, state, strategie of orders.

import csv
import math
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

import ccxt
import pandas as pd

from diamond_bot import load_yaml, get_cfg, enrich_indicators

CFG = load_yaml("config.yaml")
TRADES = Path(get_cfg(CFG, "files.trades_file", "/var/data/diamond_transactions.csv"))
OUT = Path("/var/data/diamond_long_exact_entry_exit.csv")

TF_MS = 15 * 60 * 1000
HORIZON_HOURS = 48

STAKE_CFG = float(get_cfg(CFG, "risk.fixed_stake_quote", 120))
FEE_PCT = float(get_cfg(CFG, "fees.taker_fee_pct", 0.25))
MIN_PROFIT = float(get_cfg(CFG, "risk.min_profit_eur", 1.00))

ATR_SL = float(get_cfg(CFG, "signals.atr_sl_mult", 1.2))
ATR_TP = float(get_cfg(CFG, "signals.atr_tp_mult", 2.6))
HARD_SL_PCT = float(get_cfg(CFG, "signals.hard_stop_loss_pct", 3.0))

TRAIL_ENABLED = bool(get_cfg(CFG, "signals.trailing_enabled", True))
TRAIL_ATR = float(get_cfg(CFG, "signals.trailing_atr_mult", 1.2))
PROFIT_TRIGGER_PCT = float(get_cfg(CFG, "signals.profit_trailing_trigger_pct", 1.0))
PROFIT_PULLBACK_PCT = float(get_cfg(CFG, "signals.profit_trailing_pullback_pct", 0.5))
EXIT_TREND = bool(get_cfg(CFG, "signals.exit_on_trend_break", True))

SMA_FAST = int(get_cfg(CFG, "signals.sma_fast", 20))
SMA_SLOW = int(get_cfg(CFG, "signals.sma_slow", 60))
RSI_LEN = int(get_cfg(CFG, "signals.rsi_len", 14))
ATR_LEN = int(get_cfg(CFG, "signals.atr_len", 14))


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


def load_longs():
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
        symbol = str(r.get("market", "")).upper()
        side = str(r.get("side", "")).upper()

        if side == "BUY":
            x = {"buy": r, "sell": None, "symbol": symbol}
            longs.append(x)
            open_buys[symbol].append(x)

        elif side == "SELL" and open_buys[symbol]:
            open_buys[symbol].popleft()["sell"] = r

    return longs


def fetch_frame(exchange, symbol, signal_ms):
    since = signal_ms - 30 * 60 * 60 * 1000
    end_ms = signal_ms + (HORIZON_HOURS + 4) * 60 * 60 * 1000

    rows = []
    cursor = since

    while cursor < end_ms:
        batch = exchange.fetch_ohlcv(symbol, "15m", since=cursor, limit=200) or []
        if not batch:
            break

        rows.extend(batch)
        last_ms = int(batch[-1][0])
        nxt = last_ms + TF_MS

        if nxt <= cursor:
            break

        cursor = nxt
        if last_ms >= end_ms:
            break

        time.sleep(0.05)

    if not rows:
        raise RuntimeError("geen candles")

    uniq = {int(r[0]): r for r in rows}
    data = [uniq[k] for k in sorted(uniq)]

    df = pd.DataFrame(
        data,
        columns=["ts", "open", "high", "low", "close", "volume"],
    )

    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna().sort_values("ts").reset_index(drop=True)

    return enrich_indicators(
        df,
        SMA_FAST,
        SMA_SLOW,
        RSI_LEN,
        ATR_LEN,
    )


def signal_context(frame, signal_ms):
    # De laatste volledig gesloten candle vóór de geregistreerde BUY.
    pre = frame[(frame["ts"] + TF_MS) <= signal_ms]
    if pre.empty:
        raise RuntimeError("geen gesloten signaalcandle")

    row = pre.iloc[-1]
    atr = num(row["atr"], math.nan)

    if not math.isfinite(atr) or atr <= 0:
        raise RuntimeError("ATR ontbreekt")

    return row, atr


def spread_proxy_pct(buy, sell):
    vals = []

    b = num(buy.get("spread_pct"), math.nan)
    if math.isfinite(b) and b >= 0:
        vals.append(b)

    if sell:
        s = num(sell.get("spread_pct"), math.nan)
        if math.isfinite(s) and s >= 0:
            vals.append(s)

    return sum(vals) / len(vals) if vals else 0.0


def delayed_entry(frame, signal_ms, minutes, spread_pct):
    target = signal_ms + minutes * 60 * 1000
    candidates = frame[frame["ts"] >= target]

    if candidates.empty:
        return None

    candle = candidates.iloc[0]
    mid = num(candle["open"])
    ask = mid * (1.0 + spread_pct / 200.0)

    return int(candle["ts"]), ask


def pnl_at_bid(amount, entry_quote, buy_fee, bid):
    gross = amount * max(bid, 0.0)
    sell_fee = gross * (FEE_PCT / 100.0)
    return gross - sell_fee - entry_quote - buy_fee


def min_profitable_price(amount, entry_quote, buy_fee):
    if amount <= 0:
        return float("inf")

    sell_mult = 1.0 - FEE_PCT / 100.0
    if sell_mult <= 0:
        return float("inf")

    required = entry_quote + buy_fee + max(0.0, MIN_PROFIT)
    return required / (amount * sell_mult)


def make_position(entry, atr, amount, quote_amount, buy_fee):
    atr_stop = entry - atr * ATR_SL
    hard_stop = entry * (1.0 - HARD_SL_PCT / 100.0)

    return {
        "entry": entry,
        "amount": amount,
        "quote_amount": quote_amount,
        "buy_fee": buy_fee,
        "stop": max(atr_stop, hard_stop),
        "take_profit": entry + atr * ATR_TP,
        "highest": entry,
    }


def reason_allowed(reason, est_pnl):
    if reason in {"stop_loss", "hard_stop_loss"}:
        return True

    return est_pnl + 1e-12 >= MIN_PROFIT


def simulate(frame, entry_ms, pos, spread_pct):
    horizon_end = entry_ms + HORIZON_HOURS * 60 * 60 * 1000

    # Een candle wordt pas beoordeeld zodra hij gesloten is.
    candles = frame[
        ((frame["ts"] + TF_MS) > entry_ms)
        & ((frame["ts"] + TF_MS) <= horizon_end)
    ]

    if candles.empty:
        return None

    min_exit = min_profitable_price(
        pos["amount"],
        pos["quote_amount"],
        pos["buy_fee"],
    )

    last_bid = pos["entry"]

    for _, c in candles.iterrows():
        close = num(c["close"])
        atr = num(c["atr"])
        fast = num(c["sma_fast"])
        slow = num(c["sma_slow"])

        if close <= 0:
            continue

        # Historische OHLCV heeft geen bid/ask.
        bid = close * (1.0 - spread_pct / 200.0)
        last_bid = bid

        pos["highest"] = max(pos["highest"], close)
        profit_pct = (
            (close - pos["entry"]) / pos["entry"] * 100.0
            if pos["entry"] > 0
            else 0.0
        )

        # Exacte volgorde uit long_exit_signal().
        if (
            profit_pct >= PROFIT_TRIGGER_PCT
            and pos["highest"] > 0
        ):
            tight = pos["highest"] * (
                1.0 - PROFIT_PULLBACK_PCT / 100.0
            )

            if tight >= min_exit and tight > pos["stop"]:
                pos["stop"] = tight

        if TRAIL_ENABLED and atr > 0 and pos["highest"] > 0:
            atr_trail = pos["highest"] - atr * TRAIL_ATR

            if atr_trail >= min_exit and atr_trail > pos["stop"]:
                pos["stop"] = atr_trail

        hard_stop = pos["entry"] * (
            1.0 - HARD_SL_PCT / 100.0
        )

        reason = None

        if close <= hard_stop:
            reason = "hard_stop_loss"

        elif pos["stop"] > 0 and close <= pos["stop"]:
            if pos["stop"] >= min_exit and pos["highest"] > pos["entry"]:
                reason = "trailing_stop"
            else:
                reason = "stop_loss"

        else:
            trailing_active = (
                pos["stop"] >= min_exit
                and math.isfinite(min_exit)
            )

            if (
                pos["take_profit"] > 0
                and close >= pos["take_profit"]
                and not trailing_active
            ):
                reason = "take_profit"

            elif EXIT_TREND and fast < slow:
                reason = "trend_break"

        if reason:
            est_pnl = pnl_at_bid(
                pos["amount"],
                pos["quote_amount"],
                pos["buy_fee"],
                bid,
            )

            if reason_allowed(reason, est_pnl):
                exit_ms = int(c["ts"]) + TF_MS

                return {
                    "closed": True,
                    "exit_ms": exit_ms,
                    "exit_price": bid,
                    "reason": reason,
                    "net_pnl": est_pnl,
                    "holding_min": (exit_ms - entry_ms) / 60000.0,
                    "final_stop": pos["stop"],
                    "min_profitable_price": min_exit,
                }

    unrealized = pnl_at_bid(
        pos["amount"],
        pos["quote_amount"],
        pos["buy_fee"],
        last_bid,
    )

    return {
        "closed": False,
        "exit_ms": None,
        "exit_price": last_bid,
        "reason": f"open_na_{HORIZON_HOURS}h",
        "net_pnl": unrealized,
        "holding_min": HORIZON_HOURS * 60.0,
        "final_stop": pos["stop"],
        "min_profitable_price": min_exit,
    }


def actual_pnl(x):
    s = x.get("sell")
    if not s:
        return math.nan
    return num(s.get("net_pnl_quote"), math.nan)


longs = load_longs()

print(f"LONG-signalen gevonden: {len(longs)} (verwacht 18)")
print("Varianten: CURRENT, WAIT_15M, WAIT_30M")
print(
    f"Exit: SL={ATR_SL:.2f}ATR TP={ATR_TP:.2f}ATR "
    f"hard={HARD_SL_PCT:.2f}% trail={TRAIL_ATR:.2f}ATR"
)
print(
    f"Profit trail: trigger={PROFIT_TRIGGER_PCT:.2f}% "
    f"pullback={PROFIT_PULLBACK_PCT:.2f}% min_netto=€{MIN_PROFIT:.2f}"
)
print(f"Fee: {FEE_PCT:.3f}% per zijde | horizon={HORIZON_HOURS}h")
print("Spread: historische BUY/SELL-spread per trade als proxy.")
print("Geen orders, config- of statewijzigingen.\n")

exchange = ccxt.bitvavo({
    "enableRateLimit": True,
    "options": {"fetchMarkets": {"types": ["spot"]}},
})
exchange.load_markets()

results = []

for i, x in enumerate(longs, 1):
    buy = x["buy"]
    sell = x["sell"]
    symbol = x["symbol"]

    signal_ms = int(buy["_dt"].timestamp() * 1000)
    real_entry = num(buy.get("price"))
    real_amount = num(buy.get("base_amount"))
    real_quote = num(buy.get("quote_amount"))
    real_buy_fee = num(buy.get("fees_quote"))

    if real_entry <= 0:
        print(f"{i:02d} {symbol:7s} FOUT: ongeldige BUY-prijs")
        continue

    spread = spread_proxy_pct(buy, sell)

    try:
        frame = fetch_frame(exchange, symbol, signal_ms)
        _, signal_atr = signal_context(frame, signal_ms)

        variants = []

        # CURRENT: echte historische BUY-fill en geregistreerde koopkosten.
        if real_amount <= 0:
            real_amount = real_quote / real_entry if real_quote > 0 else STAKE_CFG / real_entry
        if real_quote <= 0:
            real_quote = real_amount * real_entry
        if real_buy_fee <= 0:
            real_buy_fee = real_quote * FEE_PCT / 100.0

        variants.append(
            (
                "CURRENT",
                signal_ms,
                real_entry,
                real_amount,
                real_quote,
                real_buy_fee,
            )
        )

        # WAIT: zelfde trade-inzet; alleen entry-timing verandert.
        stake = real_quote if real_quote > 0 else STAKE_CFG

        for name, mins in [("WAIT_15M", 15), ("WAIT_30M", 30)]:
            de = delayed_entry(frame, signal_ms, mins, spread)
            if not de:
                continue

            entry_ms, entry = de
            amount = stake / entry
            quote_amount = amount * entry
            buy_fee = quote_amount * FEE_PCT / 100.0

            variants.append(
                (
                    name,
                    entry_ms,
                    entry,
                    amount,
                    quote_amount,
                    buy_fee,
                )
            )

        done = 0

        for name, entry_ms, entry, amount, quote_amount, buy_fee in variants:
            # Om uitsluitend entry-timing te testen wordt dezelfde
            # ATR-risicoafstand opnieuw vanaf de nieuwe fill geankerd.
            pos = make_position(
                entry,
                signal_atr,
                amount,
                quote_amount,
                buy_fee,
            )

            sim = simulate(
                frame,
                entry_ms,
                pos,
                spread,
            )

            if sim is None:
                continue

            done += 1

            results.append({
                "trade": i,
                "symbol": symbol,
                "signal_utc": buy["_dt"].isoformat(),
                "variant": name,
                "entry_ms": entry_ms,
                "entry_price": entry,
                "signal_atr": signal_atr,
                "spread_proxy_pct": spread,
                "quote_amount": quote_amount,
                "buy_fee": buy_fee,
                "actual_pnl": actual_pnl(x),
                **sim,
            })

        print(
            f"{i:02d} {symbol:7s} ATR={signal_atr/real_entry*100:5.3f}% "
            f"spread={spread:5.3f}% varianten={done}/3"
        )

    except Exception as exc:
        print(f"{i:02d} {symbol:7s} FOUT: {exc}")

    time.sleep(0.10)


if not results:
    raise SystemExit("Geen resultaten.")

with OUT.open("w", encoding="utf-8", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
    w.writeheader()
    w.writerows(results)


def group(name):
    return [r for r in results if r["variant"] == name]


print("\n===== EXACTE ENTRY/EXIT SAMENVATTING =====")

actual_vals = [
    actual_pnl(x)
    for x in longs
    if math.isfinite(actual_pnl(x))
]

if actual_vals:
    print(
        f"WERKELIJK          trades={len(actual_vals):2d} "
        f"wins={sum(v > 0 for v in actual_vals):2d} "
        f"losses={sum(v <= 0 for v in actual_vals):2d} "
        f"netto=€{sum(actual_vals):+8.4f}"
    )

summary = {}

for name in ["CURRENT", "WAIT_15M", "WAIT_30M"]:
    g = group(name)
    if not g:
        continue

    closed = [r for r in g if r["closed"]]
    open_rows = [r for r in g if not r["closed"]]
    pnls = [num(r["net_pnl"]) for r in closed]

    reasons = defaultdict(int)
    for r in closed:
        reasons[r["reason"]] += 1

    total = sum(pnls)
    wins = sum(v > 0 for v in pnls)
    losses = sum(v <= 0 for v in pnls)

    summary[name] = {
        "filled": len(g),
        "closed": len(closed),
        "open": len(open_rows),
        "net": total,
        "wins": wins,
        "losses": losses,
        "reasons": reasons,
    }

    print(
        f"{name:18s} filled={len(g):2d}/18 closed={len(closed):2d} "
        f"wins={wins:2d} losses={losses:2d} open={len(open_rows):2d} "
        f"netto=€{total:+8.4f} "
        f"SL={reasons['stop_loss'] + reasons['hard_stop_loss']:2d} "
        f"TRAIL={reasons['trailing_stop']:2d} "
        f"TP={reasons['take_profit']:2d} "
        f"TREND={reasons['trend_break']:2d}"
    )


print("\n===== VERSCHIL T.O.V. CURRENT =====")

base = summary.get("CURRENT")

if base:
    for name in ["WAIT_15M", "WAIT_30M"]:
        s = summary.get(name)
        if not s:
            continue

        print(
            f"{name:18s} delta_netto=€{s['net'] - base['net']:+.4f} "
            f"delta_wins={s['wins'] - base['wins']:+d} "
            f"delta_losses={s['losses'] - base['losses']:+d}"
        )


print("\n===== PER TRADE =====")

for i in range(1, len(longs) + 1):
    rows = [r for r in results if int(r["trade"]) == i]
    if not rows:
        continue

    symbol = rows[0]["symbol"]
    parts = []

    for name in ["CURRENT", "WAIT_15M", "WAIT_30M"]:
        r = next((z for z in rows if z["variant"] == name), None)
        if not r:
            parts.append(f"{name}=GEEN")
            continue

        status = r["reason"]
        parts.append(
            f"{name}=€{num(r['net_pnl']):+.2f}/{status}"
        )

    print(f"{i:02d} {symbol:7s} " + " | ".join(parts))


print("\n===== INTERPRETATIE =====")

if base:
    candidates = [
        summary[n]
        for n in ["WAIT_15M", "WAIT_30M"]
        if n in summary
        and summary[n]["closed"] == base["closed"]
    ]

    if candidates:
        best_name = max(
            ["WAIT_15M", "WAIT_30M"],
            key=lambda n: summary.get(n, {"net": -1e99})["net"],
        )

        best = summary[best_name]
        delta = best["net"] - base["net"]

        print(
            f"Beste wachtvariant op gesloten netto PnL: {best_name} "
            f"(€{best['net']:+.4f}, verschil €{delta:+.4f} t.o.v. CURRENT)."
        )

        if delta > 0:
            print(
                "Als dit voordeel ook per trade redelijk breed verdeeld is, "
                "is de volgende stap een aparte shadow/dry-run entryvariant."
            )
        else:
            print(
                "Geen wachtvariant verbetert de CURRENT netto PnL in deze test; "
                "dan wijzigen we de entry niet."
            )

print(
    "\nLET OP: exitlogica/fees zijn volgens de huidige botregels; "
    "historische bid/ask is benaderd met de geregistreerde spread."
)
print(f"Details: {OUT}")
print("Geen config/state/strategie gewijzigd.")
