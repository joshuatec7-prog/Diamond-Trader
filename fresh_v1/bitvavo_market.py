from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

import requests

from models import Book, Candle


INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "1W": 604_800_000,
}

logger = logging.getLogger(__name__)


class BitvavoPermanentError(RuntimeError):
    """Niet-tijdelijke HTTP-fout; opnieuw proberen heeft geen zin."""


class BitvavoMarket:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: int = 10,
        retries: int = 3,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.session = session or requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "CryptoBot-Fresh/1.0",
        })

    def _get(self, path: str, params: Dict[str, Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.get(
                    f"{self.base_url}{path}",
                    params=params,
                    timeout=self.timeout_seconds,
                )
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    body = getattr(response, "text", "")[:200].strip()
                    suffix = f" | {body}" if body else ""
                    raise BitvavoPermanentError(f"Bitvavo HTTP {response.status_code}{suffix}")
                if response.status_code == 429:
                    reset_at = response.headers.get("bitvavo-ratelimit-resetat")
                    raise RuntimeError(f"Bitvavo rate limit bereikt; reset={reset_at}")
                response.raise_for_status()
                return response.json()
            except BitvavoPermanentError:
                raise
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                wait = min(2 ** (attempt - 1), 4)
                logger.warning("Bitvavo request mislukt (%s/%s): %s", attempt, self.retries, exc)
                time.sleep(wait)
        raise RuntimeError(f"Bitvavo request definitief mislukt: {last_error}")

    def get_candles(self, market: str, interval: str, limit: int) -> List[Candle]:
        payload = self._get(
            f"/{market}/candles",
            {"interval": interval, "limit": limit},
        )
        candles: List[Candle] = []
        for row in payload:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            candles.append(
                Candle(
                    timestamp_ms=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
            )
        unique = {c.timestamp_ms: c for c in candles}
        return [unique[key] for key in sorted(unique)]

    def get_closed_candles(
        self,
        market: str,
        interval: str,
        limit: int,
        now_ms: int | None = None,
    ) -> List[Candle]:
        candles = self.get_candles(market, interval, limit)
        if interval not in INTERVAL_MS:
            raise ValueError(f"Interval {interval} niet ondersteund voor close-filter")
        current_ms = int(time.time() * 1000) if now_ms is None else now_ms
        duration = INTERVAL_MS[interval]
        return [c for c in candles if c.timestamp_ms + duration <= current_ms]

    def get_book(self, market: str) -> Book:
        payload = self._get("/ticker/book", {"market": market})
        if isinstance(payload, list):
            if not payload:
                raise RuntimeError(f"Leeg orderboek voor {market}")
            item = payload[0]
        else:
            item = payload
        bid = float(item["bid"])
        ask = float(item["ask"])
        if bid <= 0 or ask <= 0 or ask < bid:
            raise RuntimeError(f"Ongeldig orderboek voor {market}: bid={bid} ask={ask}")
        return Book(bid=bid, ask=ask)
