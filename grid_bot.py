#!/usr/bin/env python3
"""
Diamond Grid Bot v5.2

WIJZIGINGEN 14-07-2026:
(a) paused blokkeert alleen nieuwe aankopen; open posities blijven bewaakt.
(b) TAKER_FEE 0.25 -> 0.20 (gemeten via fetch_trading_fees).
(c) sell_at = vast percentage boven koopprijs i.p.v. de grid-step.

WIJZIGING 16-07-2026:
(d) BUGFIX market_wide_pause_active(): crash-moment wordt vastgelegd, zodat
    de cooldown echt 6 uur duurt i.p.v. ~30 minuten.

WIJZIGINGEN 16-07-2026 (avond), na analyse van de Bitvavo-export:
(e) BUGFIX try_buy(): amount werd berekend als stake / level_price, maar er
    wordt gekocht tegen de marktprijs, die per definitie hoger ligt (de
    koopconditie is price > level). Daardoor werd er tot 1% te veel gekocht:
    stake 135 -> orders van 135,27 tot 136,74. Nu: stake / price.

(f) NIEUW: harde drawdown-bodem (MAX_TEST_DRAWDOWN). Zodra het verlies sinds
    het begin van de testperiode die grens raakt, pauzeert de bot zichzelf
    permanent. agent.py hervat dit NIET automatisch: die kijkt alleen naar
    pause_reason met 'dagverlies' of 'btc_daling' erin.

LET OP - fee-tier waarschuwing:
Op 13-07 stond het 30-daags volume op EUR 100.293 met fee 0,25%. Op 14-07
stond het op EUR 109.174 met fee 0,20%. De lage fee begint dus rond EUR
100.000 30-daags volume, en 99,6% van dat volume komt van deze bot.
De break-even winrate is 61,8% bij 0,20% fee en 63,7% bij 0,25%. Gemeten
winrate: 63,5%. De hele marge van deze strategie hangt dus aan de fee-tier.
Minder trades -> minder volume -> hogere fee -> negatieve verwachting.
"""

import csv
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ccxt
from dotenv import load_dotenv

LOG = logging.getLogger("grid_bot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

# ── Configuratie ────────────────────────────────────────────────────────────
GRID_COINS   = ["BTC/EUR", "ETH/EUR", "SOL/EUR", "XRP/EUR", "ADA/EUR"]
GRID_LEVELS  = 8
RANGE_PCT    = 3.0       # ±3% range
STOP_LOSS    = 3.0       # 3% stop-loss per positie
SELL_TARGET  = 2.5       # 2.5% winst-target per positie
MAX_POSITIONS = 4        # max open posities per coin
LOOP_SLEEP   = 60        # seconden tussen checks
TAKER_FEE    = 0.20      # 0.20% Bitvavo taker fee (geldt boven ~100k 30d-volume)

# Harde bodem voor de testperiode. Verlies sinds test_start_pnl. Bij het
# raken hiervan pauzeert de bot permanent en stuurt agent.py bij het
# eerstvolgende rapport de reden mee.
MAX_TEST_DRAWDOWN = 50.0

STATE_FILE  = "/var/data/grid_state.json"
TRADES_FILE = "/var/data/grid_transactions.csv"

SCHALING = [
    (3500, 140, 4),
    (2500, 125, 4),
    (1500, 90,  4),
    (800,  60,  3),
    (0,    45,  3),
]

COIN_MAX_POSITIONS = {
    "ADA/EUR": 2,
}

TREND_DROP_LIMIT = -3.0
STOP_LOSS_COOLDOWN_HOURS = 4
MARKET_WIDE_SL_COUNT = 3
MARKET_WIDE_SL_WINDOW_MIN = 30
MARKET_WIDE_COOLDOWN_HOURS = 6

WEEKLY_STAKE_STEP = 5
WEEKLY_STAKE_MAX  = 140


def get_stake_and_max(total_inleg: float, state: dict = None):
    default_stake, max_pos_default = 45, 3
    for min_inleg, stake, max_pos in SCHALING:
        if total_inleg >= min_inleg:
            default_stake, max_pos_default = stake, max_pos
            break

    if state is not None and "manual_stake" in state:
        return float(state["manual_stake"]), max_pos_default
    return default_stake, max_pos_default


def maybe_bump_weekly_stake(state: dict):
    """Verhoogt de stake elke zondag met WEEKLY_STAKE_STEP, tot WEEKLY_STAKE_MAX."""
    now = datetime.now(timezone.utc)
    if now.weekday() != 6:
        return

    today_str = now.strftime("%Y-%m-%d")
    if state.get("last_stake_bump") == today_str:
        return

    if "manual_stake" not in state:
        total_inleg = float(state.get("total_inleg", 1795))
        current_stake, _ = get_stake_and_max(total_inleg)
        state["manual_stake"] = current_stake

    if state["manual_stake"] < WEEKLY_STAKE_MAX:
        oud = state["manual_stake"]
        state["manual_stake"] = min(state["manual_stake"] + WEEKLY_STAKE_STEP, WEEKLY_STAKE_MAX)
        LOG.info("WEKELIJKSE STAKE VERHOOGD | %.0f -> %.0f EUR", oud, state["manual_stake"])

    state["last_stake_bump"] = today_str
    save_state(state)


def check_test_drawdown(state: dict) -> bool:
    """Harde bodem onder de testperiode.

    Legt bij de eerste run het startpunt vast in state['test_start_pnl'].
    Zakt de PnL daarna MAX_TEST_DRAWDOWN euro onder dat startpunt, dan
    pauzeert de bot zichzelf. De reden bevat bewust niet 'dagverlies' of
    'btc_daling', zodat agent.py hem niet automatisch hervat.

    Geeft True terug als de bodem geraakt is.
    """
    if "test_start_pnl" not in state:
        state["test_start_pnl"] = float(state.get("pnl", 0.0))
        state["test_start_ts"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        LOG.info("TEST GESTART | startpunt PnL=%.2f EUR | bodem bij -%.2f EUR",
                 state["test_start_pnl"], MAX_TEST_DRAWDOWN)
        return False

    if state.get("paused", False):
        return False  # al gepauzeerd, niets te doen

    resultaat = float(state.get("pnl", 0.0)) - float(state["test_start_pnl"])
    if resultaat <= -MAX_TEST_DRAWDOWN:
        state["paused"] = True
        state["pause_reason"] = f"test_drawdown_{resultaat:.2f}_EUR"
        save_state(state)
        LOG.warning("BODEM GERAAKT | resultaat sinds teststart: %+.2f EUR "
                    "(limiet -%.2f) | bot gepauzeerd, open posities blijven bewaakt",
                    resultaat, MAX_TEST_DRAWDOWN)
        return True
    return False


def market_wide_pause_active(state: dict) -> bool:
    """Detecteert een markt-brede crash en houdt daarna de cooldown vast.

    BUGFIX 16-07-2026: het venster van MARKET_WIDE_SL_WINDOW_MIN werd steeds
    opnieuw vanaf 'nu' gemeten. Zodra de oudste stop-loss uit dat venster
    schoof, zakte het aantal getroffen coins onder de drempel en verdween de
    pauze - in de praktijk na ~30 minuten i.p.v. 6 uur. Het eindmoment wordt
    nu eenmalig vastgelegd in state['market_wide_pause_until'].
    """
    now = datetime.now(timezone.utc)

    until = state.get("market_wide_pause_until")
    if until:
        try:
            until_dt = datetime.fromisoformat(until)
        except (TypeError, ValueError):
            state.pop("market_wide_pause_until", None)
            save_state(state)
            until_dt = None
        if until_dt:
            if now < until_dt:
                return True
            state.pop("market_wide_pause_until", None)
            save_state(state)
            LOG.info("MARKT-BREDE COOLDOWN verlopen | nieuwe aankopen weer toegestaan")

    events = state.get("recent_stop_losses", [])
    if len(events) < MARKET_WIDE_SL_COUNT:
        return False

    try:
        parsed = sorted((datetime.fromisoformat(e["ts"]), e["symbol"]) for e in events)
    except (TypeError, ValueError, KeyError):
        return False

    window = MARKET_WIDE_SL_WINDOW_MIN * 60

    for i, (t0, _) in enumerate(parsed):
        cluster = [(t, s) for t, s in parsed[i:] if (t - t0).total_seconds() <= window]
        coins_hit = {s for _, s in cluster}
        if len(coins_hit) < MARKET_WIDE_SL_COUNT:
            continue

        laatste = max(t for t, _ in cluster)
        until_dt = laatste + timedelta(hours=MARKET_WIDE_COOLDOWN_HOURS)
        if now >= until_dt:
            continue

        state["market_wide_pause_until"] = until_dt.isoformat()
        save_state(state)
        LOG.warning("MARKT-BREDE CRASH | %d coins (%s) | pauze tot %s UTC",
                    len(coins_hit), ", ".join(sorted(coins_hit)),
                    until_dt.strftime("%Y-%m-%d %H:%M"))
        return True

    return False


def load_state() -> dict:
    if not Path(STATE_FILE).exists():
        return {"grids": {}, "pnl": 0.0, "trades": 0, "wins": 0,
                "paused": False, "pause_reason": "", "total_inleg": 1795.0}
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {"grids": {}, "pnl": 0.0, "trades": 0, "wins": 0,
                "paused": False, "pause_reason": "", "total_inleg": 1795.0}


def save_state(state: dict):
    json.dump(state, open(STATE_FILE, "w"), indent=2)


def log_trade(side: str, symbol: str, amount: float, price: float,
              cost: float, pnl: float = 0.0):
    Path(TRADES_FILE).parent.mkdir(parents=True, exist_ok=True)
    write_header = not Path(TRADES_FILE).exists()
    with open(TRADES_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["ts", "market", "side", "amount", "price",
                        "quote_amount", "pnl"])
        w.writerow([
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            symbol, side,
            f"{amount:.8f}", f"{price:.8f}",
            f"{cost:.4f}", f"{pnl:.4f}",
        ])


def setup_grid(symbol: str, price: float, state: dict):
    step = price * (RANGE_PCT / 100) / (GRID_LEVELS // 2)
    low  = price * (1 - RANGE_PCT / 100)
    high = price * (1 + RANGE_PCT / 100)
    levels = [low + i * step for i in range(GRID_LEVELS + 1)]
    state["grids"][symbol] = {
        "low": low,
        "high": high,
        "step": step,
        "levels": levels,
        "positions": {},
        "last_reset": datetime.now(timezone.utc).isoformat(),
    }
    LOG.info("GRID SETUP %s | prijs=%.4f | laag=%.4f | hoog=%.4f | stap=%.4f",
             symbol, price, low, high, step)


def try_buy(exchange, symbol: str, level_idx: int, level_price: float,
            price: float, stake: float, op_id: str, state: dict) -> bool:
    """Koop op een grid level.

    BUGFIX 16-07-2026: `price` is toegevoegd als parameter. De hoeveelheid
    werd berekend als stake / level_price, maar er wordt gekocht tegen de
    marktprijs. Die ligt altijd hoger dan het level (de koopconditie is
    price > level), dus werd er structureel tot 1% te veel gekocht.
    """
    grid = state["grids"][symbol]

    key = str(level_idx)
    if key in grid["positions"]:
        return False

    total_inleg = float(state.get("total_inleg", 1795))
    _, max_pos = get_stake_and_max(total_inleg, state)
    max_pos = COIN_MAX_POSITIONS.get(symbol, max_pos)
    if len(grid["positions"]) >= max_pos:
        return False

    try:
        bal = exchange.fetch_balance()
        free = float((bal.get("free") or {}).get("EUR", 0))
        reserve = max(100.0, total_inleg * 0.10)
        if free - stake < reserve:
            LOG.info("SKIP %s | te weinig saldo (free=%.2f reserve=%.2f)",
                     symbol, free, reserve)
            return False
    except Exception as e:
        LOG.warning("Saldo check mislukt: %s", e)
        return False

    try:
        # Delen door de prijs waartegen we daadwerkelijk kopen, niet door het level.
        amount = stake / price
        amount = float(exchange.amount_to_precision(symbol, amount))
        params = {"operatorId": op_id} if op_id else {}
        order  = exchange.create_order(symbol, "market", "buy", amount, None, params)
        actual_price = float(order.get("price") or order.get("average") or price)
        actual_cost  = float(order.get("cost") or stake)
        actual_amt   = float(order.get("filled") or amount)

        sell_at  = actual_price * (1 + SELL_TARGET / 100)
        stop_at  = actual_price * (1 - STOP_LOSS / 100)

        grid["positions"][key] = {
            "amount":     actual_amt,
            "buy_price":  actual_price,
            "buy_cost":   actual_cost,
            "sell_at":    sell_at,
            "stop_at":    stop_at,
            "level":      level_idx,
            "ts":         datetime.now(timezone.utc).isoformat(),
        }
        save_state(state)
        log_trade("BUY", symbol, actual_amt, actual_price, actual_cost)
        LOG.info("KOOP %s | level=%s | prijs=%.4f | stake=%.2f EUR | verkoop@%.4f | stop@%.4f",
                 symbol, key, actual_price, actual_cost, sell_at, stop_at)
        return True

    except Exception as e:
        LOG.error("KOOP MISLUKT %s level %s: %s", symbol, level_idx, e)
        return False


def try_sell(exchange, symbol: str, key: str, pos: dict,
             current_price: float, op_id: str, state: dict, reason: str) -> bool:
    try:
        amount = float(exchange.amount_to_precision(symbol, pos["amount"]))
        if amount <= 0:
            del state["grids"][symbol]["positions"][key]
            save_state(state)
            return False

        params = {"operatorId": op_id} if op_id else {}
        order  = exchange.create_order(symbol, "market", "sell", amount, None, params)
        sell_price = float(order.get("price") or order.get("average") or current_price)
        sell_rev   = sell_price * amount

        fee_buy  = pos["buy_cost"] * (TAKER_FEE / 100)
        fee_sell = sell_rev * (TAKER_FEE / 100)
        pnl      = sell_rev - fee_sell - pos["buy_cost"] - fee_buy

        state["trades"] += 1
        if pnl > 0:
            state["wins"] += 1
        state["pnl"] = round(state.get("pnl", 0) + pnl, 4)
        del state["grids"][symbol]["positions"][key]

        if reason == "stop_loss":
            state.setdefault("cooldowns", {})[symbol] = datetime.now(timezone.utc).isoformat()
            state.setdefault("recent_stop_losses", []).append({
                "symbol": symbol, "ts": datetime.now(timezone.utc).isoformat()
            })
            # Events moeten lang genoeg blijven om een cluster te herkennen
            # zolang de bijbehorende cooldown kan lopen.
            cutoff = datetime.now(timezone.utc).timestamp() - (
                MARKET_WIDE_COOLDOWN_HOURS * 3600 + MARKET_WIDE_SL_WINDOW_MIN * 60
            )
            state["recent_stop_losses"] = [
                e for e in state["recent_stop_losses"]
                if datetime.fromisoformat(e["ts"]).timestamp() >= cutoff
            ]

        save_state(state)

        log_trade("SELL", symbol, amount, sell_price, sell_rev, pnl)
        LOG.info("VERKOOP %s | reden=%s | prijs=%.4f | pnl=%+.4f EUR",
                 symbol, reason, sell_price, pnl)
        return True

    except Exception as e:
        LOG.error("VERKOOP MISLUKT %s key %s: %s", symbol, key, e)
        return False


def manage_coin(exchange, symbol: str, op_id: str, state: dict, skip_new_buys: bool = False):
    """Beheer grid voor één coin.

    LET OP: de paused-check die hier stond is verwijderd. Deze functie moet
    ook draaien tijdens een pauze, anders staan open posities onbeheerd.
    Nieuwe aankopen blokkeren gaat via skip_new_buys."""
    grid = state["grids"].get(symbol)

    try:
        ticker = exchange.fetch_ticker(symbol)
        price  = float(ticker.get("last") or ticker.get("close") or 0)
        change_pct = ticker.get("percentage")
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
        LOG.info("GRID RESET %s | prijs=%.4f buiten range %.4f-%.4f",
                 symbol, price, grid["low"], grid["high"])
        old_positions = grid.get("positions", {})
        setup_grid(symbol, price, state)
        state["grids"][symbol]["positions"] = old_positions
        grid = state["grids"][symbol]

    # Bestaande posities: verkopen of stop-loss. Draait ook tijdens pauze.
    for key in list(grid["positions"].keys()):
        pos = grid["positions"][key]
        if price >= pos["sell_at"]:
            try_sell(exchange, symbol, key, pos, price, op_id, state, "take_profit")
            time.sleep(0.5)
        elif price <= pos["stop_at"]:
            try_sell(exchange, symbol, key, pos, price, op_id, state, "stop_loss")
            time.sleep(0.5)

    all_synced_done = not any(
        p.get("ts", "").startswith("2026-06-30T06:00")
        for g in state.get("grids", {}).values()
        for p in g.get("positions", {}).values()
    )
    if not all_synced_done:
        LOG.info("SKIP KOOP %s | gesyncte posities nog open bij andere coins", symbol)
        return

    total_inleg = float(state.get("total_inleg", 1795))
    stake, _ = get_stake_and_max(total_inleg, state)
    levels = grid.get("levels", [])

    if skip_new_buys:
        return

    if change_pct <= TREND_DROP_LIMIT:
        LOG.info("SKIP KOOP %s | 24u-trend=%+.2f%% (te negatief, geen nieuwe aankopen)", symbol, change_pct)
        return

    cooldown_ts = state.get("cooldowns", {}).get(symbol)
    if cooldown_ts:
        last_sl = datetime.fromisoformat(cooldown_ts)
        hours_since = (datetime.now(timezone.utc) - last_sl).total_seconds() / 3600
        if hours_since < STOP_LOSS_COOLDOWN_HOURS:
            LOG.info("SKIP KOOP %s | cooldown na stop-loss, nog %.1f uur te gaan",
                     symbol, STOP_LOSS_COOLDOWN_HOURS - hours_since)
            return

    for i, level in enumerate(levels[:-1]):
        if abs(price - level) / level < 0.01 and price > level:
            try_buy(exchange, symbol, i, level, price, stake, op_id, state)
            time.sleep(0.3)


def main():
    load_dotenv()
    exchange = ccxt.bitvavo({
        "apiKey":  os.getenv("BITVAVO_API_KEY", "").strip(),
        "secret":  os.getenv("BITVAVO_API_SECRET", "").strip(),
        "enableRateLimit": True,
    })
    exchange.load_markets()
    op_id = os.getenv("BITVAVO_OPERATOR_ID", "").strip()

    state = load_state()

    if "total_inleg" not in state:
        state["total_inleg"] = 1795.0
        save_state(state)

    total_inleg = float(state.get("total_inleg", 1795))
    stake, max_pos = get_stake_and_max(total_inleg, state)
    LOG.info("Diamond Grid Bot v5.2 gestart | total_inleg=%.2f | stake=%d | max_pos=%d "
             "| target=%.1f%% | stop=%.1f%% | bodem=-%.0f EUR",
             total_inleg, stake, max_pos, SELL_TARGET, STOP_LOSS, MAX_TEST_DRAWDOWN)

    while True:
        try:
            state = load_state()
            maybe_bump_weekly_stake(state)
            check_test_drawdown(state)          # harde bodem
            total_inleg = float(state.get("total_inleg", 1795))
            stake, _ = get_stake_and_max(total_inleg, state)

            if state.get("paused", False):
                LOG.info("Bot gepauzeerd: %s | bestaande posities blijven bewaakt",
                         state.get("pause_reason", ""))
                for symbol in GRID_COINS:
                    manage_coin(exchange, symbol, op_id, state, skip_new_buys=True)
                    time.sleep(1)
                time.sleep(LOOP_SLEEP)
                continue

            if market_wide_pause_active(state):
                LOG.info("MARKT-BREDE COOLDOWN actief | geen nieuwe aankopen, bestaande posities lopen door")
                for symbol in GRID_COINS:
                    manage_coin(exchange, symbol, op_id, state, skip_new_buys=True)
                    time.sleep(1)
                LOG.info("Loop klaar (markt-brede cooldown) | pnl=%+.2f | trades=%d",
                         state.get("pnl", 0), state.get("trades", 0))
                time.sleep(LOOP_SLEEP)
                continue

            for symbol in GRID_COINS:
                manage_coin(exchange, symbol, op_id, state)
                time.sleep(1)

            resultaat = float(state.get("pnl", 0)) - float(state.get("test_start_pnl", 0))
            LOG.info("Loop klaar | stake=%.0f | pnl=%+.2f | test=%+.2f/-%.0f | trades=%d",
                     stake, state.get("pnl", 0), resultaat, MAX_TEST_DRAWDOWN,
                     state.get("trades", 0))

        except Exception as e:
            LOG.error("Loop fout: %s", e)

        time.sleep(LOOP_SLEEP)


if __name__ == "__main__":
    main()
