#!/usr/bin/env python3
"""
Diamond Trader historische combinatie-test v1.0

Vergelijkt dezelfde 18 historische LONG-signalen met 6 varianten:

CURRENT_100  = huidige entry + min netto €1.00
CURRENT_050  = huidige entry + min netto €0.50
WAIT15_100   = 15m wachten + min netto €1.00
WAIT15_050   = 15m wachten + min netto €0.50
WAIT30_100   = 30m wachten + min netto €1.00
WAIT30_050   = 30m wachten + min netto €0.50

Alle overige LONG-regels blijven gelijk:
- zelfde historische LONG-signalen
- zelfde ATR uit signaalcandle
- ATR stop-loss
- harde stop-loss
- ATR take-profit
- profit trailing
- ATR trailing
- trend-break
- taker fee
- geregistreerde spread als historische proxy
- horizon 48 uur

Veiligheid:
- GEEN orders
- GEEN private API
- GEEN configwijziging
- GEEN bot-statewijziging
- schrijft alleen analyse-CSV naar /var/data
"""

from __future__ import annotations

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

TRADES = Path(
    get_cfg(
        CFG,
        "files.trades_file",
        "/var/data/diamond_transactions.csv",
    )
)

OUT = Path(
    "/var/data/diamond_long_entry_minprofit_combo.csv"
)

TF_MS = 15 * 60 * 1000
HORIZON_HOURS = 48

FEE_PCT = float(
    get_cfg(CFG, "fees.taker_fee_pct", 0.25)
)

ATR_SL = float(
    get_cfg(CFG, "signals.atr_sl_mult", 1.2)
)

ATR_TP = float(
    get_cfg(CFG, "signals.atr_tp_mult", 2.6)
)

HARD_SL_PCT = float(
    get_cfg(CFG, "signals.hard_stop_loss_pct", 3.0)
)

TRAIL_ENABLED = bool(
    get_cfg(CFG, "signals.trailing_enabled", True)
)

TRAIL_ATR = float(
    get_cfg(CFG, "signals.trailing_atr_mult", 1.2)
)

PROFIT_TRIGGER_PCT = float(
    get_cfg(CFG, "signals.profit_trailing_trigger_pct", 1.0)
)

PROFIT_PULLBACK_PCT = float(
    get_cfg(CFG, "signals.profit_trailing_pullback_pct", 0.5)
)

EXIT_TREND = bool(
    get_cfg(CFG, "signals.exit_on_trend_break", True)
)

SMA_FAST = int(
    get_cfg(CFG, "signals.sma_fast", 20)
)

SMA_SLOW = int(
    get_cfg(CFG, "signals.sma_slow", 60)
)

RSI_LEN = int(
    get_cfg(CFG, "signals.rsi_len", 14)
)

ATR_LEN = int(
    get_cfg(CFG, "signals.atr_len", 14)
)

VARIANTS = {
    "CURRENT_100": {"wait_min": 0, "min_profit": 1.00},
    "CURRENT_050": {"wait_min": 0, "min_profit": 0.50},
    "WAIT15_100":  {"wait_min": 15, "min_profit": 1.00},
    "WAIT15_050":  {"wait_min": 15, "min_profit": 0.50},
    "WAIT30_100":  {"wait_min": 30, "min_profit": 1.00},
    "WAIT30_050":  {"wait_min": 30, "min_profit": 0.50},
}


def num(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def utc_dt(value):
    dt = datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_longs():
    rows = []

    with TRADES.open(
        encoding="utf-8",
        newline="",
    ) as handle:
        for row in csv.DictReader(handle):
            try:
                row["_dt"] = utc_dt(row["ts"])
                rows.append(row)
            except Exception:
                pass

    rows.sort(key=lambda row: row["_dt"])

    open_buys = defaultdict(deque)
    longs = []

    for row in rows:
        symbol = str(row.get("market", "")).upper()
        side = str(row.get("side", "")).upper()

        if side == "BUY":
            item = {
                "buy": row,
                "sell": None,
                "symbol": symbol,
            }
            longs.append(item)
            open_buys[symbol].append(item)

        elif side == "SELL" and open_buys[symbol]:
            open_buys[symbol].popleft()["sell"] = row

    return longs


def fetch_frame(exchange, symbol, signal_ms):
    since = signal_ms - 30 * 60 * 60 * 1000
    end_ms = (
        signal_ms
        + (HORIZON_HOURS + 4) * 60 * 60 * 1000
    )

    rows = []
    cursor = since

    while cursor < end_ms:
        batch = exchange.fetch_ohlcv(
            symbol,
            "15m",
            since=cursor,
            limit=200,
        ) or []

        if not batch:
            break

        rows.extend(batch)

        last_ms = int(batch[-1][0])
        next_cursor = last_ms + TF_MS

        if next_cursor <= cursor:
            break

        cursor = next_cursor

        if last_ms >= end_ms:
            break

        time.sleep(0.05)

    if not rows:
        raise RuntimeError("geen candles")

    unique = {
        int(row[0]): row
        for row in rows
    }

    data = [
        unique[key]
        for key in sorted(unique)
    ]

    frame = pd.DataFrame(
        data,
        columns=[
            "ts",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ],
    )

    for column in [
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]:
        frame[column] = pd.to_numeric(
            frame[column],
            errors="coerce",
        )

    frame = (
        frame
        .dropna()
        .sort_values("ts")
        .reset_index(drop=True)
    )

    return enrich_indicators(
        frame,
        SMA_FAST,
        SMA_SLOW,
        RSI_LEN,
        ATR_LEN,
    )


def signal_atr(frame, signal_ms):
    pre = frame[
        (frame["ts"] + TF_MS) <= signal_ms
    ]

    if pre.empty:
        raise RuntimeError(
            "geen gesloten signaalcandle"
        )

    row = pre.iloc[-1]

    atr = num(
        row["atr"],
        math.nan,
    )

    if (
        not math.isfinite(atr)
        or atr <= 0
    ):
        raise RuntimeError(
            "ATR ontbreekt"
        )

    return atr


def spread_proxy_pct(buy, sell):
    values = []

    buy_spread = num(
        buy.get("spread_pct"),
        math.nan,
    )

    if (
        math.isfinite(buy_spread)
        and buy_spread >= 0
    ):
        values.append(buy_spread)

    if sell:
        sell_spread = num(
            sell.get("spread_pct"),
            math.nan,
        )

        if (
            math.isfinite(sell_spread)
            and sell_spread >= 0
        ):
            values.append(sell_spread)

    return (
        sum(values) / len(values)
        if values
        else 0.0
    )


def delayed_entry(
    frame,
    signal_ms,
    wait_minutes,
    spread_pct,
):
    target_ms = (
        signal_ms
        + wait_minutes * 60 * 1000
    )

    candidates = frame[
        frame["ts"] >= target_ms
    ]

    if candidates.empty:
        return None

    candle = candidates.iloc[0]

    mid_open = num(
        candle["open"],
        0.0,
    )

    if mid_open <= 0:
        return None

    ask = (
        mid_open
        * (
            1.0
            + spread_pct / 200.0
        )
    )

    return (
        int(candle["ts"]),
        ask,
    )


def minimum_profitable_price(
    amount,
    entry_quote,
    buy_fee,
    min_profit_eur,
):
    if amount <= 0:
        return float("inf")

    sell_multiplier = (
        1.0
        - FEE_PCT / 100.0
    )

    if sell_multiplier <= 0:
        return float("inf")

    required = (
        entry_quote
        + buy_fee
        + max(0.0, min_profit_eur)
    )

    return (
        required
        / (
            amount
            * sell_multiplier
        )
    )


def pnl_at_bid(
    amount,
    entry_quote,
    buy_fee,
    bid,
):
    gross = (
        amount
        * max(bid, 0.0)
    )

    sell_fee = (
        gross
        * FEE_PCT
        / 100.0
    )

    return (
        gross
        - sell_fee
        - entry_quote
        - buy_fee
    )


def make_position(
    entry,
    atr,
    amount,
    quote_amount,
    buy_fee,
):
    atr_stop = (
        entry
        - atr * ATR_SL
    )

    hard_stop = (
        entry
        * (
            1.0
            - HARD_SL_PCT / 100.0
        )
    )

    return {
        "entry": entry,
        "amount": amount,
        "quote_amount": quote_amount,
        "buy_fee": buy_fee,
        "stop": max(
            atr_stop,
            hard_stop,
        ),
        "take_profit": (
            entry
            + atr * ATR_TP
        ),
        "highest": entry,
    }


def reason_allowed(
    reason,
    estimated_pnl,
    min_profit_eur,
):
    if reason in {
        "stop_loss",
        "hard_stop_loss",
    }:
        return True

    return (
        estimated_pnl
        + 1e-12
        >= min_profit_eur
    )


def simulate(
    frame,
    entry_ms,
    position,
    spread_pct,
    min_profit_eur,
):
    horizon_end = (
        entry_ms
        + HORIZON_HOURS
        * 60 * 60 * 1000
    )

    candles = frame[
        (
            (frame["ts"] + TF_MS)
            > entry_ms
        )
        & (
            (frame["ts"] + TF_MS)
            <= horizon_end
        )
    ]

    if candles.empty:
        return None

    min_exit = minimum_profitable_price(
        position["amount"],
        position["quote_amount"],
        position["buy_fee"],
        min_profit_eur,
    )

    last_bid = position["entry"]

    for _, candle in candles.iterrows():
        close = num(
            candle["close"],
            0.0,
        )

        atr = num(
            candle["atr"],
            0.0,
        )

        fast = num(
            candle["sma_fast"],
            0.0,
        )

        slow = num(
            candle["sma_slow"],
            0.0,
        )

        if close <= 0:
            continue

        bid = (
            close
            * (
                1.0
                - spread_pct / 200.0
            )
        )

        last_bid = bid

        position["highest"] = max(
            position["highest"],
            close,
        )

        profit_pct = (
            (
                close
                - position["entry"]
            )
            / position["entry"]
            * 100.0
            if position["entry"] > 0
            else 0.0
        )

        if (
            profit_pct
            >= PROFIT_TRIGGER_PCT
            and position["highest"] > 0
        ):
            tight = (
                position["highest"]
                * (
                    1.0
                    - PROFIT_PULLBACK_PCT
                    / 100.0
                )
            )

            if (
                tight >= min_exit
                and tight > position["stop"]
            ):
                position["stop"] = tight

        if (
            TRAIL_ENABLED
            and atr > 0
            and position["highest"] > 0
        ):
            atr_trail = (
                position["highest"]
                - atr * TRAIL_ATR
            )

            if (
                atr_trail >= min_exit
                and atr_trail > position["stop"]
            ):
                position["stop"] = atr_trail

        hard_stop = (
            position["entry"]
            * (
                1.0
                - HARD_SL_PCT / 100.0
            )
        )

        reason = None

        if close <= hard_stop:
            reason = "hard_stop_loss"

        elif (
            position["stop"] > 0
            and close <= position["stop"]
        ):
            if (
                position["stop"] >= min_exit
                and position["highest"]
                > position["entry"]
            ):
                reason = "trailing_stop"
            else:
                reason = "stop_loss"

        else:
            trailing_active = (
                position["stop"] >= min_exit
                and math.isfinite(min_exit)
            )

            if (
                position["take_profit"] > 0
                and close >= position["take_profit"]
                and not trailing_active
            ):
                reason = "take_profit"

            elif (
                EXIT_TREND
                and fast < slow
            ):
                reason = "trend_break"

        if reason:
            estimated_pnl = pnl_at_bid(
                position["amount"],
                position["quote_amount"],
                position["buy_fee"],
                bid,
            )

            if reason_allowed(
                reason,
                estimated_pnl,
                min_profit_eur,
            ):
                exit_ms = (
                    int(candle["ts"])
                    + TF_MS
                )

                return {
                    "closed": True,
                    "exit_ms": exit_ms,
                    "exit_price": bid,
                    "reason": reason,
                    "net_pnl": estimated_pnl,
                    "holding_min": (
                        exit_ms
                        - entry_ms
                    ) / 60000.0,
                    "final_stop": position["stop"],
                    "min_profitable_price": min_exit,
                }

    unrealized = pnl_at_bid(
        position["amount"],
        position["quote_amount"],
        position["buy_fee"],
        last_bid,
    )

    return {
        "closed": False,
        "exit_ms": None,
        "exit_price": last_bid,
        "reason": f"open_na_{HORIZON_HOURS}h",
        "net_pnl": unrealized,
        "holding_min": HORIZON_HOURS * 60.0,
        "final_stop": position["stop"],
        "min_profitable_price": min_exit,
    }


def actual_pnl(item):
    sell = item.get("sell")

    if not sell:
        return math.nan

    return num(
        sell.get("net_pnl_quote"),
        math.nan,
    )


longs = load_longs()

print(
    f"LONG-signalen gevonden: {len(longs)} (verwacht 18)"
)

print(
    "Varianten: CURRENT/WAIT15/WAIT30 x min €1.00/€0.50"
)

print(
    f"Exit: SL={ATR_SL:.2f}ATR TP={ATR_TP:.2f}ATR "
    f"hard={HARD_SL_PCT:.2f}% trail={TRAIL_ATR:.2f}ATR"
)

print(
    f"Profit trail: trigger={PROFIT_TRIGGER_PCT:.2f}% "
    f"pullback={PROFIT_PULLBACK_PCT:.2f}%"
)

print(
    f"Fee: {FEE_PCT:.3f}% per zijde | horizon={HORIZON_HOURS}h"
)

print(
    "Geen orders, config- of statewijzigingen.\n"
)

exchange = ccxt.bitvavo(
    {
        "enableRateLimit": True,
        "options": {
            "fetchMarkets": {
                "types": ["spot"]
            }
        },
    }
)

exchange.load_markets()

results = []

for index, item in enumerate(
    longs,
    1,
):
    buy = item["buy"]
    sell = item["sell"]
    symbol = item["symbol"]

    signal_ms = int(
        buy["_dt"].timestamp()
        * 1000
    )

    real_entry = num(
        buy.get("price"),
        0.0,
    )

    real_amount = num(
        buy.get("base_amount"),
        0.0,
    )

    real_quote = num(
        buy.get("quote_amount"),
        0.0,
    )

    real_buy_fee = num(
        buy.get("fees_quote"),
        0.0,
    )

    if real_entry <= 0:
        print(
            f"{index:02d} {symbol:7s} FOUT: ongeldige BUY-prijs"
        )
        continue

    if real_amount <= 0 and real_quote > 0:
        real_amount = (
            real_quote / real_entry
        )

    if real_quote <= 0:
        real_quote = (
            real_amount * real_entry
        )

    if real_buy_fee <= 0:
        real_buy_fee = (
            real_quote
            * FEE_PCT
            / 100.0
        )

    stake = real_quote
    spread = spread_proxy_pct(
        buy,
        sell,
    )

    try:
        frame = fetch_frame(
            exchange,
            symbol,
            signal_ms,
        )

        atr = signal_atr(
            frame,
            signal_ms,
        )

        variant_count = 0

        for variant, spec in VARIANTS.items():
            wait_min = int(
                spec["wait_min"]
            )
            min_profit = float(
                spec["min_profit"]
            )

            if wait_min == 0:
                entry_ms = signal_ms
                entry_price = real_entry
                amount = real_amount
                quote_amount = real_quote
                buy_fee = real_buy_fee

            else:
                delayed = delayed_entry(
                    frame,
                    signal_ms,
                    wait_min,
                    spread,
                )

                if not delayed:
                    continue

                entry_ms, entry_price = delayed

                amount = (
                    stake
                    / entry_price
                )

                quote_amount = (
                    amount
                    * entry_price
                )

                buy_fee = (
                    quote_amount
                    * FEE_PCT
                    / 100.0
                )

            position = make_position(
                entry_price,
                atr,
                amount,
                quote_amount,
                buy_fee,
            )

            sim = simulate(
                frame,
                entry_ms,
                position,
                spread,
                min_profit,
            )

            if sim is None:
                continue

            variant_count += 1

            results.append(
                {
                    "trade": index,
                    "symbol": symbol,
                    "signal_utc": buy["_dt"].isoformat(),
                    "variant": variant,
                    "wait_min": wait_min,
                    "min_profit_eur": min_profit,
                    "entry_price": entry_price,
                    "signal_atr": atr,
                    "spread_proxy_pct": spread,
                    "quote_amount": quote_amount,
                    "buy_fee": buy_fee,
                    "actual_pnl": actual_pnl(item),
                    **sim,
                }
            )

        print(
            f"{index:02d} {symbol:7s} "
            f"ATR={atr/real_entry*100:5.3f}% "
            f"spread={spread:5.3f}% "
            f"varianten={variant_count}/6"
        )

    except Exception as exc:
        print(
            f"{index:02d} {symbol:7s} FOUT: {exc}"
        )

    time.sleep(0.10)


if not results:
    raise SystemExit(
        "Geen resultaten."
    )


with OUT.open(
    "w",
    encoding="utf-8",
    newline="",
) as handle:
    writer = csv.DictWriter(
        handle,
        fieldnames=list(
            results[0].keys()
        ),
    )

    writer.writeheader()
    writer.writerows(
        results
    )


def group(variant):
    return [
        row
        for row in results
        if row["variant"] == variant
    ]


print(
    "\n===== ENTRY + MIN-PROFIT COMBINATIE ====="
)

actual_values = [
    actual_pnl(item)
    for item in longs
    if math.isfinite(
        actual_pnl(item)
    )
]

if actual_values:
    print(
        f"WERKELIJK          trades={len(actual_values):2d} "
        f"wins={sum(v > 0 for v in actual_values):2d} "
        f"losses={sum(v <= 0 for v in actual_values):2d} "
        f"netto=€{sum(actual_values):+8.4f}"
    )

summary = {}

for variant, spec in VARIANTS.items():
    rows = group(variant)

    closed = [
        row
        for row in rows
        if row["closed"]
    ]

    open_rows = [
        row
        for row in rows
        if not row["closed"]
    ]

    pnls = [
        num(row["net_pnl"])
        for row in closed
    ]

    reasons = defaultdict(int)

    for row in closed:
        reasons[row["reason"]] += 1

    total = sum(pnls)
    wins = sum(v > 0 for v in pnls)
    losses = sum(v <= 0 for v in pnls)

    summary[variant] = {
        "net": total,
        "wins": wins,
        "losses": losses,
        "closed": len(closed),
        "open": len(open_rows),
        "reasons": reasons,
    }

    print(
        f"{variant:14s} "
        f"wait={int(spec['wait_min']):2d}m "
        f"min=€{spec['min_profit']:.2f} "
        f"closed={len(closed):2d} "
        f"wins={wins:2d} "
        f"losses={losses:2d} "
        f"netto=€{total:+8.4f} "
        f"SL={reasons['stop_loss'] + reasons['hard_stop_loss']:2d} "
        f"TRAIL={reasons['trailing_stop']:2d} "
        f"TP={reasons['take_profit']:2d} "
        f"TREND={reasons['trend_break']:2d}"
    )


print(
    "\n===== RANGLIJST ====="
)

ranking = sorted(
    summary.items(),
    key=lambda item: item[1]["net"],
    reverse=True,
)

for place, (variant, row) in enumerate(
    ranking,
    1,
):
    print(
        f"{place}. {variant:14s} "
        f"netto=€{row['net']:+.4f} "
        f"wins={row['wins']} "
        f"losses={row['losses']}"
    )


print(
    "\n===== VERSCHIL BINNEN DEZELFDE ENTRY ====="
)

pairs = [
    ("CURRENT_100", "CURRENT_050"),
    ("WAIT15_100", "WAIT15_050"),
    ("WAIT30_100", "WAIT30_050"),
]

for base_name, test_name in pairs:
    base = summary.get(base_name)
    test = summary.get(test_name)

    if not base or not test:
        continue

    print(
        f"{test_name:14s} vs {base_name:14s} "
        f"delta_netto=€{test['net'] - base['net']:+.4f} "
        f"delta_wins={test['wins'] - base['wins']:+d} "
        f"delta_losses={test['losses'] - base['losses']:+d}"
    )


print(
    "\n===== PER TRADE ====="
)

for trade_number in range(
    1,
    len(longs) + 1,
):
    rows = [
        row
        for row in results
        if int(row["trade"]) == trade_number
    ]

    if not rows:
        continue

    symbol = rows[0]["symbol"]
    parts = []

    for variant in VARIANTS:
        row = next(
            (
                item
                for item in rows
                if item["variant"] == variant
            ),
            None,
        )

        if row is None:
            parts.append(
                f"{variant}=GEEN"
            )
        else:
            parts.append(
                f"{variant}=€{num(row['net_pnl']):+.2f}/{row['reason']}"
            )

    print(
        f"{trade_number:02d} {symbol:7s} "
        + " | ".join(parts)
    )


print(
    "\n===== INTERPRETATIE ====="
)

best_name, best_row = ranking[0]

print(
    f"Beste combinatie op gesloten netto PnL: "
    f"{best_name} (€{best_row['net']:+.4f})."
)

if "050" in best_name:
    print(
        "De beste combinatie gebruikt in deze historische test "
        "een minimum netto winst van €0.50."
    )
else:
    print(
        "De beste combinatie gebruikt in deze historische test "
        "de huidige minimum netto winst van €1.00."
    )

if "WAIT30" in best_name:
    print(
        "De beste combinatie gebruikt 30 minuten uitgestelde entry."
    )
elif "WAIT15" in best_name:
    print(
        "De beste combinatie gebruikt 15 minuten uitgestelde entry."
    )
else:
    print(
        "De beste combinatie gebruikt de huidige entry."
    )

print(
    "\nLET OP: historische bid/ask is benaderd "
    "met de geregistreerde spread."
)

print(
    f"Details: {OUT}"
)

print(
    "Geen config/state/strategie gewijzigd."
)
