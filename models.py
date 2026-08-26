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
    def spread_pct(self) -> float:
        mid = (self.bid + self.ask) / 2.0
        if mid <= 0:
            return 999.0
        return ((self.ask - self.bid) / mid) * 100.0


@dataclass(frozen=True)
class Decision:
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
    stop_price: float
    take_price: float
    bars_held: int = 0


@dataclass(frozen=True)
class TradeEvent:
    kind: str
    market: str
    price: float
    reason: str
    pnl_eur: Optional[float] = None
