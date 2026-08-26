from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Optional

from config import Settings
from models import Book, Candle, Position
from storage import Storage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradeEvent:
    kind: str
    market: str
    price: float
    reason: str
    pnl_eur: Optional[float] = None


class PaperTrader:
    def __init__(self, settings: Settings, storage: Storage) -> None:
        self.s = settings
        self.db = storage

    @property
    def fee_rate(self) -> float:
        return self.s.taker_fee_pct / 100.0

    @property
    def slippage_rate(self) -> float:
        return self.s.slippage_pct / 100.0

    def can_open(self, market: str, book: Book) -> tuple[bool, str]:
        if self.db.get_position(market) is not None:
            return False, "positie_bestaat_al"
        if len(self.db.all_positions()) >= self.s.max_open_positions:
            return False, "max_open_posities"
        if book.spread_pct > self.s.max_spread_pct:
            return False, "spread_te_hoog"
        min_cash = self.s.stake_eur * (1.0 + self.fee_rate)
        if self.db.cash_eur() + 1e-9 < min_cash:
            return False, "onvoldoende_paper_cash"
        return True, "ok"

    def open_long(
        self,
        market: str,
        book: Book,
        atr_value: float,
        candle_ts: int,
        now_ms: int | None = None,
    ) -> Optional[TradeEvent]:
        if not math.isfinite(atr_value) or atr_value <= 0:
            logger.error("%s BUY geblokkeerd: ongeldige_atr", market)
            return None
        allowed, reason = self.can_open(market, book)
        if not allowed:
            logger.info("%s BUY geblokkeerd: %s", market, reason)
            return None

        entry_price = book.ask * (1.0 + self.slippage_rate)
        entry_notional = self.s.stake_eur
        amount = entry_notional / entry_price
        entry_fee = entry_notional * self.fee_rate
        total_cost = entry_notional + entry_fee
        p = Position(
            market=market,
            opened_at_ms=int(time.time() * 1000) if now_ms is None else now_ms,
            entry_candle_ts=candle_ts,
            entry_price=entry_price,
            amount=amount,
            entry_notional=entry_notional,
            entry_fee=entry_fee,
            atr_at_entry=atr_value,
            stop_price=max(1e-12, entry_price - (atr_value * self.s.stop_atr_mult)),
            take_price=entry_price + (atr_value * self.s.take_atr_mult),
            highest_price=entry_price,
            trailing_active=False,
            trailing_stop=None,
        )
        self.db.open_position_atomic(p, total_cost)
        return TradeEvent("OPEN", market, entry_price, "trend_breakout")

    def process_candle(self, market: str, candle: Candle, now_ms: int | None = None) -> Optional[TradeEvent]:
        p = self.db.get_position(market)
        if p is None or candle.timestamp_ms <= p.entry_candle_ts:
            return None
        active_stop = p.stop_price
        if p.trailing_active and p.trailing_stop is not None:
            active_stop = max(active_stop, p.trailing_stop)
        if candle.low <= active_stop:
            return self._close(p, active_stop, "stop", now_ms or candle.timestamp_ms)
        if candle.high >= p.take_price:
            return self._close(p, p.take_price, "take_profit", now_ms or candle.timestamp_ms)
        new_high = max(p.highest_price, candle.high)
        trigger = p.entry_price + (p.atr_at_entry * self.s.trailing_trigger_atr)
        if new_high >= trigger:
            new_trailing = new_high - (p.atr_at_entry * self.s.trailing_distance_atr)
            if p.trailing_stop is not None:
                new_trailing = max(new_trailing, p.trailing_stop)
            p.trailing_active = True
            p.trailing_stop = new_trailing
        p.highest_price = new_high
        self.db.upsert_position(p)
        return None

    def _close(self, p: Position, trigger_price: float, reason: str, closed_at_ms: int) -> TradeEvent:
        exit_price = trigger_price * (1.0 - self.slippage_rate)
        exit_notional = p.amount * exit_price
        exit_fee = exit_notional * self.fee_rate
        net_exit = exit_notional - exit_fee
        pnl_eur = net_exit - p.entry_notional - p.entry_fee
        pnl_pct = (pnl_eur / (p.entry_notional + p.entry_fee)) * 100.0
        self.db.close_position_atomic(
            p=p,
            closed_at_ms=closed_at_ms,
            exit_price=exit_price,
            exit_fee=exit_fee,
            net_exit=net_exit,
            pnl_eur=pnl_eur,
            pnl_pct=pnl_pct,
            exit_reason=reason,
        )
        return TradeEvent("CLOSE", p.market, exit_price, reason, pnl_eur)
