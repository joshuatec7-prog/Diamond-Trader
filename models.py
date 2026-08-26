from __future__ import annotations

import math
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

    @property
    def is_valid(self) -> bool:
        values = (self.open, self.high, self.low, self.close, self.volume)
        if self.timestamp_ms < 0 or not all(math.isfinite(v) for v in values):
            return False
        if min(self.open, self.high, self.low, self.close) <= 0 or self.volume < 0:
            return False
        return self.low <= min(self.open, self.close) and self.high >= max(self.open, self.close) and self.high >= self.low


@dataclass(frozen=True)
class Book:
    bid: float
    ask: float

    @property
    def is_valid(self) -> bool:
        return (
            math.isfinite(self.bid) and math.isfinite(self.ask)
            and self.bid > 0 and self.ask > 0 and self.ask >= self.bid
        )

    @property
    def spread_pct(self) -> float:
        if not self.is_valid:
            return 999.0
        mid = (self.bid + self.ask) / 2.0
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
