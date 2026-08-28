from __future__ import annotations

import math
import time

from models import Book, Candle, Position, TradeEvent
from paper_trader import PaperTrader


class AdaptiveTrendPaperTrader(PaperTrader):
    """PAPER trader met ATR-afhankelijke stop en trailing zonder harde TP."""

    INITIAL_ATR_MULT = 1.80
    INITIAL_STOP_MIN_PCT = 1.25
    INITIAL_STOP_MAX_PCT = 3.50

    PROTECT_TRIGGER_PCT = 1.50
    LOCK_PROFIT_EUR = 0.50
    TRAIL_ATR_MULT = 2.20
    TRAIL_MIN_PCT = 1.00
    TRAIL_MAX_PCT = 4.00

    def __init__(self, settings, storage, entry_reason: str = 'adaptive_trend_follow') -> None:
        super().__init__(settings, storage, entry_reason=entry_reason)

    @classmethod
    def initial_stop_pct(cls, atr_pct: float) -> float:
        if not math.isfinite(atr_pct) or atr_pct <= 0:
            raise ValueError('ongeldige ATR voor adaptive initial stop')
        return min(cls.INITIAL_STOP_MAX_PCT, max(cls.INITIAL_STOP_MIN_PCT, cls.INITIAL_ATR_MULT * atr_pct))

    @classmethod
    def trailing_distance_pct(cls, atr_pct: float) -> float:
        if not math.isfinite(atr_pct) or atr_pct <= 0:
            raise ValueError('ongeldige ATR voor adaptive trailing')
        return min(cls.TRAIL_MAX_PCT, max(cls.TRAIL_MIN_PCT, cls.TRAIL_ATR_MULT * atr_pct))

    def _lock_reference_price(self, p: Position) -> float:
        target_net_exit = p.entry_notional + p.entry_fee + self.LOCK_PROFIT_EUR
        denominator = p.amount * (1.0 - self.slippage_rate) * (1.0 - self.fee_rate)
        if denominator <= 0:
            raise ValueError('ongeldige positie voor adaptive winstlock')
        return target_net_exit / denominator

    def _persist_stop(self, p: Position, new_stop: float) -> None:
        if not math.isfinite(new_stop) or new_stop <= 0:
            raise ValueError('ongeldige adaptive stop')
        with self.db.conn:
            cur = self.db.conn.execute(
                'UPDATE positions SET stop_price=? WHERE market=? AND opened_at_ms=?',
                (new_stop, p.market, p.opened_at_ms),
            )
            if cur.rowcount != 1:
                raise RuntimeError('positie niet gevonden tijdens adaptive stop update')
        p.stop_price = new_stop

    @staticmethod
    def _is_protected(p: Position) -> bool:
        return p.stop_price > p.entry_price * (1.0 + 1e-10)

    def open_long_adaptive(
        self,
        market: str,
        book: Book,
        candle_ts: int,
        atr_pct: float,
        now_ms: int | None = None,
    ) -> TradeEvent | None:
        allowed, _reason = self.can_open(market, book)
        if not allowed:
            return None

        entry_price = book.ask * (1.0 + self.slippage_rate)
        entry_notional = self.s.position_eur
        amount = entry_notional / entry_price
        entry_fee = entry_notional * self.fee_rate
        stop_distance = self.initial_stop_pct(atr_pct)
        p = Position(
            market=market,
            opened_at_ms=int(time.time() * 1000) if now_ms is None else now_ms,
            entry_candle_ts=candle_ts,
            entry_price=entry_price,
            amount=amount,
            entry_notional=entry_notional,
            entry_fee=entry_fee,
            stop_price=entry_price * (1.0 - stop_distance / 100.0),
            take_price=entry_price * 2.0,
            bars_held=0,
        )
        self.db.open_position_atomic(p, entry_notional + entry_fee)
        return TradeEvent('OPEN', market, entry_price, self.entry_reason)

    def _maybe_raise_stop(self, p: Position, bid: float, atr_pct: float) -> bool:
        trigger = p.entry_price * (1.0 + self.PROTECT_TRIGGER_PCT / 100.0)
        if bid < trigger:
            return False

        trail_pct = self.trailing_distance_pct(atr_pct)
        candidate = max(
            p.stop_price,
            self._lock_reference_price(p),
            bid * (1.0 - trail_pct / 100.0),
        )
        candidate = min(candidate, bid * (1.0 - 1e-9))
        if candidate <= p.stop_price * (1.0 + 1e-10):
            return False
        self._persist_stop(p, candidate)
        return True

    def process_book(
        self,
        market: str,
        book: Book,
        atr_pct: float,
        now_ms: int | None = None,
    ) -> TradeEvent | None:
        if not book.is_valid:
            raise ValueError(f'ongeldig orderboek voor {market}')
        p = self.db.get_position(market)
        if p is None:
            return None
        closed_at_ms = int(time.time() * 1000) if now_ms is None else now_ms

        if book.bid <= p.stop_price:
            reference = min(book.bid, p.stop_price)
            reason = 'adaptive_trailing_stop' if self._is_protected(p) else 'adaptive_stop_loss'
            return self._close(p, reference, reason, closed_at_ms)

        self._maybe_raise_stop(p, book.bid, atr_pct)
        return None

    def process_candle(
        self,
        market: str,
        candle: Candle,
        now_ms: int | None = None,
    ) -> TradeEvent | None:
        if not candle.is_valid:
            raise ValueError(f'ongeldige candle voor {market}')
        p = self.db.get_position(market)
        if p is None or candle.timestamp_ms <= p.entry_candle_ts:
            return None

        p.bars_held += 1
        closed_at_ms = int(time.time() * 1000) if now_ms is None else now_ms
        if candle.low <= p.stop_price:
            reason = 'adaptive_trailing_stop' if self._is_protected(p) else 'adaptive_stop_loss'
            return self._close(p, p.stop_price, reason, closed_at_ms)
        if p.bars_held >= self.s.max_hold_bars and not self._is_protected(p):
            return self._close(p, candle.close, 'adaptive_time_exit', closed_at_ms)

        self.db.update_position(p)
        return None

    def close_trend_break(
        self,
        market: str,
        reference_price: float,
        reason: str,
        now_ms: int | None = None,
    ) -> TradeEvent | None:
        p = self.db.get_position(market)
        if p is None:
            return None
        if not math.isfinite(reference_price) or reference_price <= 0:
            raise ValueError('ongeldige adaptive trend-break prijs')
        closed_at_ms = int(time.time() * 1000) if now_ms is None else now_ms
        return self._close(p, reference_price, reason, closed_at_ms)
