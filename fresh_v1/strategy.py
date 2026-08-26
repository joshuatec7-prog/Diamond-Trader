from __future__ import annotations

from typing import List

from config import Settings
from indicators import atr, ema, rolling_median_ratio, rsi
from models import Candle, Signal


class TrendBreakoutStrategy:
    def __init__(self, settings: Settings) -> None:
        self.s = settings

    @property
    def minimum_candles(self) -> int:
        return max(
            self.s.ema_slow + 2,
            self.s.rsi_period + 2,
            self.s.atr_period + 2,
            self.s.breakout_lookback + 2,
            self.s.volume_lookback + 2,
        )

    def evaluate(self, candles: List[Candle]) -> Signal:
        if len(candles) < self.minimum_candles:
            return Signal("SKIP", "te_weinig_candles", {"count": float(len(candles))})

        closes = [c.close for c in candles]
        volumes = [c.volume for c in candles]
        ef_values = ema(closes, self.s.ema_fast)
        es_values = ema(closes, self.s.ema_slow)
        rsi_values = rsi(closes, self.s.rsi_period)
        atr_values = atr(candles, self.s.atr_period)
        volume_ratios = rolling_median_ratio(volumes, self.s.volume_lookback)

        idx = len(candles) - 1
        latest = candles[idx]
        ef, es = ef_values[idx], es_values[idx]
        rv, av, vr = rsi_values[idx], atr_values[idx], volume_ratios[idx]
        if None in (ef, es, rv, av, vr):
            return Signal("SKIP", "indicator_niet_beschikbaar", {})

        breakout_level = max(closes[idx - self.s.breakout_lookback:idx])
        metrics = {
            "close": latest.close,
            "ema_fast": float(ef),
            "ema_slow": float(es),
            "rsi": float(rv),
            "atr": float(av),
            "volume_ratio": float(vr),
            "breakout_level": float(breakout_level),
        }

        checks = [
            (ef > es, "trend_niet_opwaarts"),
            (latest.close > ef, "close_niet_boven_ema_fast"),
            (self.s.rsi_min <= rv <= self.s.rsi_max, "rsi_buiten_band"),
            (latest.close > breakout_level, "geen_breakout"),
            (vr >= self.s.min_volume_ratio, "volume_te_laag"),
        ]
        for passed, reason in checks:
            if not passed:
                return Signal("SKIP", reason, metrics)
        return Signal("BUY", "trend_breakout", metrics)
