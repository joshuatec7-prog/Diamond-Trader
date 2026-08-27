from __future__ import annotations

import math
import time

from models import Book, Candle, Position, TradeEvent
from profit_protect_trader import RunnerProfitProtectPaperTrader


class StagedRunnerPaperTrader(RunnerProfitProtectPaperTrader):
    """PAPER runner met drie vaste winstbeschermingsfasen.

    Fase 1: vanaf `lock_trigger_pct` alleen de netto winstvloer vastzetten.
    Fase 2: vanaf `wide_trigger_pct` een ruime trailing stop gebruiken.
    Fase 3: vanaf `tight_trigger_pct` de trailing stop aanscherpen.

    Er is geen harde take-profit. Een beschermde positie heeft ook geen
    max-hold exit; zolang de koers blijft stijgen kan de PAPER-runner doorgaan.
    """

    def __init__(
        self,
        settings,
        storage,
        entry_reason: str,
        lock_trigger_pct: float = 1.50,
        lock_profit_eur: float = 0.50,
        wide_trigger_pct: float = 3.00,
        wide_trail_pct: float = 1.25,
        tight_trigger_pct: float = 6.00,
        tight_trail_pct: float = 0.75,
    ) -> None:
        super().__init__(
            settings,
            storage,
            entry_reason=entry_reason,
            trigger_pct=lock_trigger_pct,
            lock_profit_eur=lock_profit_eur,
            trail_distance_pct=wide_trail_pct,
        )

        values = (
            lock_trigger_pct,
            lock_profit_eur,
            wide_trigger_pct,
            wide_trail_pct,
            tight_trigger_pct,
            tight_trail_pct,
        )
        if not all(math.isfinite(v) for v in values):
            raise ValueError('staged-runner bevat niet-eindige waarde')
        if lock_trigger_pct <= 0 or lock_profit_eur < 0:
            raise ValueError('staged-runner lockwaarden zijn ongeldig')
        if not (lock_trigger_pct < wide_trigger_pct < tight_trigger_pct):
            raise ValueError('staged-runner triggers moeten oplopend zijn')
        if not (0 < tight_trail_pct < wide_trail_pct < wide_trigger_pct):
            raise ValueError('staged-runner trailing afstanden zijn ongeldig')

        self.lock_trigger_pct = float(lock_trigger_pct)
        self.wide_trigger_pct = float(wide_trigger_pct)
        self.wide_trail_pct = float(wide_trail_pct)
        self.tight_trigger_pct = float(tight_trigger_pct)
        self.tight_trail_pct = float(tight_trail_pct)

    def _maybe_raise_stop(self, p: Position, bid: float) -> bool:
        lock_trigger = p.entry_price * (1.0 + self.lock_trigger_pct / 100.0)
        if bid < lock_trigger:
            return False

        # Fase 1: alleen de minimale netto winst vastzetten. Dus nog geen
        # strakke trailing stop tijdens de eerste winstbeweging.
        candidate = max(p.stop_price, self._lock_reference_price(p))

        # Fase 2: pas vanaf +3% krijgt de runner een ruime 1,25%-trail.
        wide_trigger = p.entry_price * (1.0 + self.wide_trigger_pct / 100.0)
        if bid >= wide_trigger:
            candidate = max(
                candidate,
                bid * (1.0 - self.wide_trail_pct / 100.0),
            )

        # Fase 3: vanaf +6% wordt de trail 0,75%, zodat een grote runner
        # meer van de opgebouwde stijging beschermt.
        tight_trigger = p.entry_price * (1.0 + self.tight_trigger_pct / 100.0)
        if bid >= tight_trigger:
            candidate = max(
                candidate,
                bid * (1.0 - self.tight_trail_pct / 100.0),
            )

        # Geen harde TP. De stop mag onbeperkt verder omhoog lopen, maar blijft
        # altijd net onder de huidige uitvoerbare biedprijs.
        candidate = min(candidate, bid * (1.0 - 1e-9))

        if candidate <= p.stop_price * (1.0 + 1e-10):
            return False

        self._persist_stop(p, candidate)
        return True

    def _protected_exit_reason(self, p: Position) -> str:
        if not self._is_protected(p):
            return 'stop_loss'
        lock_floor = self._lock_reference_price(p)
        if p.stop_price <= lock_floor * (1.0 + 1e-8):
            return 'runner_profit_lock'
        return 'runner_trailing_stop'

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
            return self._close(
                p,
                reference_price,
                self._protected_exit_reason(p),
                closed_at_ms,
            )

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
            return self._close(
                p,
                p.stop_price,
                self._protected_exit_reason(p),
                closed_at_ms,
            )

        if p.bars_held >= self.s.max_hold_bars and not self._is_protected(p):
            return self._close(p, candle.close, 'time_exit', closed_at_ms)

        self.db.update_position(p)
        return None
