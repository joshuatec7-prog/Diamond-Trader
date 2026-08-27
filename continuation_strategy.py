from __future__ import annotations

import math
from statistics import fmean
from typing import Sequence

from config import Settings
from models import Candle, Decision


class TrendContinuationStrategy:
    """Long-only trend continuation after a controlled pullback.

    Fixed prospectieve PAPER-regels:
    - 12-bar SMA boven 48-bar SMA;
    - 48-bar SMA stijgt minimaal 0,15% over 8 bars;
    - recente pullback vanaf een lokale 8-bar top is 0,60% t/m 4,00%;
    - vorige close ligt niet meer dan 0,50% boven de vorige fast SMA;
    - huidige close herneemt de fast SMA en sluit hoger dan de vorige close;
    - recovery-candle is maximaal +3,00% om pumps niet na te jagen.
    """

    FAST_WINDOW = 12
    SLOW_WINDOW = 48
    SLOPE_LOOKBACK = 8
    PULLBACK_LOOKBACK = 8
    MIN_SLOW_SLOPE_PCT = 0.15
    MIN_PULLBACK_PCT = 0.60
    MAX_PULLBACK_PCT = 4.00
    MAX_PREV_ABOVE_FAST_PCT = 0.50
    MAX_RECOVERY_PCT = 3.00

    def __init__(self, settings: Settings) -> None:
        self.s = settings

    @classmethod
    def required_candles(cls) -> int:
        return cls.SLOW_WINDOW + cls.SLOPE_LOOKBACK + 1

    def evaluate(self, candles: Sequence[Candle]) -> Decision:
        if len(candles) < self.required_candles():
            close = candles[-1].close if candles else float('nan')
            return Decision('SKIP', 'onvoldoende_data', {'close': close})

        closes = [c.close for c in candles]
        last = closes[-1]
        prev = closes[-2]

        fast = fmean(closes[-self.FAST_WINDOW:])
        slow = fmean(closes[-self.SLOW_WINDOW:])
        fast_prev = fmean(closes[-1-self.FAST_WINDOW:-1])
        slow_prev = fmean(
            closes[-self.SLOW_WINDOW-self.SLOPE_LOOKBACK:-self.SLOPE_LOOKBACK]
        )

        prior = closes[-1-self.PULLBACK_LOOKBACK:-1]
        high_index = max(range(len(prior)), key=prior.__getitem__)
        recent_high = prior[high_index]
        after_high = prior[high_index + 1:]
        pullback_low = min(after_high) if after_high else recent_high

        slow_slope_pct = ((slow / slow_prev) - 1.0) * 100.0 if slow_prev > 0 else float('nan')
        pullback_pct = ((recent_high / pullback_low) - 1.0) * 100.0 if pullback_low > 0 else float('nan')
        prev_above_fast_pct = ((prev / fast_prev) - 1.0) * 100.0 if fast_prev > 0 else float('nan')
        recovery_pct = ((last / prev) - 1.0) * 100.0 if prev > 0 else float('nan')

        metrics = {
            'close': last,
            'prev_close': prev,
            'fast_sma': fast,
            'slow_sma': slow,
            'fast_prev': fast_prev,
            'slow_slope_pct': slow_slope_pct,
            'recent_high': recent_high,
            'pullback_low': pullback_low,
            'pullback_pct': pullback_pct,
            'prev_above_fast_pct': prev_above_fast_pct,
            'recovery_pct': recovery_pct,
        }

        values = [
            last, prev, fast, slow, fast_prev, slow_prev, recent_high, pullback_low,
            slow_slope_pct, pullback_pct, prev_above_fast_pct, recovery_pct,
        ]
        if not all(math.isfinite(v) for v in values) or min(
            last, prev, fast, slow, fast_prev, slow_prev, recent_high, pullback_low
        ) <= 0:
            return Decision('SKIP', 'ongeldige_continuation_waarden', metrics)

        if not (fast > slow and last > slow):
            return Decision('SKIP', 'trend_niet_opwaarts', metrics)
        if slow_slope_pct < self.MIN_SLOW_SLOPE_PCT:
            return Decision('SKIP', 'trend_slope_te_zwak', metrics)
        if pullback_pct < self.MIN_PULLBACK_PCT:
            return Decision('SKIP', 'geen_duidelijke_pullback', metrics)
        if pullback_pct > self.MAX_PULLBACK_PCT:
            return Decision('SKIP', 'pullback_te_diep', metrics)
        if prev_above_fast_pct > self.MAX_PREV_ABOVE_FAST_PCT:
            return Decision('SKIP', 'nog_te_ver_boven_fast', metrics)
        if not (last > fast and last > prev):
            return Decision('SKIP', 'nog_geen_hervatting', metrics)
        if recovery_pct > self.MAX_RECOVERY_PCT:
            return Decision('SKIP', 'recovery_te_scherp', metrics)

        return Decision('BUY', 'trend_pullback_continuation', metrics)

    @staticmethod
    def rank_score(decision: Decision) -> float:
        m = decision.metrics
        slope = float(m.get('slow_slope_pct', float('nan')))
        pullback = float(m.get('pullback_pct', float('nan')))
        recovery = float(m.get('recovery_pct', float('nan')))
        if not all(math.isfinite(v) for v in (slope, pullback, recovery)):
            return float('nan')

        # Voorkeur voor sterke trend, gezonde pullback rond circa 1,5% en
        # duidelijke maar niet extreme recovery.
        pullback_quality = max(0.0, 2.0 - abs(pullback - 1.5))
        return (2.0 * slope) + pullback_quality + (0.5 * max(0.0, recovery))
