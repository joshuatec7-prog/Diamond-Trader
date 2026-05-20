#!/usr/bin/env python3
"""
Diamond Grid Bot v2
- Koopt direct bij opstart op alle levels onder huidige prijs
- Verkoopt zodra prijs het volgende level bereikt
"""
import json
import logging
import os
import time
import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any

import ccxt
from dotenv import load_dotenv

LOG = logging.getLogger("grid_bot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

STATE_FILE  = "/opt/render/project/src/grid_state.json"
TRADES_FILE = "/opt/render/project/src/grid_transactions.csv"

GRID_COINS      = ["BTC/EUR", "ETH/EUR", "XRP/EUR", "SOL/EUR"]
STAKE_PER_COIN  = 75.0
GRID_LEVELS     = 10
EUR_RESERVE     = 25.0
LOOP_SLEEP      = 30
RANGE_PCT       = 8.0
TAKER_FEE_PCT   = 0.25


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_state() -> Dict[str, Any]:
    if not Path(STATE_FILE).exists():
        return {"grids": {}, "pnl": 0.0, "trades": 0, "wins": 0}
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {"grids": {}, "pnl": 0.0, "trades": 0, "wins": 0}


def save_state(state: Dict[str, Any]):
    Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
    json.dump(state, open(STATE_FILE, "w"), indent=2)


def append_csv(row: Dict[str, Any]):
    Path(TRADES_FILE).parent.mkdir(parents=True, exist_ok=True)
    exists = Path(TRADES_FILE).exists()
    cols = ["ts", "market", "side", "price", "amount", "quote", "fee", "pnl"]
    with open(TRADES_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if not exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in cols})


class GridBot:
    def __init__(self):
        load_dotenv()
        self.api_key     = os.getenv("BITVAVO_API_KEY", "").strip()
        self.api_secret  = os.getenv("BITVAVO_API_SECRET", "").strip()
        self.operator_id = os.getenv("BITVAVO_OPERATOR_ID", "").strip()

        self.exchange = ccxt.bitvavo({
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "enableRateLimit": True,
        })
        self.exchange.load_markets()
        self.state = load_state()

    def params(self):
        return {"operatorId": self.operator_id} if self.operator_id else {}

    def get_price(self, symbol: str) -> float:
        ticker = self.exchange.fetch_ticker(symbol)
        return float(ticker.get("last") or 0)

    def get_min_notional(self, symbol: str) -> float:
        m = self.exchange.market(symbol)
        limit_cost = (((m.get("limits") or {}).get("cost") or {}).get("min"))
        if limit_cost:
            return float(limit_cost)
        info = m.get("info") or {}
        raw = info.get("minOrderInQuoteAsset") or info.get("minOrderInBaseAsset")
        return float(raw) if raw else 5.0

    def setup_grid(self, symbol: str, price: float) -> Dict:
        low  = price * (1 - RANGE_PCT / 100)
        high = price * (1 + RANGE_PCT / 100)
        step = (high - low) / GRID_LEVELS
        levels = [round(low + i * step, 8) for i in range(GRID_LEVELS + 1)]
        amount_per_level = (STAKE_PER_COIN / GRID_LEVELS) / price

        LOG.info("GRID SETUP %s | prijs=%.4f | range=%.4f-%.4f | step=%.4f",
                 symbol, price, low, high, step)

        return {
            "symbol": symbol,
            "low": low, "high": high, "step": step,
            "levels": levels,
            "amount_per_level": amount_per_level,
            "start_price": price,
            "positions": {},  # level_idx -> {amount, buy_price, buy_cost}
            "active": True,
            "created_at": now_iso(),
        }

    def buy_level(self, symbol: str, grid: Dict, level_idx: int):
        """Koop op een specifiek grid level."""
        level_key = str(level_idx)
        if level_key in grid["positions"]:
            return  # al gekocht op dit level

        buy_price = grid["levels"][level_idx]
        amount    = grid["amount_per_level"]
        min_not   = self.get_min_notional(symbol)

        # Zorg dat order groot genoeg is
        if amount * buy_price < min_not:
            amount = (min_not * 1.1) / buy_price

        try:
            amount_f = float(self.exchange.amount_to_precision(symbol, amount))
            if amount_f <= 0:
                return

            order = self.exchange.create_order(
                symbol, "market", "buy", amount_f, None, self.params()
            )
            actual_price  = float(order.get("average") or order.get("price") or buy_price)
            actual_amount = float(order.get("filled") or order.get("amount") or amount_f)
            cost          = float(order.get("cost") or actual_amount * actual_price)
            fee           = cost * (TAKER_FEE_PCT / 100)

            grid["positions"][level_key] = {
                "amount":    actual_amount,
                "buy_price": actual_price,
                "buy_cost":  cost,
                "sell_at":   grid["levels"][level_idx + 1] if level_idx + 1 < len(grid["levels"]) else actual_price * 1.01,
            }
            save_state(self.state)
            append_csv({
                "ts": now_iso(), "market": symbol, "side": "BUY",
                "price": round(actual_price, 8), "amount": actual_amount,
                "quote": round(cost, 4), "fee": round(fee, 4), "pnl": "",
            })
            LOG.info("KOOP %s | level=%s | prijs=%.6f | amount=%.6f | cost=%.2f EUR",
                     symbol, level_idx, actual_price, actual_amount, cost)
            time.sleep(0.5)

        except Exception as e:
            LOG.warning("KOOP mislukt %s level %s: %s", symbol, level_idx, e)

    def sell_level(self, symbol: str, grid: Dict, level_idx: int):
        """Verkoop positie op een grid level."""
        level_key = str(level_idx)
        pos = grid["positions"].get(level_key)
        if not pos:
            return

        try:
            amount_f = float(self.exchange.amount_to_precision(symbol, pos["amount"]))
            if amount_f <= 0:
                return

            order = self.exchange.create_order(
                symbol, "market", "sell", amount_f, None, self.params()
            )
            actual_price  = float(order.get("average") or order.get("price") or pos["sell_at"])
            actual_amount = float(order.get("filled") or order.get("amount") or amount_f)
            revenue       = float(order.get("cost") or actual_amount * actual_price)
            sell_fee      = revenue * (TAKER_FEE_PCT / 100)
            buy_fee       = pos["buy_cost"] * (TAKER_FEE_PCT / 100)
            pnl           = revenue - sell_fee - pos["buy_cost"] - buy_fee

            self.state["pnl"]    = round(self.state.get("pnl", 0.0) + pnl, 4)
            self.state["trades"] = self.state.get("trades", 0) + 1
            if pnl > 0:
                self.state["wins"] = self.state.get("wins", 0) + 1

            del grid["positions"][level_key]
            save_state(self.state)
            append_csv({
                "ts": now_iso(), "market": symbol, "side": "SELL",
                "price": round(actual_price, 8), "amount": actual_amount,
                "quote": round(revenue, 4), "fee": round(sell_fee, 4),
                "pnl": round(pnl, 4),
            })
            LOG.info("VERKOOP %s | level=%s | prijs=%.6f | pnl=%.4f EUR | totaal=%.2f EUR",
                     symbol, level_idx, actual_price, pnl, self.state["pnl"])
            time.sleep(0.5)

        except Exception as e:
            LOG.warning("VERKOOP mislukt %s level %s: %s", symbol, level_idx, e)

    def manage_grid(self, symbol: str):
        grid = self.state["grids"].get(symbol)
        if not grid or not grid.get("active"):
            return

        try:
            price  = self.get_price(symbol)
            levels = grid["levels"]
            low    = grid["low"]
            high   = grid["high"]
        except Exception as e:
            LOG.warning("Prijs ophalen mislukt %s: %s", symbol, e)
            return

        # Reset als prijs buiten range breekt
        if price < low * 0.97 or price > high * 1.03:
            LOG.info("GRID RESET %s | prijs=%.4f buiten range %.4f-%.4f", symbol, price, low, high)
            # Verkoop eerst alle open posities
            for level_key in list(grid["positions"].keys()):
                self.sell_level(symbol, grid, int(level_key))
            self.state["grids"][symbol] = self.setup_grid(symbol, price)
            save_state(self.state)
            return

        # Bepaal huidig level
        current_level = 0
        for i, level in enumerate(levels[:-1]):
            if price >= level:
                current_level = i

        # KOOP: alle levels onder huidige prijs die nog geen positie hebben
        for i in range(current_level):
            if str(i) not in grid["positions"]:
                self.buy_level(symbol, grid, i)

        # VERKOOP: posities waarvan de sell_at prijs geraakt is
        for level_key, pos in list(grid["positions"].items()):
            if price >= pos["sell_at"] * 0.999:
                self.sell_level(symbol, grid, int(level_key))

    def run(self):
        LOG.info("Diamond Grid Bot v2 gestart | coins=%s | stake=%.0f EUR/coin",
                 GRID_COINS, STAKE_PER_COIN)

        # Setup grids
        for symbol in GRID_COINS:
            if symbol not in self.state.get("grids", {}):
                try:
                    price = self.get_price(symbol)
                    self.state.setdefault("grids", {})[symbol] = self.setup_grid(symbol, price)
                    save_state(self.state)
                    time.sleep(1)
                except Exception as e:
                    LOG.error("Grid setup mislukt %s: %s", symbol, e)

        loop = 0
        while True:
            try:
                for symbol in GRID_COINS:
                    self.manage_grid(symbol)
                    time.sleep(1)
                loop += 1
                if loop % 20 == 0:
                    LOG.info("STATUS | grids=4 | trades=%s | winrate=%.1f%% | pnl=%.2f EUR",
                             self.state.get("trades", 0),
                             (self.state.get("wins", 0) / self.state["trades"] * 100)
                             if self.state.get("trades") else 0,
                             self.state.get("pnl", 0.0))
            except Exception as e:
                LOG.exception("Loop fout: %s", e)
            time.sleep(LOOP_SLEEP)


if __name__ == "__main__":
    bot = GridBot()
    bot.run()
