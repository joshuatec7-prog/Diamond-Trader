from __future__ import annotations

from statistics import median
from typing import Iterable, List, Optional

from models import Candle


def ema(values: Iterable[float], period: int) -> List[Optional[float]]:
    vals = list(values)
    out: List[Optional[float]] = [None] * len(vals)
    if period <= 0 or len(vals) < period:
        return out
    seed = sum(vals[:period]) / period
    out[period - 1] = seed
    alpha = 2.0 / (period + 1.0)
    prev = seed
    for i in range(period, len(vals)):
        prev = (vals[i] * alpha) + (prev * (1.0 - alpha))
        out[i] = prev
    return out


def rsi(values: Iterable[float], period: int = 14) -> List[Optional[float]]:
    vals = list(values)
    out: List[Optional[float]] = [None] * len(vals)
    if period <= 0 or len(vals) <= period:
        return out
    gains, losses = [], []
    for i in range(1, period + 1):
        delta = vals[i] - vals[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    def _value(gain: float, loss: float) -> float:
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        rs = gain / loss
        return 100.0 - (100.0 / (1.0 + rs))
    out[period] = _value(avg_gain, avg_loss)
    for i in range(period + 1, len(vals)):
        delta = vals[i] - vals[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        out[i] = _value(avg_gain, avg_loss)
    return out


def atr(candles: Iterable[Candle], period: int = 14) -> List[Optional[float]]:
    rows = list(candles)
    out: List[Optional[float]] = [None] * len(rows)
    if period <= 0 or len(rows) <= period:
        return out
    true_ranges: List[float] = []
    for i, candle in enumerate(rows):
        if i == 0:
            tr = candle.high - candle.low
        else:
            prev_close = rows[i - 1].close
            tr = max(candle.high - candle.low, abs(candle.high - prev_close), abs(candle.low - prev_close))
        true_ranges.append(tr)
    seed = sum(true_ranges[1:period + 1]) / period
    out[period] = seed
    prev = seed
    for i in range(period + 1, len(rows)):
        prev = ((prev * (period - 1)) + true_ranges[i]) / period
        out[i] = prev
    return out


def rolling_median_ratio(values: Iterable[float], lookback: int) -> List[Optional[float]]:
    vals = list(values)
    out: List[Optional[float]] = [None] * len(vals)
    if lookback < 1:
        return out
    for i in range(lookback, len(vals)):
        base = median(vals[i - lookback:i])
        out[i] = (vals[i] / base) if base > 0 else None
    return out
