from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class Candle:
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Book:
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_pct(self) -> float:
        if self.mid <= 0:
            return 999.0
        return ((self.ask - self.bid) / self.mid) * 100.0


@dataclass(frozen=True)
class Signal:
    action: str
    reason: str
    metrics: Dict[str, float]


@dataclass
class Position:
    market: str
    opened_at_ms: int
    entry_candle_ts: int
    entry_price: float
    amount: float
    entry_notional: float
    entry_fee: float
    atr_at_entry: float
    stop_price: float
    take_price: float
    highest_price: float
    trailing_active: bool = False
    trailing_stop: Optional[float] = None
