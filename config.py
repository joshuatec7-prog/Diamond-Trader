from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None else int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None else float(raw)


def default_db_path() -> str:
    render_data = Path('/var/data')
    if render_data.exists() and os.access(render_data, os.W_OK):
        return str(render_data / 'cryptobot_cleanroom.db')
    return str(Path('data') / 'cryptobot_cleanroom.db')


@dataclass(frozen=True)
class Settings:
    run_mode: str = os.getenv('RUN_MODE', 'PAPER').upper()
    api_base_url: str = os.getenv('BITVAVO_API_BASE_URL', 'https://api.bitvavo.com/v2')
    quote_currency: str = os.getenv('QUOTE_CURRENCY', 'EUR').upper()
    universe_size: int = _env_int('UNIVERSE_SIZE', 3)

    interval: str = os.getenv('CANDLE_INTERVAL', '1h')
    candle_limit: int = _env_int('CANDLE_LIMIT', 240)
    poll_seconds: int = _env_int('POLL_SECONDS', 120)
    degraded_retry_seconds: int = _env_int('DEGRADED_RETRY_SECONDS', 300)
    max_signal_age_seconds: int = _env_int('MAX_SIGNAL_AGE_SECONDS', 900)
    max_consecutive_failed_cycles: int = _env_int('MAX_CONSECUTIVE_FAILED_CYCLES', 5)

    paper_start_eur: float = _env_float('PAPER_START_EUR', 5000.0)
    position_eur: float = _env_float('POSITION_EUR', 200.0)
    max_open_positions: int = _env_int('MAX_OPEN_POSITIONS', 1)

    taker_fee_pct: float = _env_float('TAKER_FEE_PCT', 0.25)
    slippage_pct: float = _env_float('SLIPPAGE_PCT', 0.08)
    max_spread_pct: float = _env_float('MAX_SPREAD_PCT', 0.40)
    backtest_assumed_spread_pct: float = _env_float('BACKTEST_ASSUMED_SPREAD_PCT', 0.12)

    band_window: int = _env_int('BAND_WINDOW', 20)
    band_stddev: float = _env_float('BAND_STDDEV', 2.0)
    stop_loss_pct: float = _env_float('STOP_LOSS_PCT', 1.5)
    take_profit_pct: float = _env_float('TAKE_PROFIT_PCT', 2.5)
    max_hold_bars: int = _env_int('MAX_HOLD_BARS', 24)

    eval_min_trades: int = _env_int('EVAL_MIN_TRADES', 40)
    eval_min_span_days: float = _env_float('EVAL_MIN_SPAN_DAYS', 14.0)
    eval_min_profit_factor: float = _env_float('EVAL_MIN_PROFIT_FACTOR', 1.25)
    eval_max_drawdown_pct: float = _env_float('EVAL_MAX_DRAWDOWN_PCT', 10.0)

    request_timeout_seconds: int = _env_int('REQUEST_TIMEOUT_SECONDS', 10)
    request_retries: int = _env_int('REQUEST_RETRIES', 3)
    log_level: str = os.getenv('LOG_LEVEL', 'INFO').upper()
    loop_enabled: bool = _env_bool('LOOP_ENABLED', True)
    db_path: str = os.getenv('DB_PATH', default_db_path())

    def validate(self) -> None:
        if self.run_mode != 'PAPER':
            raise ValueError('Clean-room v1 staat uitsluitend RUN_MODE=PAPER toe')
        if not self.api_base_url.startswith('https://'):
            raise ValueError('BITVAVO_API_BASE_URL moet HTTPS gebruiken')

        numeric_floats = (
            self.paper_start_eur, self.position_eur, self.taker_fee_pct, self.slippage_pct,
            self.max_spread_pct, self.backtest_assumed_spread_pct, self.band_stddev,
            self.stop_loss_pct, self.take_profit_pct, self.eval_min_span_days,
            self.eval_min_profit_factor, self.eval_max_drawdown_pct,
        )
        if not all(math.isfinite(v) for v in numeric_floats):
            raise ValueError('Configuratie bevat niet-eindige numerieke waarde')

        allowed_intervals = {'1m','5m','15m','30m','1h','2h','4h','6h','8h','12h','1d'}
        if self.interval not in allowed_intervals:
            raise ValueError(f'Ongeldig CANDLE_INTERVAL: {self.interval}')
        if self.quote_currency != 'EUR':
            raise ValueError('Clean-room v1 ondersteunt alleen EUR-markten')
        if not (1 <= self.universe_size <= 10):
            raise ValueError('UNIVERSE_SIZE moet tussen 1 en 10 liggen')
        if not (60 <= self.candle_limit <= 1440):
            raise ValueError('CANDLE_LIMIT moet tussen 60 en 1440 liggen')
        if self.poll_seconds < 30:
            raise ValueError('POLL_SECONDS moet minimaal 30 zijn')
        if not (60 <= self.degraded_retry_seconds <= 3600):
            raise ValueError('DEGRADED_RETRY_SECONDS buiten bereik')
        if not (60 <= self.max_signal_age_seconds <= 7200):
            raise ValueError('MAX_SIGNAL_AGE_SECONDS buiten bereik')
        if not (1 <= self.max_consecutive_failed_cycles <= 60):
            raise ValueError('MAX_CONSECUTIVE_FAILED_CYCLES buiten bereik')
        if self.paper_start_eur <= 0 or self.position_eur <= 0:
            raise ValueError('Paperbedragen moeten positief zijn')
        if self.position_eur >= self.paper_start_eur:
            raise ValueError('POSITION_EUR moet kleiner zijn dan PAPER_START_EUR')
        if not (1 <= self.max_open_positions <= 10):
            raise ValueError('MAX_OPEN_POSITIONS buiten bereik')
        if not (0 <= self.taker_fee_pct <= 2):
            raise ValueError('TAKER_FEE_PCT buiten bereik')
        if not (0 <= self.slippage_pct <= 2):
            raise ValueError('SLIPPAGE_PCT buiten bereik')
        if not (0 < self.max_spread_pct <= 5):
            raise ValueError('MAX_SPREAD_PCT buiten bereik')
        if not (0 <= self.backtest_assumed_spread_pct <= 2):
            raise ValueError('BACKTEST_ASSUMED_SPREAD_PCT buiten bereik')
        if not (5 <= self.band_window <= 200):
            raise ValueError('BAND_WINDOW buiten bereik')
        if not (0.5 <= self.band_stddev <= 4.0):
            raise ValueError('BAND_STDDEV buiten bereik')
        if not (0.1 <= self.stop_loss_pct <= 20):
            raise ValueError('STOP_LOSS_PCT buiten bereik')
        if not (0.1 <= self.take_profit_pct <= 30):
            raise ValueError('TAKE_PROFIT_PCT buiten bereik')
        if not (1 <= self.max_hold_bars <= 240):
            raise ValueError('MAX_HOLD_BARS buiten bereik')
        if not (10 <= self.eval_min_trades <= 500):
            raise ValueError('EVAL_MIN_TRADES buiten bereik')
        if not (1 <= self.eval_min_span_days <= 90):
            raise ValueError('EVAL_MIN_SPAN_DAYS buiten bereik')
        if not (1.0 <= self.eval_min_profit_factor <= 5.0):
            raise ValueError('EVAL_MIN_PROFIT_FACTOR buiten bereik')
        if not (1 <= self.eval_max_drawdown_pct <= 50):
            raise ValueError('EVAL_MAX_DRAWDOWN_PCT buiten bereik')
