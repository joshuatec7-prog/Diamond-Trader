from __future__ import annotations

import math
import time

from models import Book, Candle, Position, TradeEvent
from paper_trader import PaperTrader


class AdaptiveLongShortPaperTrader(PaperTrader):
    INITIAL_ATR_MULT = 1.80
    INITIAL_STOP_MIN_PCT = 1.25
    INITIAL_STOP_MAX_PCT = 3.50
    PROTECT_TRIGGER_PCT = 1.50
    LOCK_PROFIT_EUR = 0.50
    TRAIL_ATR_MULT = 2.20
    TRAIL_MIN_PCT = 1.00
    TRAIL_MAX_PCT = 4.00

    @classmethod
    def initial_stop_pct(cls, atr_pct: float) -> float:
        if not math.isfinite(atr_pct) or atr_pct <= 0:
            raise ValueError('ongeldige ATR')
        return min(cls.INITIAL_STOP_MAX_PCT, max(cls.INITIAL_STOP_MIN_PCT, cls.INITIAL_ATR_MULT * atr_pct))

    @classmethod
    def trailing_distance_pct(cls, atr_pct: float) -> float:
        if not math.isfinite(atr_pct) or atr_pct <= 0:
            raise ValueError('ongeldige ATR')
        return min(cls.TRAIL_MAX_PCT, max(cls.TRAIL_MIN_PCT, cls.TRAIL_ATR_MULT * atr_pct))

    def position_side(self, market: str) -> str:
        return (self.db.get_state(f'position_side:{market}', '') or '').upper()

    def _open_atomic(self, p: Position, side: str) -> None:
        total_cost = p.entry_notional + p.entry_fee
        with self.db.conn:
            row = self.db.conn.execute("SELECT value FROM state WHERE key='cash_eur'").fetchone()
            cash = 0.0 if row is None else float(row['value'])
            if cash + 1e-9 < total_cost:
                raise RuntimeError('onvoldoende paper cash')
            if self.db.conn.execute('SELECT 1 FROM positions WHERE market=?', (p.market,)).fetchone():
                raise RuntimeError('positie bestaat al')
            self.db.conn.execute(
                "INSERT INTO state(key,value) VALUES('cash_eur',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f'{cash-total_cost:.12f}',),
            )
            self.db.conn.execute(
                'INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)',
                (p.market,p.opened_at_ms,p.entry_candle_ts,p.entry_price,p.amount,p.entry_notional,
                 p.entry_fee,p.stop_price,p.take_price,p.bars_held),
            )
            self.db.conn.execute(
                'INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                (f'position_side:{p.market}', side),
            )

    def open_directional(self, side: str, market: str, book: Book, candle_ts: int, atr_pct: float,
                         now_ms: int | None = None) -> TradeEvent | None:
        allowed, _ = self.can_open(market, book)
        if not allowed:
            return None
        side = side.upper()
        stop_pct = self.initial_stop_pct(atr_pct)
        entry_notional = self.s.position_eur
        entry_fee = entry_notional * self.fee_rate
        if side == 'LONG':
            entry_price = book.ask * (1.0 + self.slippage_rate)
            stop_price = entry_price * (1.0 - stop_pct / 100.0)
            take_price = entry_price * 2.0
        elif side == 'SHORT':
            entry_price = book.bid * (1.0 - self.slippage_rate)
            stop_price = entry_price * (1.0 + stop_pct / 100.0)
            take_price = entry_price * 0.5
        else:
            raise ValueError('side moet LONG of SHORT zijn')
        amount = entry_notional / entry_price
        p = Position(market, int(time.time()*1000) if now_ms is None else now_ms, candle_ts,
                     entry_price, amount, entry_notional, entry_fee, stop_price, take_price, 0)
        self._open_atomic(p, side)
        return TradeEvent('OPEN', market, entry_price, f'adaptive_v2_{side.lower()}')

    def _persist_stop(self, p: Position, new_stop: float) -> None:
        with self.db.conn:
            cur = self.db.conn.execute('UPDATE positions SET stop_price=? WHERE market=? AND opened_at_ms=?',
                                       (new_stop, p.market, p.opened_at_ms))
            if cur.rowcount != 1:
                raise RuntimeError('positie niet gevonden bij stop-update')
        p.stop_price = new_stop

    def _lock_long_reference(self, p: Position) -> float:
        target_net_exit = p.entry_notional + p.entry_fee + self.LOCK_PROFIT_EUR
        return target_net_exit / (p.amount * (1.0-self.slippage_rate) * (1.0-self.fee_rate))

    def _lock_short_reference(self, p: Position) -> float:
        target_cover = (p.amount*p.entry_price - p.entry_fee - self.LOCK_PROFIT_EUR) / (p.amount*(1.0+self.fee_rate))
        return target_cover / (1.0+self.slippage_rate)

    def _protected(self, p: Position, side: str) -> bool:
        return p.stop_price > p.entry_price if side == 'LONG' else p.stop_price < p.entry_price

    def _raise_or_lower_stop(self, p: Position, side: str, book: Book, atr_pct: float) -> None:
        trail = self.trailing_distance_pct(atr_pct)
        if side == 'LONG':
            if book.bid < p.entry_price * (1.0 + self.PROTECT_TRIGGER_PCT/100.0):
                return
            candidate = max(p.stop_price, self._lock_long_reference(p), book.bid*(1.0-trail/100.0))
            candidate = min(candidate, book.bid*(1.0-1e-9))
            if candidate > p.stop_price*(1.0+1e-10):
                self._persist_stop(p, candidate)
        else:
            if book.ask > p.entry_price * (1.0 - self.PROTECT_TRIGGER_PCT/100.0):
                return
            candidate = min(p.stop_price, self._lock_short_reference(p), book.ask*(1.0+trail/100.0))
            candidate = max(candidate, book.ask*(1.0+1e-9))
            if candidate < p.stop_price*(1.0-1e-10):
                self._persist_stop(p, candidate)

    def _close_atomic(self, p: Position, side: str, closed_at_ms: int, exit_price: float,
                      exit_fee: float, cash_return: float, pnl_eur: float, pnl_pct: float,
                      reason: str) -> None:
        with self.db.conn:
            row = self.db.conn.execute("SELECT value FROM state WHERE key='cash_eur'").fetchone()
            cash = 0.0 if row is None else float(row['value'])
            self.db.conn.execute(
                "INSERT INTO state(key,value) VALUES('cash_eur',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f'{cash+cash_return:.12f}',),
            )
            self.db.conn.execute(
                '''INSERT INTO trades(market,opened_at_ms,closed_at_ms,entry_price,exit_price,amount,
                   entry_fee,exit_fee,pnl_eur,pnl_pct,exit_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (p.market,p.opened_at_ms,closed_at_ms,p.entry_price,exit_price,p.amount,p.entry_fee,
                 exit_fee,pnl_eur,pnl_pct,reason),
            )
            self.db.conn.execute('DELETE FROM positions WHERE market=?', (p.market,))
            self.db.conn.execute('DELETE FROM state WHERE key=?', (f'position_side:{p.market}',))

    def _close(self, p: Position, side: str, reference: float, reason: str, closed_at_ms: int) -> TradeEvent:
        if side == 'LONG':
            exit_price = reference * (1.0-self.slippage_rate)
            exit_notional = p.amount*exit_price
            exit_fee = exit_notional*self.fee_rate
            cash_return = exit_notional-exit_fee
            pnl = cash_return-p.entry_notional-p.entry_fee
        else:
            exit_price = reference * (1.0+self.slippage_rate)
            exit_notional = p.amount*exit_price
            exit_fee = exit_notional*self.fee_rate
            gross = p.amount*(p.entry_price-exit_price)
            pnl = gross-p.entry_fee-exit_fee
            cash_return = p.entry_notional+gross-exit_fee
        pnl_pct = pnl/(p.entry_notional+p.entry_fee)*100.0
        full_reason = f'adaptive_v2_{side.lower()}_{reason}'
        self._close_atomic(p, side, closed_at_ms, exit_price, exit_fee, cash_return, pnl, pnl_pct, full_reason)
        return TradeEvent('CLOSE', p.market, exit_price, full_reason, pnl)

    def process_book(self, market: str, book: Book, atr_pct: float, now_ms: int | None = None) -> TradeEvent | None:
        if not book.is_valid:
            raise ValueError('ongeldig orderboek')
        p = self.db.get_position(market)
        if p is None:
            return None
        side = self.position_side(market)
        if side not in {'LONG','SHORT'}:
            raise RuntimeError(f'ontbrekende position side voor {market}')
        now = int(time.time()*1000) if now_ms is None else now_ms
        if side == 'LONG' and book.bid <= p.stop_price:
            return self._close(p, side, min(book.bid,p.stop_price), 'trailing_stop' if self._protected(p,side) else 'stop_loss', now)
        if side == 'SHORT' and book.ask >= p.stop_price:
            return self._close(p, side, max(book.ask,p.stop_price), 'trailing_stop' if self._protected(p,side) else 'stop_loss', now)
        self._raise_or_lower_stop(p, side, book, atr_pct)
        return None

    def process_candle(self, market: str, candle: Candle, now_ms: int | None = None) -> TradeEvent | None:
        p = self.db.get_position(market)
        if p is None or candle.timestamp_ms <= p.entry_candle_ts:
            return None
        side = self.position_side(market)
        if side not in {'LONG','SHORT'}:
            raise RuntimeError(f'ontbrekende position side voor {market}')
        p.bars_held += 1
        now = int(time.time()*1000) if now_ms is None else now_ms
        hit = candle.low <= p.stop_price if side == 'LONG' else candle.high >= p.stop_price
        if hit:
            return self._close(p, side, p.stop_price, 'trailing_stop' if self._protected(p,side) else 'stop_loss', now)
        if p.bars_held >= self.s.max_hold_bars and not self._protected(p,side):
            return self._close(p, side, candle.close, 'time_exit', now)
        self.db.update_position(p)
        return None

    def close_trend_break(self, market: str, reference_price: float, reason: str,
                          now_ms: int | None = None) -> TradeEvent | None:
        p = self.db.get_position(market)
        if p is None:
            return None
        side = self.position_side(market)
        now = int(time.time()*1000) if now_ms is None else now_ms
        return self._close(p, side, reference_price, reason, now)
