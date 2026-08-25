#!/usr/bin/env python3
import diamond_bot as mod

class News:
    def forced_exit_reason(self, symbol):
        return None

real_enrich = mod.enrich_indicators
mod.enrich_indicators = lambda df, *args: df

def make_case(closed_price, live_bid):
    bot = mod.Bot.__new__(mod.Bot)
    bot.quote = "EUR"
    bot.news = News()
    bot.cfg = {
        "risk": {"min_profit_eur": 0.50},
        "signals": {
            "sma_fast": 20,
            "sma_slow": 60,
            "rsi_len": 14,
            "atr_len": 14,
            "hard_stop_loss_pct": 3.0,
            "trailing_enabled": True,
            "trailing_atr_mult": 1.2,
            "profit_trailing_trigger_pct": 1.0,
            "profit_trailing_pullback_pct": 0.5,
            "exit_on_trend_break": False,
        },
    }

    live = {"bid": live_bid}
    bot.fetch_ohlcv_df = lambda symbol: mod.pd.DataFrame([{
        "close": closed_price,
        "atr": 1.0,
        "sma_fast": 105.0,
        "sma_slow": 100.0,
    }])
    bot.get_ticker = lambda symbol: {"bid": live["bid"]}
    bot.minimum_profitable_exit_price = lambda pos, profit: 101.0

    seen = []
    bot.estimated_exit_pnl_quote = (
        lambda symbol, pos, price:
        seen.append(price) or (price - 100.0)
    )

    position = {
        "entry_price": 100.0,
        "highest_price": 100.0,
        "stop_loss": 97.0,
        "take_profit": 110.0,
        "amount": 1.0,
        "quote_amount": 100.0,
        "fees_buy_quote": 0.25,
    }
    return bot, position, live, seen

try:
    b, p, live, seen = make_case(120.0, 100.2)
    assert b.long_exit_signal("TEST/EUR", p) is None
    assert abs(p["highest_price"] - 100.2) < 1e-9
    assert abs(p["stop_loss"] - 97.0) < 1e-9
    assert abs(seen[0] - 100.2) < 1e-9
    print("PASS | oude hoge candle beïnvloedt high/profit/trailing/TP niet")

    b, p, live, seen = make_case(90.0, 100.2)
    assert b.long_exit_signal("TEST/EUR", p) is None
    assert abs(p["highest_price"] - 100.2) < 1e-9
    assert abs(p["stop_loss"] - 97.0) < 1e-9
    assert abs(seen[0] - 100.2) < 1e-9
    print("PASS | oude lage candle kan SL/profit niet faken")

    b, p, live, seen = make_case(90.0, 103.0)
    assert b.long_exit_signal("TEST/EUR", p) is None
    assert abs(p["highest_price"] - 103.0) < 1e-9
    assert 102.4 < p["stop_loss"] < 102.6

    live["bid"] = 102.4
    assert b.long_exit_signal("TEST/EUR", p) == "trailing_stop"
    print("PASS | echte post-entry rally en pullback activeren trailing")

    print("TOTAL 3/3 PASS")
finally:
    mod.enrich_indicators = real_enrich
