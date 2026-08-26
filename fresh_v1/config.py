from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None else float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None else int(raw)


def _env_markets(name: str, default: str) -> Tuple[str, ...]:
    raw = os.getenv(name, default)
    markets = tuple(part.strip().upper() for part in raw.split(",") if part.strip())
    if not markets:
        raise ValueError("MARKETS mag niet leeg zijn")
    return markets


def default_db_path() -> str:
    render_data = Path("/var/data")
    if render_data.exists() and os.access(render_data, os.W_OK):
        return str(render_data / "cryptobot_fresh.db")
    return str(Path("data") / "cryptobot_fresh.db")


@dataclass(frozen=True)
class Settings:
    api_base_url: str = os.getenv("BITVAVO_API_BASE_URL", "https://api.bitvavo.com/v2")
    markets: Tuple[str, ...] = _env_markets("MARKETS", "BTC-EUR,ETH-EUR,SOL-EUR")
    interval: str = os.getenv("CANDLE_INTERVAL", "15m")
    poll_seconds: int = _env_int("POLL_SECONDS", 60)
    candle_limit: int = _env_int("CANDLE_LIMIT", 250)
    db_path: str = os.getenv("DB_PATH", default_db_path())

    paper_start_eur: float = _env_float("PAPER_START_EUR", 1000.0)
    stake_eur: float = _env_float("STAKE_EUR", 100.0)
    max_open_positions: int = _env_int("MAX_OPEN_POSITIONS", 2)

    taker_fee_pct: float = _env_float("TAKER_FEE_PCT", 0.25)
    slippage_pct: float = _env_float("SLIPPAGE_PCT", 0.05)
    max_spread_pct: float = _env_float("MAX_SPREAD_PCT", 0.30)
    backtest_assumed_spread_pct: float = _env_float("BACKTEST_ASSUMED_SPREAD_PCT", 0.10)

    ema_fast: int = _env_int("EMA_FAST", 20)
    ema_slow: int = _env_int("EMA_SLOW", 50)
    rsi_period: int = _env_int("RSI_PERIOD", 14)
    atr_period: int = _env_int("ATR_PERIOD", 14)
    breakout_lookback: int = _env_int("BREAKOUT_LOOKBACK", 20)
    volume_lookback: int = _env_int("VOLUME_LOOKBACK", 20)
    min_volume_ratio: float = _env_float("MIN_VOLUME_RATIO", 1.10)
    rsi_min: float = _env_float("RSI_MIN", 52.0)
    rsi_max: float = _env_float("RSI_MAX", 72.0)

    stop_atr_mult: float = _env_float("STOP_ATR_MULT", 1.6)
    take_atr_mult: float = _env_float("TAKE_ATR_MULT", 2.8)
    trailing_trigger_atr: float = _env_float("TRAILING_TRIGGER_ATR", 1.6)
    trailing_distance_atr: float = _env_float("TRAILING_DISTANCE_ATR", 1.1)

    request_timeout_seconds: int = _env_int("REQUEST_TIMEOUT_SECONDS", 10)
    request_retries: int = _env_int("REQUEST_RETRIES", 3)
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
    loop_enabled: bool = _env_bool("LOOP_ENABLED", True)

    def validate(self) -> None:
        allowed_intervals = {
            "1m", "5m", "15m", "30m", "1h", "2h", "4h",
            "6h", "8h", "12h", "1d"
        }
        if self.interval not in allowed_intervals:
            raise ValueError(f"Ongeldig CANDLE_INTERVAL: {self.interval}")
        if self.poll_seconds < 10:
            raise ValueError("POLL_SECONDS moet minimaal 10 zijn")
        if not (60 <= self.candle_limit <= 1440):
            raise ValueError("CANDLE_LIMIT moet tussen 60 en 1440 liggen")
        if self.paper_start_eur <= 0 or self.stake_eur <= 0:
            raise ValueError("Paperkapitaal en stake moeten positief zijn")
        if self.max_open_positions < 1:
            raise ValueError("MAX_OPEN_POSITIONS moet minimaal 1 zijn")
        if not (0 <= self.taker_fee_pct <= 2):
            raise ValueError("TAKER_FEE_PCT buiten verwacht bereik")
        if not (0 <= self.slippage_pct <= 2):
            raise ValueError("SLIPPAGE_PCT buiten verwacht bereik")
        if not (0 < self.max_spread_pct <= 5):
            raise ValueError("MAX_SPREAD_PCT buiten verwacht bereik")
        if not (0 <= self.backtest_assumed_spread_pct <= 2):
            raise ValueError("BACKTEST_ASSUMED_SPREAD_PCT buiten verwacht bereik")
        if not (2 <= self.ema_fast < self.ema_slow):
            raise ValueError("EMA_FAST moet kleiner zijn dan EMA_SLOW")
        if self.breakout_lookback < 2 or self.volume_lookback < 2:
            raise ValueError("Lookback moet minimaal 2 zijn")
        if not (0 <= self.rsi_min < self.rsi_max <= 100):
            raise ValueError("RSI-bereik ongeldig")
