from __future__ import annotations

from typing import Protocol

from models import Book, Candle


class MarketDataSource(Protocol):
    """Minimal market-data contract used by strategy and paper execution."""

    def top_markets_by_quote_volume(self, quote: str, limit: int) -> list[str]: ...

    def closed_candles(self, market: str, interval: str, limit: int,
                       now_ms: int | None = None) -> list[Candle]: ...

    def book(self, market: str) -> Book: ...
