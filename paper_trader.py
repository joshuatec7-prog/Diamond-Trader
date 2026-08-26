from __future__ import annotations

import math
import time

from config import Settings
from models import Book, Candle, Position, TradeEvent
from storage import Storage


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
        if not book.is_valid:
            return False, 'ongeldig_orderboek'
        if self.db.get_position(market) is not None:
            return False, 'positie_bestaat_al'
        if len(self.db.all_positions()) >= self.s.max_open_positions:
            return False, 'max_open_posities'
        if book.spread_pct > self.s.max_spread_pct:
            return False, 'spread_te_hoog'
        needed = self.s.position_eur * (1.0 + self.fee_rate)
        if not math.isfinite(needed) or needed <= 0:
            return False, 'ongeldige_paper_inzet'
        if self.db.cash_eur() + 1e-9 < needed:
            return False, 'onvoldoende_paper_cash'
        return True, 'ok'

    def open_long(self, market: str, book: Book, candle_ts: int,
                  now_ms: int | None = None) -> TradeEvent | None:
        allowed, reason = self.can_open(market, book)
        if not allowed:
            return None
        entry_price = book.ask * (1.0 + self.slippage_rate)
        entry_notional = self.s.position_eur
        amount = entry_notional / entry_price
        entry_fee = entry_notional * self.fee_rate
        p = Position(
            market=market,
            opened_at_ms=int(time.time()*1000) if now_ms is None else now_ms,
            entry_candle_ts=candle_ts,
            entry_price=entry_price,
            amount=amount,
            entry_notional=entry_notional,
            entry_fee=entry_fee,
            stop_price=entry_price * (1.0 - self.s.stop_loss_pct/100.0),
            take_price=entry_price * (1.0 + self.s.take_profit_pct/100.0),
            bars_held=0,
        )
        self.db.open_position_atomic(p, entry_notional + entry_fee)
        return TradeEvent('OPEN', market, entry_price, 'lower_band_reentry')

    def process_candle(self, market: str, candle: Candle,
                       now_ms: int | None = None) -> TradeEvent | None:
        if not candle.is_valid:
            raise ValueError(f'ongeldige candle voor {market}')
        p = self.db.get_position(market)
        if p is None or candle.timestamp_ms <= p.entry_candle_ts:
            return None
        p.bars_held += 1
        closed_at_ms = int(time.time()*1000) if now_ms is None else now_ms
        if candle.low <= p.stop_price:
            return self._close(p, p.stop_price, 'stop_loss', closed_at_ms)
        if candle.high >= p.take_price:
            return self._close(p, p.take_price, 'take_profit', closed_at_ms)
        if p.bars_held >= self.s.max_hold_bars:
            return self._close(p, candle.close, 'time_exit', closed_at_ms)
        self.db.update_position(p)
        return None

    def _close(self, p: Position, reference_price: float, reason: str, closed_at_ms: int) -> TradeEvent:
        exit_price = reference_price * (1.0 - self.slippage_rate)
        exit_notional = p.amount * exit_price
        exit_fee = exit_notional * self.fee_rate
        net_exit = exit_notional - exit_fee
        pnl_eur = net_exit - p.entry_notional - p.entry_fee
        pnl_pct = pnl_eur / (p.entry_notional + p.entry_fee) * 100.0
        self.db.close_position_atomic(p, closed_at_ms, exit_price, exit_fee, net_exit, pnl_eur, pnl_pct, reason)
        return TradeEvent('CLOSE', p.market, exit_price, reason, pnl_eur)
