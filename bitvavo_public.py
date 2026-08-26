from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

import requests

from models import Book, Candle


INTERVAL_MS = {
    '1m': 60_000, '5m': 300_000, '15m': 900_000, '30m': 1_800_000,
    '1h': 3_600_000, '2h': 7_200_000, '4h': 14_400_000, '6h': 21_600_000,
    '8h': 28_800_000, '12h': 43_200_000, '1d': 86_400_000,
}

logger = logging.getLogger(__name__)


class PermanentHTTPError(RuntimeError):
    pass


class BitvavoPublic:
    def __init__(self, base_url: str, timeout_seconds: int = 10, retries: int = 3,
                 session: requests.Session | None = None) -> None:
        self.base_url = base_url.rstrip('/')
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.session = session or requests.Session()
        self.session.headers.update({'Accept': 'application/json', 'User-Agent': 'CryptoBot-CleanRoom/1.0'})

    def _get(self, path: str, params: Dict[str, Any] | None = None) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.get(
                    f'{self.base_url}{path}', params=params or {}, timeout=self.timeout_seconds
                )
                if response.status_code == 429:
                    reset_at = response.headers.get('bitvavo-ratelimit-resetat')
                    raise RuntimeError(f'rate limit bereikt; reset={reset_at}')
                if 400 <= response.status_code < 500:
                    raise PermanentHTTPError(f'HTTP {response.status_code} voor {path}')
                response.raise_for_status()
                return response.json()
            except PermanentHTTPError:
                raise
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                logger.warning('Publieke marktrequest mislukt (%s/%s): %s', attempt, self.retries, exc)
                time.sleep(min(2 ** (attempt - 1), 4))
        raise RuntimeError(f'publieke marktrequest definitief mislukt: {last_error}')

    def trading_markets(self, quote: str = 'EUR') -> List[str]:
        payload = self._get('/markets')
        result: List[str] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            if item.get('status') == 'trading' and str(item.get('quote', '')).upper() == quote.upper():
                market = str(item.get('market', '')).upper()
                if market:
                    result.append(market)
        return sorted(set(result))

    def top_markets_by_quote_volume(self, quote: str, limit: int) -> List[str]:
        allowed = set(self.trading_markets(quote))
        payload = self._get('/ticker/24h')
        ranked: List[tuple[float, str]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            market = str(item.get('market', '')).upper()
            if market not in allowed:
                continue
            try:
                volume_quote = float(item.get('volumeQuote', 0) or 0)
            except (TypeError, ValueError):
                continue
            if volume_quote > 0:
                ranked.append((volume_quote, market))
        ranked.sort(reverse=True)
        selected = [market for _, market in ranked[:limit]]
        if len(selected) < limit:
            raise RuntimeError(f'onvoldoende actieve {quote}-markten met 24h-volume')
        return selected

    def candles(self, market: str, interval: str, limit: int) -> List[Candle]:
        payload = self._get(f'/{market}/candles', {'interval': interval, 'limit': limit})
        parsed: Dict[int, Candle] = {}
        for row in payload:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            candle = Candle(
                timestamp_ms=int(row[0]), open=float(row[1]), high=float(row[2]),
                low=float(row[3]), close=float(row[4]), volume=float(row[5]),
            )
            if candle.open > 0 and candle.high > 0 and candle.low > 0 and candle.close > 0:
                parsed[candle.timestamp_ms] = candle
        return [parsed[key] for key in sorted(parsed)]

    def closed_candles(self, market: str, interval: str, limit: int,
                       now_ms: int | None = None) -> List[Candle]:
        if interval not in INTERVAL_MS:
            raise ValueError(f'interval niet ondersteund: {interval}')
        now = int(time.time() * 1000) if now_ms is None else now_ms
        duration = INTERVAL_MS[interval]
        return [c for c in self.candles(market, interval, limit) if c.timestamp_ms + duration <= now]

    def book(self, market: str) -> Book:
        payload = self._get('/ticker/book', {'market': market})
        item = payload[0] if isinstance(payload, list) else payload
        if not isinstance(item, dict):
            raise RuntimeError('ongeldig ticker/book antwoord')
        bid = float(item['bid'])
        ask = float(item['ask'])
        if bid <= 0 or ask <= 0 or ask < bid:
            raise RuntimeError(f'ongeldige bid/ask voor {market}')
        return Book(bid=bid, ask=ask)
