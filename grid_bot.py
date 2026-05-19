#!/usr/bin/env python3
"""
Diamond Grid Bot
- Grid trading op BTC/EUR, ETH/EUR, XRP/EUR, SOL/EUR
- €75 per coin, 10 grid levels per coin
- Koopt laag, verkoopt hoog binnen een prijsrange
- Geen trend analyse nodig
"""
import json
import logging
import os
import time
import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

import ccxt
from dotenv import load_dotenv

LOG = logging.getLogger("grid_bot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

STATE_FILE  = "/opt/render/project/src/grid_state.json"
TRADES_FILE = "/opt/render/project/src/grid_transactions.csv"

GRID_COINS = ["BTC/EUR", "ETH/EUR", "XRP/EUR", "SOL/EUR"]
STAKE_PER_COIN = 75.0
GRID_LEVELS = 10
EUR_RESERVE = 25.0
LOOP_SLEEP = 60  # elke minuut checken
RANGE_PCT = 8.0  # range is ±8% van huidige prijs bij opstart
MIN_PROFIT_PCT = 0.3  # minimaal 0.3% winst per grid trade
TAKER_FEE_PCT = 0.25


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
        self.api_key    = os.getenv("BITVAVO_API_KEY", "").strip()
        self.api_secret = os.getenv("BITVAVO_API_SECRET", "").strip()
        self.operator_id = os.getenv("BITVAVO_OPERATOR_ID", "").strip()

        self.exchange = ccxt.bitvavo({
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "enableRateLimit": True,
        })
        self.exchange.load_markets()
        self.state = load_state()

    def order_params(self):
        return {"operatorId": self.operator_id} if self.operator_id else {}

    def get_price(self, symbol: str) -> float:
        ticker = self.exchange.fetch_ticker(symbol)
        return float(ticker.get("last") or 0)

    def setup_grid(self, symbol: str, current_price: float):
        """Bereken grid levels op basis van huidige prijs."""
        low  = current_price * (1 - RANGE_PCT / 100)
        high = current_price * (1 + RANGE_PCT / 100)
        step = (high - low) / GRID_LEVELS

        levels = []
        for i in range(GRID_LEVELS + 1):
            levels.append(round(low + i * step, 8))

        # Verdeel stake over grid levels
        amount_per_level = (STAKE_PER_COIN / GRID_LEVELS) / current_price

        grid = {
            "symbol": symbol,
            "low": low,
            "high": high,
            "step": step,
            "levels": levels,
            "amount_per_level": amount_per_level,
            "current_price": current_price,
            "buy_orders": {},   # level_index -> {"price", "amount", "filled"}
            "sell_orders": {},  # level_index -> {"price", "amount", "buy_price"}
            "active": True,
            "created_at": now_iso(),
        }

        LOG.info(
            "GRID SETUP %s | prijs=%.4f | range=%.4f-%.4f | step=%.4f | levels=%s",
            symbol, current_price, low, high, step, GRID_LEVELS,
        )
        return grid

    def check_and_trade(self, symbol: str, grid: Dict[str, Any]):
        """Check huidige prijs en voer grid trades uit."""
        try:
            price = self.get_price(symbol)
        except Exception as e:
            LOG.warning("Prijs ophalen mislukt voor %s: %s", symbol, e)
            return

        levels = grid["levels"]
        amount = grid["amount_per_level"]
        low    = grid["low"]
        high   = grid["high"]

        # Check of prijs buiten range is
        if price < low * 0.98 or price > high * 1.02:
            LOG.info("GRID RESET %s | prijs %.4f buiten range %.4f-%.4f", symbol, price, low, high)
            self.state["grids"][symbol] = self.setup_grid(symbol, price)
            save_state(self.state)
            return

        # Zoek het huidige grid level
        current_level = 0
        for i, level in enumerate(levels):
            if price >= level:
                current_level = i

        # Koop orders: voor elk level onder huidige prijs dat nog niet gekocht is
        for i in range(current_level):
            level_key = str(i)
            buy_price = levels[i]
            sell_price = levels[i + 1]

            # Skip als al een verkoop order op dit level staat
            if level_key in grid["sell_orders"]:
                continue

            # Skip als al een koop order op dit level staat
            if level_key in grid["buy_orders"]:
                # Check of koop prijs geraakt is
                if price <= buy_price * 1.002:
                    # Voer koop uit
                    self._execute_buy(symbol, grid, i, buy_price, sell_price, amount)
                continue

            # Plaats nieuw koop order als prijs dicht bij dit level is
            if abs(price - buy_price) / buy_price < 0.005:  # binnen 0.5%
                self._execute_buy(symbol, grid, i, buy_price, sell_price, amount)

        # Verkoop orders: check of prijs een sell level geraakt heeft
        for level_key, sell_order in list(grid["sell_orders"].items()):
            sell_price = sell_order["sell_price"]
            if price >= sell_price * 0.998:
                self._execute_sell(symbol, grid, int(level_key), sell_order)

    def _execute_buy(self, symbol: str, grid: Dict, level_idx: int, buy_price: float, sell_price: float, amount: float):
        """Voer een koop uit op een grid level."""
        level_key = str(level_idx)
        try:
            # Controleer minimale orderwaarde
            min_notional = self._get_min_notional(symbol)
            order_value = amount * buy_price
            if order_value < min_notional:
                amount = (min_notional * 1.05) / buy_price

            amount_str = float(self.exchange.amount_to_precision(symbol, amount))
            if amount_str <= 0:
                return

            order = self.exchange.create_order(
                symbol, "market", "buy", amount_str, None, self.order_params()
            )
            actual_price = float(order.get("average") or order.get("price") or buy_price)
            actual_amount = float(order.get("filled") or order.get("amount") or amount_str)
            cost = float(order.get("cost") or actual_amount * actual_price)
            fee = cost * (TAKER_FEE_PCT / 100)

            grid["buy_orders"][level_key] = {
                "price": actual_price,
                "amount": actual_amount,
                "cost": cost,
            }
            grid["sell_orders"][level_key] = {
                "sell_price": sell_price,
                "amount": actual_amount,
                "buy_price": actual_price,
                "buy_cost": cost,
            }

            save_state(self.state)
            append_csv({
                "ts": now_iso(), "market": symbol, "side": "BUY",
                "price": round(actual_price, 8), "amount": actual_amount,
                "quote": round(cost, 4), "fee": round(fee, 4), "pnl": "",
            })
            LOG.info(
                "GRID KOOP %s | level=%s | prijs=%.6f | amount=%.6f | cost=%.2f EUR",
                symbol, level_idx, actual_price, actual_amount, cost,
            )
        except Exception as e:
            LOG.warning("GRID KOOP mislukt %s level %s: %s", symbol, level_idx, e)

    def _execute_sell(self, symbol: str, grid: Dict, level_idx: int, sell_order: Dict):
        """Voer een verkoop uit op een grid level."""
        level_key = str(level_idx)
        try:
            amount = sell_order["amount"]
            amount_str = float(self.exchange.amount_to_precision(symbol, amount))
            if amount_str <= 0:
                return

            order = self.exchange.create_order(
                symbol, "market", "sell", amount_str, None, self.order_params()
            )
            actual_price = float(order.get("average") or order.get("price") or sell_order["sell_price"])
            actual_amount = float(order.get("filled") or order.get("amount") or amount_str)
            sell_revenue = float(order.get("cost") or actual_amount * actual_price)
            sell_fee = sell_revenue * (TAKER_FEE_PCT / 100)
            buy_cost = sell_order["buy_cost"]
            buy_fee = buy_cost * (TAKER_FEE_PCT / 100)
            pnl = sell_revenue - sell_fee - buy_cost - buy_fee

            self.state["pnl"] = round(self.state.get("pnl", 0.0) + pnl, 4)
            self.state["trades"] = self.state.get("trades", 0) + 1
            if pnl > 0:
                self.state["wins"] = self.state.get("wins", 0) + 1

            # Verwijder sell order, houd level vrij voor nieuwe koop
            grid["sell_orders"].pop(level_key, None)
            grid["buy_orders"].pop(level_key, None)

            save_state(self.state)
            append_csv({
                "ts": now_iso(), "market": symbol, "side": "SELL",
                "price": round(actual_price, 8), "amount": actual_amount,
                "quote": round(sell_revenue, 4), "fee": round(sell_fee, 4),
                "pnl": round(pnl, 4),
            })
            LOG.info(
                "GRID VERKOOP %s | level=%s | prijs=%.6f | pnl=%.4f EUR | totaal_pnl=%.2f EUR",
                symbol, level_idx, actual_price, pnl, self.state["pnl"],
            )
        except Exception as e:
            LOG.warning("GRID VERKOOP mislukt %s level %s: %s", symbol, level_idx, e)

    def _get_min_notional(self, symbol: str) -> float:
        m = self.exchange.market(symbol)
        limit_cost = (((m.get("limits") or {}).get("cost") or {}).get("min"))
        if limit_cost:
            return float(limit_cost)
        info = m.get("info") or {}
        raw = info.get("minOrderInQuoteAsset") or info.get("minOrderInBaseAsset")
        return float(raw) if raw else 5.0

    def print_status(self):
        pnl    = self.state.get("pnl", 0.0)
        trades = self.state.get("trades", 0)
        wins   = self.state.get("wins", 0)
        winrate = (wins / trades * 100) if trades else 0
        active_grids = len([g for g in self.state.get("grids", {}).values() if g.get("active")])
        LOG.info(
            "STATUS | grids=%s | trades=%s | winrate=%.1f%% | pnl=%.2f EUR",
            active_grids, trades, winrate, pnl,
        )

    def run(self):
        LOG.info("Diamond Grid Bot gestart | coins=%s | stake=%.0f EUR/coin", GRID_COINS, STAKE_PER_COIN)

        # Setup grids voor alle coins
        for symbol in GRID_COINS:
            if symbol not in self.state.get("grids", {}):
                try:
                    price = self.get_price(symbol)
                    self.state.setdefault("grids", {})[symbol] = self.setup_grid(symbol, price)
                    save_state(self.state)
                    time.sleep(1)
                except Exception as e:
                    LOG.error("Grid setup mislukt voor %s: %s", symbol, e)

        last_status = 0.0
        loop = 0

        while True:
            try:
                for symbol in GRID_COINS:
                    grid = self.state.get("grids", {}).get(symbol)
                    if grid and grid.get("active"):
                        self.check_and_trade(symbol, grid)
                    time.sleep(1)

                loop += 1
                if loop % 10 == 0:
                    self.print_status()

            except Exception as e:
                LOG.exception("Loop fout: %s", e)

            time.sleep(LOOP_SLEEP)


if __name__ == "__main__":
    bot = GridBot()
    bot.run()
