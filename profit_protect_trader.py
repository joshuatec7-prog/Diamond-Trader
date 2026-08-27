from __future__ import annotations

import math
import time

from models import Book, Candle, Position, TradeEvent
from paper_trader import PaperTrader


class ProfitProtectPaperTrader(PaperTrader):
    """PAPER long trader met winstbescherming na een aantoonbare plusbeweging.

    De vaste take-profit blijft bestaan. Zodra de uitvoerbare biedprijs de
    trigger haalt, wordt de stop alleen omhoog geschoven. De eerste verhoging
    mikt bij normale stop-uitvoering op minimaal `lock_profit_eur` netto PnL;
    daarna volgt de stop de biedprijs op `trail_distance_pct` afstand.

    Een neerwaartse gap kan lager uitvoeren dan de beschermde stop en daarmee
    de bedoelde minimumwinst verminderen. Dat is bewust conservatief gemodelleerd.
    """

    def __init__(
        self,
        settings,
        storage,
        entry_reason: str,
        trigger_pct: float = 1.50,
        lock_profit_eur: float = 0.50,
        trail_distance_pct: float = 0.75,
    ) -> None:
        super().__init__(settings, storage, entry_reason=entry_reason)
        values = (trigger_pct, lock_profit_eur, trail_distance_pct)
        if not all(math.isfinite(v) for v in values):
            raise ValueError('profit-protect bevat niet-eindige waarde')
        if trigger_pct <= 0 or lock_profit_eur < 0 or trail_distance_pct <= 0:
            raise ValueError('profit-protect waarden moeten positief zijn')
        if trail_distance_pct >= trigger_pct:
            raise ValueError('profit-protect trail moet kleiner zijn dan trigger')

        self.trigger_pct = float(trigger_pct)
        self.lock_profit_eur = float(lock_profit_eur)
        self.trail_distance_pct = float(trail_distance_pct)

        required = self.minimum_lock_move_pct()
        if self.trigger_pct <= required + 0.05:
            raise ValueError(
                'profit-protect trigger geeft onvoldoende ruimte boven netto winstvloer'
            )

    def minimum_lock_move_pct(self) -> float:
        """Benodigde referentiebeweging om de ingestelde netto €-winst te halen."""
        entry_notional = self.s.position_eur
        entry_fee = entry_notional * self.fee_rate
        denominator = entry_notional * (1.0 - self.slippage_rate) * (1.0 - self.fee_rate)
        if denominator <= 0:
            raise ValueError('ongeldige fee/slippage voor profit-protect')
        ratio = (entry_notional + entry_fee + self.lock_profit_eur) / denominator
        return (ratio - 1.0) * 100.0

    def _lock_reference_price(self, p: Position) -> float:
        target_net_exit = p.entry_notional + p.entry_fee + self.lock_profit_eur
        denominator = p.amount * (1.0 - self.slippage_rate) * (1.0 - self.fee_rate)
        if denominator <= 0:
            raise ValueError('ongeldige positie voor profit-protect')
        return target_net_exit / denominator

    def _original_stop(self, p: Position) -> float:
        return p.entry_price * (1.0 - self.s.stop_loss_pct / 100.0)

    def _is_protected(self, p: Position) -> bool:
        return p.stop_price > self._original_stop(p) * (1.0 + 1e-10)

    def _persist_stop(self, p: Position, new_stop: float) -> None:
        if not math.isfinite(new_stop) or new_stop <= 0:
            raise ValueError('ongeldige profit-protect stop')
        with self.db.conn:
            cur = self.db.conn.execute(
                'UPDATE positions SET stop_price=? WHERE market=? AND opened_at_ms=?',
                (new_stop, p.market, p.opened_at_ms),
            )
            if cur.rowcount != 1:
                raise RuntimeError('positie niet gevonden tijdens profit-protect update')
        p.stop_price = new_stop

    def _maybe_raise_stop(self, p: Position, bid: float) -> bool:
        trigger_price = p.entry_price * (1.0 + self.trigger_pct / 100.0)
        if bid < trigger_price:
            return False

        lock_floor = self._lock_reference_price(p)
        trailing_stop = bid * (1.0 - self.trail_distance_pct / 100.0)
        candidate = max(p.stop_price, lock_floor, trailing_stop)

        # Een beschermstop hoort altijd onder de huidige uitvoerbare biedprijs
        # en onder de vaste take-profit te blijven.
        ceiling = min(bid, p.take_price) * (1.0 - 1e-9)
        candidate = min(candidate, ceiling)

        if candidate <= p.stop_price * (1.0 + 1e-10):
            return False

        self._persist_stop(p, candidate)
        return True

    def process_book(
        self,
        market: str,
        book: Book,
        now_ms: int | None = None,
    ) -> TradeEvent | None:
        if not book.is_valid:
            raise ValueError(f'ongeldig orderboek voor {market}')
        p = self.db.get_position(market)
        if p is None:
            return None

        closed_at_ms = int(time.time() * 1000) if now_ms is None else now_ms

        if book.bid <= p.stop_price:
            reference_price = min(book.bid, p.stop_price)
            reason = 'profit_protect' if self._is_protected(p) else 'stop_loss'
            return self._close(p, reference_price, reason, closed_at_ms)

        if book.bid >= p.take_price:
            return self._close(p, p.take_price, 'take_profit', closed_at_ms)

        self._maybe_raise_stop(p, book.bid)
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
            reason = 'profit_protect' if self._is_protected(p) else 'stop_loss'
            return self._close(p, p.stop_price, reason, closed_at_ms)
        if candle.high >= p.take_price:
            return self._close(p, p.take_price, 'take_profit', closed_at_ms)
        if p.bars_held >= self.s.max_hold_bars:
            return self._close(p, candle.close, 'time_exit', closed_at_ms)

        # De gewone Storage-update bewaart bars_held; stop_price is al apart
        # atomair opgeslagen zodra de live profit-protect hem verhoogt.
        self.db.update_position(p)
        return None
