from __future__ import annotations

import base64
import logging
import math
import os
import socket
import ssl
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
        self.session.headers.update({'Accept': 'application/json'})

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

    def probe_public_endpoints(self) -> List[Dict[str, Any]]:
        probes = [
            ('markets', '/markets'),
            ('ticker24h', '/ticker/24h'),
            ('book', '/ticker/book'),
        ]
        results: List[Dict[str, Any]] = []
        for name, path in probes:
            try:
                response = self.session.get(
                    f'{self.base_url}{path}', params={}, timeout=self.timeout_seconds
                )
                text = str(getattr(response, 'text', '') or '')
                body = ' '.join(text.split())[:160]
                results.append({'name': name, 'status': int(response.status_code), 'body': body})
            except requests.RequestException as exc:
                results.append({'name': name, 'status': 'ERROR', 'body': f'{type(exc).__name__}: {exc}'})
        return results

    def probe_websocket_handshake(self) -> Dict[str, Any]:
        host = 'ws.bitvavo.com'
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        request = (
            'GET /v2/ HTTP/1.1\r\n'
            f'Host: {host}\r\n'
            'Upgrade: websocket\r\n'
            'Connection: Upgrade\r\n'
            f'Sec-WebSocket-Key: {key}\r\n'
            'Sec-WebSocket-Version: 13\r\n'
            '\r\n'
        ).encode('ascii')
        try:
            context = ssl.create_default_context()
            with socket.create_connection((host, 443), timeout=self.timeout_seconds) as raw:
                with context.wrap_socket(raw, server_hostname=host) as tls:
                    tls.settimeout(self.timeout_seconds)
                    tls.sendall(request)
                    response = tls.recv(2048).decode('latin-1', errors='replace')
            first_line = response.splitlines()[0] if response else 'geen antwoord'
            status = 101 if ' 101 ' in first_line else first_line
            return {'name': 'websocket', 'status': status, 'body': first_line[:160]}
        except (OSError, ssl.SSLError) as exc:
            return {'name': 'websocket', 'status': 'ERROR', 'body': f'{type(exc).__name__}: {exc}'}

    def trading_markets(self, quote: str = 'EUR') -> List[str]:
        payload = self._get('/markets')
        if not isinstance(payload, list):
            raise RuntimeError('ongeldig /markets antwoord')
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
        if not isinstance(payload, list):
            raise RuntimeError('ongeldig /ticker/24h antwoord')
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
            if math.isfinite(volume_quote) and volume_quote > 0:
                ranked.append((volume_quote, market))
        ranked.sort(reverse=True)
        selected = [market for _, market in ranked[:limit]]
        if len(selected) < limit:
            raise RuntimeError(f'onvoldoende actieve {quote}-markten met 24h-volume')
        return selected

    def candles(self, market: str, interval: str, limit: int) -> List[Candle]:
        payload = self._get(f'/{market}/candles', {'interval': interval, 'limit': limit})
        if not isinstance(payload, list):
            raise RuntimeError(f'ongeldig candles-antwoord voor {market}')
        parsed: Dict[int, Candle] = {}
        for row in payload:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            try:
                candle = Candle(
                    timestamp_ms=int(row[0]), open=float(row[1]), high=float(row[2]),
                    low=float(row[3]), close=float(row[4]), volume=float(row[5]),
                )
            except (TypeError, ValueError, OverflowError):
                continue
            if candle.is_valid:
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
        item = payload[0] if isinstance(payload, list) and payload else payload
        if not isinstance(item, dict):
            raise RuntimeError('ongeldig ticker/book antwoord')
        try:
            book = Book(bid=float(item['bid']), ask=float(item['ask']))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError(f'ongeldig ticker/book antwoord voor {market}') from exc
        if not book.is_valid:
            raise RuntimeError(f'ongeldige bid/ask voor {market}')
        return book
