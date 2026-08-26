from __future__ import annotations

import argparse
from dataclasses import dataclass

from bitvavo_public import BitvavoPublic
from config import Settings
from models import Candle
from strategy import BandReentryStrategy


@dataclass(frozen=True)
class BacktestResult:
    market: str
    trades: int
    wins: int
    losses: int
    pnl_eur: float
    profit_factor: float


def run_backtest(market: str, candles: list[Candle], s: Settings) -> BacktestResult:
    if any(not c.is_valid for c in candles):
        raise ValueError(f'{market}: backtest bevat ongeldige candle')
    strategy = BandReentryStrategy(s)
    fee = s.taker_fee_pct/100.0
    slip = s.slippage_pct/100.0
    half_spread = s.backtest_assumed_spread_pct/200.0
    trades = wins = losses = 0
    pnl_total = gross_profit = gross_loss = 0.0
    i = s.band_window

    while i < len(candles)-1:
        decision = strategy.evaluate(candles[:i+1])
        if decision.action != 'BUY':
            i += 1
            continue

        entry_candle = candles[i+1]
        entry_price = entry_candle.open * (1.0 + half_spread + slip)
        notional = s.position_eur
        amount = notional / entry_price
        entry_fee = notional * fee
        stop = entry_price * (1.0-s.stop_loss_pct/100.0)
        take = entry_price * (1.0+s.take_profit_pct/100.0)

        exit_price = None
        exit_idx = None
        # Entry gebeurt op de open van candle i+1. Diezelfde candle is dus de
        # eerste blootstellingsbar en moet direct op stop/take worden gecontroleerd.
        max_end = min(len(candles)-1, i+s.max_hold_bars)
        for j in range(i+1, max_end+1):
            c = candles[j]
            if c.low <= stop:
                exit_price = stop * (1.0-half_spread-slip)
                exit_idx = j
                break
            if c.high >= take:
                exit_price = take * (1.0-half_spread-slip)
                exit_idx = j
                break
        if exit_price is None:
            exit_idx = max_end
            exit_price = candles[exit_idx].close * (1.0-half_spread-slip)

        exit_notional = amount * exit_price
        exit_fee = exit_notional * fee
        pnl = exit_notional - exit_fee - notional - entry_fee
        trades += 1
        pnl_total += pnl
        if pnl > 0:
            wins += 1
            gross_profit += pnl
        elif pnl < 0:
            losses += 1
            gross_loss += -pnl
        i = max(i+1, exit_idx)

    pf = gross_profit/gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    return BacktestResult(market, trades, wins, losses, pnl_total, pf)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=1000)
    args = parser.parse_args()
    s = Settings()
    s.validate()
    api = BitvavoPublic(s.api_base_url, s.request_timeout_seconds, s.request_retries)
    markets = api.top_markets_by_quote_volume(s.quote_currency, s.universe_size)
    print('=== CLEAN-ROOM SANITY BACKTEST ===')
    print(f'Universe: {", ".join(markets)}')
    for market in markets:
        candles = api.closed_candles(market, s.interval, args.limit)
        r = run_backtest(market, candles, s)
        pf = 'INF' if r.profit_factor >= 999 else f'{r.profit_factor:.3f}'
        print(f'{market:12s} trades={r.trades:3d} W/L={r.wins}/{r.losses} pnl=€{r.pnl_eur:+.2f} PF={pf}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
