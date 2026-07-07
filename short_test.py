#!/usr/bin/env python3
"""
short_test.py - PAPER TRADING test voor short-grid strategie.
GEEN echte orders, GEEN aanraking van grid_bot.py of grid_state.json.
Simuleert shorts: open op prijsstijging boven een grid-level,
sluit (winst) bij daling met margin, of stop-loss bij verdere stijging.

State: /var/data/short_test_state.json  (los bestand)
Log:   /var/data/short_test_transactions.csv
"""

import csv
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import ccxt
from dotenv import load_dotenv

LOG = logging.getLogger("short_test")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ── Config (los van de live bot) ────────────────────────────────────────────
COINS        = ["BTC/EUR", "ETH/EUR", "SOL/EUR", "XRP/EUR", "ADA/EUR"]
GRID_LEVELS  = 8
RANGE_PCT    = 3.0
STOP_LOSS    = 5.0        # % stijging t.o.v. short-entry = stop-loss
SELL_MARGIN  = 0.8        # % daling t.o.v. entry = winst nemen (zelfde als live bot)
MAX_POSITIONS = 4
STAKE        = 125.0      # fictief bedrag, geen echt geld
LOOP_SLEEP   = 60
TAKER_FEE    = 0.25
TREND_FILTER_PCT = 0.0    # alleen shorten als 24u-verandering onder dit % ligt (negatief = dalend)
COIN_STOP_LOSS = {        # per-coin override, whipsaw-coins krijgen krappere stop
    "ADA/EUR": 2.5,
}
MOMENTUM_CANDLES   = 12    # aantal 5-min candles terugkijken (~1 uur)
MOMENTUM_TIMEFRAME = "5m"
MAX_HOLD_HOURS = 12         # sluit positie automatisch als tp/stop niet binnen deze tijd geraakt wordt

STATE_FILE  = "/var/data/short_test_state.json"
TRADES_FILE = "/var/data/short_test_transactions.csv"


def get_momentum_direction(exchange, symbol):
    """Simpel momentum-signaal: vergelijkt de laatste candle-close met het
    gemiddelde van de vorige MOMENTUM_CANDLES candles. Negatief = dalende trend
    (goed voor shorts), positief = stijgende trend (niet shorten)."""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=MOMENTUM_TIMEFRAME, limit=MOMENTUM_CANDLES + 1)
        if len(ohlcv) < MOMENTUM_CANDLES + 1:
            return 0.0
        closes = [c[4] for c in ohlcv]
        avg_prev = sum(closes[:-1]) / len(closes[:-1])
        last = closes[-1]
        return (last - avg_prev) / avg_prev * 100  # % afwijking t.o.v. gemiddelde
    except Exception as e:
        LOG.warning("Momentum ophalen mislukt %s: %s", symbol, e)
        return 0.0


def load_state():
    if not Path(STATE_FILE).exists():
        return {"grids": {}, "pnl": 0.0, "trades": 0, "wins": 0}
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {"grids": {}, "pnl": 0.0, "trades": 0, "wins": 0}


def save_state(state):
    json.dump(state, open(STATE_FILE, "w"), indent=2)


def log_trade(side, symbol, amount, price, cost, pnl=0.0):
    Path(TRADES_FILE).parent.mkdir(parents=True, exist_ok=True)
    write_header = not Path(TRADES_FILE).exists()
    with open(TRADES_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["ts", "market", "side", "amount", "price", "quote_amount", "pnl"])
        w.writerow([
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            symbol, side, f"{amount:.8f}", f"{price:.8f}", f"{cost:.4f}", f"{pnl:.4f}",
        ])


def setup_grid(symbol, price, state):
    step = price * (RANGE_PCT / 100) / (GRID_LEVELS // 2)
    low  = price * (1 - RANGE_PCT / 100)
    high = price * (1 + RANGE_PCT / 100)
    levels = [low + i * step for i in range(GRID_LEVELS + 1)]
    state["grids"][symbol] = {
        "low": low, "high": high, "step": step, "levels": levels,
        "positions": {}, "last_reset": datetime.now(timezone.utc).isoformat(),
    }
    LOG.info("GRID SETUP %s | prijs=%.4f | laag=%.4f | hoog=%.4f", symbol, price, low, high)


def try_short(symbol, level_idx, level_price, price, state):
    """PAPER: open een fictieve short als prijs boven een level uitkomt."""
    grid = state["grids"][symbol]
    key = str(level_idx)
    if key in grid["positions"]:
        return
    if len(grid["positions"]) >= MAX_POSITIONS:
        return

    stop_pct = COIN_STOP_LOSS.get(symbol, STOP_LOSS)
    amount = STAKE / price
    entry_cost = STAKE
    take_profit_at = price * (1 - SELL_MARGIN / 100)   # winst bij daling
    stop_at        = price * (1 + stop_pct / 100)      # verlies-stop bij stijging

    grid["positions"][key] = {
        "amount": amount, "entry_price": price, "entry_cost": entry_cost,
        "take_profit_at": take_profit_at, "stop_at": stop_at,
        "level": level_idx, "ts": datetime.now(timezone.utc).isoformat(),
    }
    save_state(state)
    log_trade("SHORT_OPEN", symbol, amount, price, entry_cost)
    LOG.info("SHORT OPEN %s | level=%s | prijs=%.4f | tp@%.4f | stop@%.4f",
             symbol, key, price, take_profit_at, stop_at)


def try_close(symbol, key, pos, current_price, state, reason):
    """PAPER: sluit fictieve short. Winst bij daling (short-logica omgekeerd t.o.v. long)."""
    amount = pos["amount"]
    entry_cost = pos["entry_cost"]
    exit_value = current_price * amount

    # Short-winst = entry_cost - exit_value (prijs gedaald = winst)
    fee_open  = entry_cost * (TAKER_FEE / 100)
    fee_close = exit_value * (TAKER_FEE / 100)
    pnl = (entry_cost - exit_value) - fee_open - fee_close

    state["trades"] += 1
    if pnl > 0:
        state["wins"] += 1
    state["pnl"] = round(state.get("pnl", 0) + pnl, 4)
    del state["grids"][symbol]["positions"][key]
    save_state(state)

    log_trade("SHORT_CLOSE", symbol, amount, current_price, exit_value, pnl)
    LOG.info("SHORT CLOSE %s | reden=%s | prijs=%.4f | pnl=%+.4f EUR",
             symbol, reason, current_price, pnl)


def manage_coin(exchange, symbol, state):
    grid = state["grids"].get(symbol)
    try:
        ticker = exchange.fetch_ticker(symbol)
        price = float(ticker.get("last") or ticker.get("close") or 0)
        change_pct = ticker.get("percentage")  # 24u verandering in %
        if change_pct is None:
            change_pct = 0.0
        if price <= 0:
            return
    except Exception as e:
        LOG.warning("Prijs ophalen mislukt %s: %s", symbol, e)
        return

    if not grid:
        setup_grid(symbol, price, state)
        grid = state["grids"][symbol]
    elif price < grid["low"] * 0.97 or price > grid["high"] * 1.03:
        old_positions = grid.get("positions", {})
        setup_grid(symbol, price, state)
        state["grids"][symbol]["positions"] = old_positions
        grid = state["grids"][symbol]

    # Bestaande shorts checken: winst nemen, stop-loss, of max-houdtijd overschreden
    for key in list(grid["positions"].keys()):
        pos = grid["positions"][key]
        if price <= pos["take_profit_at"]:
            try_close(symbol, key, pos, price, state, "take_profit")
            continue
        if price >= pos["stop_at"]:
            try_close(symbol, key, pos, price, state, "stop_loss")
            continue
        opened = datetime.fromisoformat(pos["ts"])
        hours_open = (datetime.now(timezone.utc) - opened).total_seconds() / 3600
        if hours_open >= MAX_HOLD_HOURS:
            try_close(symbol, key, pos, price, state, "max_hold_tijd")

    # Trend-filter: alleen nieuwe shorts als 24u-trend niet positief is
    if change_pct > TREND_FILTER_PCT:
        LOG.info("SKIP SHORT %s | 24u-trend=%+.2f%% (positief, niet shorten)", symbol, change_pct)
        return

    # Momentum-filter: alleen shorten als kortetermijn-momentum ook niet stijgend is
    momentum = get_momentum_direction(exchange, symbol)
    if momentum > 0:
        LOG.info("SKIP SHORT %s | momentum=%+.2f%% (stijgend, niet shorten)", symbol, momentum)
        return

    # Nieuwe shorts openen: prijs binnen 1% boven een grid-level
    levels = grid.get("levels", [])
    for i, level in enumerate(levels[1:], start=1):  # niet het laagste level shorten
        if abs(price - level) / level < 0.01 and price < level:
            try_short(symbol, i, level, price, state)


def main():
    load_dotenv()
    exchange = ccxt.bitvavo({
        "apiKey": os.getenv("BITVAVO_API_KEY", "").strip(),
        "secret": os.getenv("BITVAVO_API_SECRET", "").strip(),
        "enableRateLimit": True,
    })
    exchange.load_markets()

    LOG.info("SHORT PAPER-TEST gestart | GEEN echte orders | stake=%.0f", STAKE)
    state = load_state()

    while True:
        try:
            state = load_state()
            for symbol in COINS:
                manage_coin(exchange, symbol, state)
                time.sleep(1)
            LOG.info("Loop klaar | pnl=%+.2f | trades=%d", state.get("pnl", 0), state.get("trades", 0))
        except Exception as e:
            LOG.error("Loop fout: %s", e)
        time.sleep(LOOP_SLEEP)


if __name__ == "__main__":
    main()
