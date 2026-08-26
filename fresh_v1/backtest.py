from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import List, Optional

from bitvavo_market import BitvavoMarket
from config import Settings
from models import Candle
from strategy import TrendBreakoutStrategy


@dataclass
class SimPosition:
    entry_price: float
    amount: float
    entry_notional: float
    entry_fee: float
    atr: float
    stop: float
    take: float
    highest: float
    trailing_stop: Optional[float] = None


@dataclass(frozen=True)
class BacktestResult:
    market: str
    trades: int
    wins: int
    losses: int
    pnl_eur: float
    gross_profit: float
    gross_loss: float

    @property
    def pf(self) -> float:
        if self.gross_loss > 0:
            return self.gross_profit / self.gross_loss
        return 999.0 if self.gross_profit > 0 else 0.0


def _buy_fill(price: float, settings: Settings) -> float:
    adverse = (settings.backtest_assumed_spread_pct / 2.0 + settings.slippage_pct) / 100.0
    return price * (1.0 + adverse)


def _sell_fill(price: float, settings: Settings) -> float:
    adverse = (settings.backtest_assumed_spread_pct / 2.0 + settings.slippage_pct) / 100.0
    return price * (1.0 - adverse)


def run_backtest(market: str, candles: List[Candle], settings: Settings) -> BacktestResult:
    strat = TrendBreakoutStrategy(settings)
    fee_rate = settings.taker_fee_pct / 100.0
    position: Optional[SimPosition] = None
    pending_atr: Optional[float] = None
    pnls: List[float] = []

    for i, candle in enumerate(candles):
        if position is None and pending_atr is not None:
            entry = _buy_fill(candle.open, settings)
            notional = settings.stake_eur
            amount = notional / entry
            entry_fee = notional * fee_rate
            position = SimPosition(
                entry_price=entry,
                amount=amount,
                entry_notional=notional,
                entry_fee=entry_fee,
                atr=pending_atr,
                stop=entry - pending_atr * settings.stop_atr_mult,
                take=entry + pending_atr * settings.take_atr_mult,
                highest=entry,
            )
            pending_atr = None

        if position is not None:
            active_stop = max(position.stop, position.trailing_stop) if position.trailing_stop is not None else position.stop
            exit_trigger: Optional[float] = None
            if candle.low <= active_stop:
                exit_trigger = active_stop
            elif candle.high >= position.take:
                exit_trigger = position.take
            if exit_trigger is not None:
                exit_price = _sell_fill(exit_trigger, settings)
                exit_notional = position.amount * exit_price
                exit_fee = exit_notional * fee_rate
                pnls.append(exit_notional - exit_fee - position.entry_notional - position.entry_fee)
                position = None
            else:
                position.highest = max(position.highest, candle.high)
                trigger = position.entry_price + position.atr * settings.trailing_trigger_atr
                if position.highest >= trigger:
                    candidate = position.highest - position.atr * settings.trailing_distance_atr
                    position.trailing_stop = candidate if position.trailing_stop is None else max(candidate, position.trailing_stop)

        if position is None and i + 1 < len(candles):
            signal = strat.evaluate(candles[: i + 1])
            if signal.action == "BUY":
                pending_atr = signal.metrics["atr"]

    if position is not None and candles:
        exit_price = _sell_fill(candles[-1].close, settings)
        exit_notional = position.amount * exit_price
        exit_fee = exit_notional * fee_rate
        pnls.append(exit_notional - exit_fee - position.entry_notional - position.entry_fee)

    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    return BacktestResult(market, len(pnls), wins, losses, sum(pnls), gross_profit, gross_loss)


def main() -> int:
    parser = argparse.ArgumentParser(description="CryptoBot Fresh v1 eenvoudige sanity-backtest")
    parser.add_argument("--limit", type=int, default=1000, choices=range(100, 1441), metavar="100..1440")
    args = parser.parse_args()
    settings = Settings()
    settings.validate()
    api = BitvavoMarket(settings.api_base_url, settings.request_timeout_seconds, settings.request_retries)

    print("=== CRYPTOBOT FRESH v1 BACKTEST ===")
    print(
        f"Kosten: fee {settings.taker_fee_pct:.2f}%/zijde + "
        f"spread {settings.backtest_assumed_spread_pct:.2f}% roundtrip-aanname + "
        f"slippage {settings.slippage_pct:.2f}%/zijde"
    )
    print("Execution: signaal op close, entry pas op volgende candle-open\n")

    results: List[BacktestResult] = []
    for market in settings.markets:
        candles = api.get_closed_candles(market, settings.interval, args.limit)
        result = run_backtest(market, candles, settings)
        results.append(result)
        pf = "INF" if result.pf >= 999 else f"{result.pf:.3f}"
        print(f"{market:10} n={result.trades:3d} W/L={result.wins}/{result.losses} PnL=€{result.pnl_eur:+.2f} PF={pf}")

    trades = sum(r.trades for r in results)
    wins = sum(r.wins for r in results)
    losses = sum(r.losses for r in results)
    pnl = sum(r.pnl_eur for r in results)
    gp = sum(r.gross_profit for r in results)
    gl = sum(r.gross_loss for r in results)
    pf = gp / gl if gl > 0 else (999.0 if gp > 0 else 0.0)
    print(f"\nTOTAAL     n={trades} W/L={wins}/{losses} PnL=€{pnl:+.2f} PF={'INF' if pf >= 999 else f'{pf:.3f}'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
