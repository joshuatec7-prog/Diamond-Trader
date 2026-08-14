#!/usr/bin/env python3
# Diamond Trader Rollback Offline Drill v1.0
#
# Simuleert rollback/recovery situaties volledig offline.
# Geen echte orders, geen private exchange-API, geen live/config wijziging.

from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
from pathlib import Path


VERSION = "1.0"
MODULE_PATH = Path(__file__).resolve().with_name("diamond_bot.py")


# Minimal ccxt stub zodat productiecode offline geladen kan worden.
ccxt = types.ModuleType("ccxt")


class OrderNotFound(Exception):
    pass


class Exchange:
    pass


ccxt.OrderNotFound = OrderNotFound
ccxt.Exchange = Exchange
ccxt.bitvavo = lambda *args, **kwargs: None
sys.modules["ccxt"] = ccxt


spec = importlib.util.spec_from_file_location(
    "diamond_bot_rollback_drill",
    str(MODULE_PATH),
)
if spec is None or spec.loader is None:
    raise RuntimeError("diamond_bot.py kon niet offline worden geladen")

mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.time.sleep = lambda *_args, **_kwargs: None


class FakeNews:
    def buy_gate(self, symbol):
        return {"allow": True, "reason": "offline_drill"}

    def coin_news(self, symbol):
        return {"news_score": 0.0}

    def fear_greed(self):
        return {"value": 50}


class FakeExchange:
    def __init__(self):
        self.create_calls = []
        self.fetch_calls = []
        self.orders = {}
        self.fetch_error = None

    def create_order(
        self,
        symbol,
        order_type,
        side,
        amount,
        price,
        params,
    ):
        self.create_calls.append(
            (
                symbol,
                order_type,
                side,
                amount,
                price,
                dict(params or {}),
            )
        )
        client_id = (params or {}).get("clientOrderId")
        fill_price = 100.0 if side == "buy" else 110.0
        order = {
            "id": f"offline-{len(self.create_calls)}",
            "symbol": symbol,
            "status": "closed",
            "side": side,
            "filled": amount,
            "amount": amount,
            "average": fill_price,
            "price": fill_price,
            "cost": amount * fill_price,
            "clientOrderId": client_id,
            "fee": {
                "cost": amount * fill_price * 0.0025,
                "currency": "EUR",
            },
            "info": {"clientOrderId": client_id},
        }
        if client_id:
            self.orders[client_id] = dict(order)
        return order

    def fetch_order(self, order_id, symbol=None, params=None):
        self.fetch_calls.append(
            (order_id, symbol, dict(params or {}))
        )
        if self.fetch_error is not None:
            raise self.fetch_error
        client_id = (params or {}).get("clientOrderId") or order_id
        if client_id not in self.orders:
            raise OrderNotFound(client_id)
        return dict(self.orders[client_id])


def make_bot(root, exchange=None, state=None, dry_run=False):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)

    bot = mod.Bot.__new__(mod.Bot)
    bot.cfg = {
        "quote": "EUR",
        "risk": {
            "dry_run": bool(dry_run),
            "fixed_stake_quote": 35,
            "max_open_positions": 5,
            "eur_reserve": 250,
            "taker_fee_pct": 0.25,
            "avoid_symbols_with_existing_balance": False,
        },
        "trading": {
            "enable_spot": True,
            "max_total_positions": 5,
            "allow_long_and_short_same_symbol": False,
        },
        "signals": {},
        "news": {},
        "fees": {"taker_fee_pct": 0.25},
    }
    bot.quote = "EUR"
    bot.dry_run = bool(dry_run)
    bot.state_file = str(root / "state.json")
    bot.trades_file = str(root / "transactions.csv")
    bot.canary_execution_file = str(root / "canary.csv")
    bot.control_file = str(root / "control.json")
    bot.short_test_baseline_file = str(root / "short_base.json")
    bot.short_test_report_file = str(root / "short_report.json")
    bot.short_execution_file = str(root / "short_exec.csv")
    bot.short_test_archive_dir = str(root / "short_archive")
    bot.state = state if state is not None else mod.default_state()
    bot.last_status_log_ts = 0.0
    bot.last_hold_log_ts = {}
    bot.last_skip_log_ts = {}
    bot.balance_cache = {
        "free": {"EUR": 1000.0, "BTC": 1.0},
        "total": {"EUR": 1000.0, "BTC": 1.0},
    }
    bot.api_key = "offline"
    bot.api_secret = "offline"
    bot.operator_id = ""
    bot.exchange = exchange or FakeExchange()
    bot.news = FakeNews()
    bot.short_strategy_baseline_mismatch = False

    bot.refresh_balance_cache = lambda: None
    bot.get_ticker = lambda symbol: {
        "bid": 110.0,
        "ask": 100.0,
        "last": 105.0,
    }
    bot.estimate_spread_pct = lambda ticker: 0.05
    bot.market_min_notional = lambda symbol: 5.0
    bot.amount_to_precision_safe = (
        lambda symbol, amount: float(amount)
    )
    bot.asset_balance = lambda asset: {
        "EUR": 1000.0,
        "BTC": 1.0,
    }.get(str(asset).upper(), 0.0)
    bot.rate_limited_info = lambda *args, **kwargs: None

    mod.save_state(bot.state_file, bot.state)
    return bot


SIGNAL = {
    "signal_candle_ts": "2026-08-14T07:00:00Z",
    "close": 100.0,
    "stop_loss": 95.0,
    "take_profit": 110.0,
    "tech_score": 2.0,
    "rsi": 60.0,
    "atr_pct": 1.0,
}


def bot_position():
    return {
        "opened_by_bot": True,
        "opened_at": 1000.0,
        "entry_price": 100.0,
        "amount": 0.35,
        "quote_amount": 35.0,
        "fees_buy_quote": 0.0875,
        "protected_base_amount": 0.0,
        "canary_trade_number": 1,
        "entry_reference_ask": 100.0,
        "entry_slippage_pct": 0.0,
        "entry_spread_pct": 0.05,
        "exchange_order_id": "buy-1",
        "client_order_id": "client-buy-1",
    }


RESULTS = []


def case(name, fn):
    try:
        detail = fn()
        RESULTS.append((name, True, detail or "OK"))
    except Exception as exc:
        RESULTS.append(
            (
                name,
                False,
                f"{type(exc).__name__}: {exc}",
            )
        )


def drill_1_recovery_blocks_new_entry():
    with tempfile.TemporaryDirectory() as td:
        exchange = FakeExchange()
        bot = make_bot(td, exchange)
        bot.state["recovery_required"] = True
        bot.state["recovery_reason"] = "offline rollback drill"
        mod.save_state(bot.state_file, bot.state)

        before = len(exchange.create_calls)
        bot.try_buy_symbol(
            "BTC/EUR",
            precomputed_signal=SIGNAL,
            precomputed_news_gate={"allow": True},
            precomputed_ticker={"bid": 99.9, "ask": 100.0},
            precomputed_spread_pct=0.05,
        )

        assert len(exchange.create_calls) == before
        return "recovery_required -> geen nieuwe BUY"


case(
    "1. Recovery gate blokkeert nieuwe entry",
    drill_1_recovery_blocks_new_entry,
)


def drill_2_pending_buy_survives_uncertain_restart():
    with tempfile.TemporaryDirectory() as td:
        exchange = FakeExchange()
        bot = make_bot(td, exchange)

        key = bot.long_order_key("BTC/EUR", SIGNAL)
        record = bot.prepare_pending_long_order(
            key,
            "BTC/EUR",
            SIGNAL,
            35,
            0.05,
            0.0,
        )
        bot.mark_pending_submitting(key)
        client_id = record["clientOrderId"]

        exchange.orders[client_id] = {
            "id": "buy-open",
            "status": "open",
            "filled": 0.0,
            "amount": 0.35,
            "clientOrderId": client_id,
            "info": {"clientOrderId": client_id},
        }

        restarted = make_bot(
            td,
            exchange,
            mod.load_state(bot.state_file),
        )
        restarted.reconcile_pending_orders()

        assert key in restarted.state["pending_orders"]
        assert restarted.state["recovery_required"] is True
        assert restarted.entries_blocked_by_recovery() is True
        return "onzekere BUY blijft pending; restart houdt bot geblokkeerd"


case(
    "2. Pending BUY + restart blijft veilig geblokkeerd",
    drill_2_pending_buy_survives_uncertain_restart,
)


def drill_3_pending_sell_blocks_duplicate():
    with tempfile.TemporaryDirectory() as td:
        exchange = FakeExchange()
        bot = make_bot(td, exchange)
        pos = bot_position()
        bot.state["positions"]["BTC/EUR"] = pos
        mod.save_state(bot.state_file, bot.state)

        key = bot.sell_order_key(
            "BTC/EUR",
            pos,
            "take_profit",
            0.35,
        )
        bot.prepare_pending_sell_order(
            key,
            "BTC/EUR",
            pos,
            "take_profit",
            0.35,
        )

        before = len(exchange.create_calls)
        bot.try_sell_symbol(
            "BTC/EUR",
            pos,
            "take_profit",
        )

        assert len(exchange.create_calls) == before
        assert bot.state["recovery_required"] is True
        return "pending SELL -> tweede SELL niet verstuurd"


case(
    "3. Pending SELL blokkeert duplicate SELL",
    drill_3_pending_sell_blocks_duplicate,
)


def drill_4_state_mismatch_stays_paused():
    with tempfile.TemporaryDirectory() as td:
        exchange = FakeExchange()
        bot = make_bot(td, exchange)
        pos = bot_position()

        key = bot.sell_order_key(
            "BTC/EUR",
            pos,
            "take_profit",
            0.35,
        )
        record = bot.prepare_pending_sell_order(
            key,
            "BTC/EUR",
            pos,
            "take_profit",
            0.35,
        )
        record["reference_bid"] = 110.0
        record["execution_spread_pct"] = 0.05
        mod.save_state(bot.state_file, bot.state)
        bot.mark_pending_submitting(key)

        client_id = record["clientOrderId"]
        exchange.orders[client_id] = {
            "id": "sell-filled",
            "status": "closed",
            "filled": 0.35,
            "average": 109.9,
            "cost": 38.465,
            "clientOrderId": client_id,
            "fee": {"cost": 0.0961625, "currency": "EUR"},
            "info": {"clientOrderId": client_id},
        }

        # Bewust geen lokale positie: simuleert state/balance mismatch.
        restarted = make_bot(
            td,
            exchange,
            mod.load_state(bot.state_file),
        )
        restarted.reconcile_pending_orders()

        assert restarted.state["recovery_required"] is True
        assert key in restarted.state["pending_orders"]
        assert "sell_position_missing" in restarted.state["recovery_reason"]
        return "state mismatch -> recovery blijft verplicht"


case(
    "4. State/position mismatch blijft in recovery",
    drill_4_state_mismatch_stays_paused,
)


def drill_5_unknown_coin_is_never_sold():
    with tempfile.TemporaryDirectory() as td:
        exchange = FakeExchange()
        bot = make_bot(td, exchange)
        unknown = {
            "opened_by_bot": False,
            "amount": 1.0,
            "opened_at": 1.0,
            "quote_amount": 100.0,
            "fees_buy_quote": 0.0,
        }

        bot.try_sell_symbol(
            "BTC/EUR",
            unknown,
            "rollback_test",
        )

        assert len(exchange.create_calls) == 0
        return "niet-bot bezit -> 0 SELL orders"


case(
    "5. Rollback verkoopt onbekend bezit nooit",
    drill_5_unknown_coin_is_never_sold,
)


def drill_6_dry_run_cannot_create_private_order():
    with tempfile.TemporaryDirectory() as td:
        exchange = FakeExchange()
        bot = make_bot(td, exchange, dry_run=True)

        order = bot.place_market_buy(
            "BTC/EUR",
            35.0,
        )

        assert len(exchange.create_calls) == 0
        assert str(order.get("id", "")).startswith("drybuy-")
        return "dry_run=True -> alleen lokale simulatie, 0 exchange create_order"


case(
    "6. Hard rollback naar dry-run voorkomt echte order",
    drill_6_dry_run_cannot_create_private_order,
)


def drill_7_sell_recovery_exact_once_then_clear():
    with tempfile.TemporaryDirectory() as td:
        exchange = FakeExchange()
        bot = make_bot(td, exchange)
        pos = bot_position()
        bot.state["positions"]["BTC/EUR"] = pos
        mod.save_state(bot.state_file, bot.state)

        key = bot.sell_order_key(
            "BTC/EUR",
            pos,
            "take_profit",
            0.35,
        )
        record = bot.prepare_pending_sell_order(
            key,
            "BTC/EUR",
            pos,
            "take_profit",
            0.35,
        )
        record["reference_bid"] = 110.0
        record["execution_spread_pct"] = 0.05
        mod.save_state(bot.state_file, bot.state)
        bot.mark_pending_submitting(key)

        client_id = record["clientOrderId"]
        exchange.orders[client_id] = {
            "id": "sell-recovered",
            "status": "closed",
            "filled": 0.35,
            "average": 109.9,
            "cost": 38.465,
            "clientOrderId": client_id,
            "fee": {"cost": 0.0961625, "currency": "EUR"},
            "info": {"clientOrderId": client_id},
        }

        restarted = make_bot(
            td,
            exchange,
            mod.load_state(bot.state_file),
        )
        restarted.reconcile_pending_orders()

        trades_after_first = restarted.state["trades"]
        pnl_after_first = restarted.state["pnl_quote"]

        restarted.reconcile_pending_orders()

        assert restarted.state["trades"] == trades_after_first == 1
        assert restarted.state["pnl_quote"] == pnl_after_first
        assert restarted.state["pending_orders"] == {}
        assert restarted.state["recovery_required"] is False
        assert "BTC/EUR" not in restarted.state["positions"]
        return "SELL recovery exact één keer; pending/recovery daarna vrij"


case(
    "7. Recovery verwerkt SELL exact één keer",
    drill_7_sell_recovery_exact_once_then_clear,
)


def drill_8_resume_only_after_recovery_clear():
    with tempfile.TemporaryDirectory() as td:
        exchange = FakeExchange()
        bot = make_bot(td, exchange)

        bot.state["recovery_required"] = True
        bot.state["recovery_reason"] = "test pause"
        mod.save_state(bot.state_file, bot.state)

        assert bot.entries_blocked_by_recovery() is True

        bot.state["recovery_required"] = False
        bot.state["recovery_reason"] = ""
        bot.state["pending_orders"] = {}
        mod.save_state(bot.state_file, bot.state)

        restarted = make_bot(
            td,
            exchange,
            mod.load_state(bot.state_file),
        )

        assert restarted.entries_blocked_by_recovery() is False
        return "resume pas mogelijk nadat pending leeg en recovery false zijn"


case(
    "8. Hervatten alleen na schone recovery-state",
    drill_8_resume_only_after_recovery_clear,
)


print("=" * 78)
print(f" DIAMOND ROLLBACK OFFLINE DRILL v{VERSION}")
print("=" * 78)

for name, ok, detail in RESULTS:
    mark = "PASS" if ok else "FAIL"
    print(f"{mark} | {name} | {detail}")

passed = sum(1 for _name, ok, _detail in RESULTS if ok)
total = len(RESULTS)

print()
print(f"TOTAL {passed}/{total} PASS")
print("Echte orders        : NEE")
print("Private API         : NEE")
print("Live/config wijziging: NEE")

if passed != total:
    raise SystemExit(1)
