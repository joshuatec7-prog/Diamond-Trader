from __future__ import annotations

import math
from statistics import fmean
from typing import Sequence

from config import Settings
from models import Candle, Decision


class TrendMomentumStrategy:
    """Long-only trend/momentum entry for rising markets.

    Vaste prospectieve PAPER-regels:
    - 12-bar gemiddelde boven 48-bar gemiddelde;
    - 48-bar gemiddelde stijgt over de laatste 8 bars;
    - 1-uurs momentum (4 x 15m) tussen +0,30% en +6,00%;
    - laatste close breekt boven de hoogste close van de vorige 8 bars.

    Geldige BUY-signalen krijgen daarna een vaste trend-score. Die score is de
    som van vier percentages: fast/slow-afstand, slow-slope, 1h momentum en de
    breakout-afstand. Daardoor wordt niet meer simpelweg de eerste munt uit de
    universe gekozen, maar worden gelijktijdige kandidaten onderling gerangschikt.
    """

    FAST_WINDOW = 12
    SLOW_WINDOW = 48
    SLOPE_LOOKBACK = 8
    BREAKOUT_WINDOW = 8
    MOMENTUM_BARS = 4
    MIN_SLOW_SLOPE_PCT = 0.15
    MIN_MOMENTUM_PCT = 0.30
    MAX_MOMENTUM_PCT = 6.00

    def __init__(self, settings: Settings) -> None:
        self.s = settings

    @classmethod
    def required_candles(cls) -> int:
        return cls.SLOW_WINDOW + cls.SLOPE_LOOKBACK

    @classmethod
    def rank_score(cls, decision: Decision) -> float:
        if decision.action != 'BUY':
            return float('-inf')
        try:
            values = [
                float(decision.metrics['fast_slow_gap_pct']),
                float(decision.metrics['slow_slope_pct']),
                float(decision.metrics['momentum_pct']),
                float(decision.metrics['breakout_pct']),
            ]
        except (KeyError, TypeError, ValueError):
            return float('-inf')
        if not all(math.isfinite(v) for v in values):
            return float('-inf')
        return sum(values)

    def evaluate(self, candles: Sequence[Candle]) -> Decision:
        if len(candles) < self.required_candles():
            return Decision('SKIP', 'onvoldoende_data', {})

        closes = [c.close for c in candles]
        last = closes[-1]
        fast = fmean(closes[-self.FAST_WINDOW:])
        slow = fmean(closes[-self.SLOW_WINDOW:])
        slow_prev = fmean(
            closes[-self.SLOW_WINDOW-self.SLOPE_LOOKBACK:-self.SLOPE_LOOKBACK]
        )
        momentum_base = closes[-1-self.MOMENTUM_BARS]
        breakout_ref = max(closes[-1-self.BREAKOUT_WINDOW:-1])

        slow_slope_pct = ((slow / slow_prev) - 1.0) * 100.0 if slow_prev > 0 else float('nan')
        momentum_pct = ((last / momentum_base) - 1.0) * 100.0 if momentum_base > 0 else float('nan')
        fast_slow_gap_pct = ((fast / slow) - 1.0) * 100.0 if slow > 0 else float('nan')
        breakout_pct = ((last / breakout_ref) - 1.0) * 100.0 if breakout_ref > 0 else float('nan')

        metrics = {
            'close': last,
            'fast_sma': fast,
            'slow_sma': slow,
            'fast_slow_gap_pct': fast_slow_gap_pct,
            'slow_slope_pct': slow_slope_pct,
            'momentum_pct': momentum_pct,
            'breakout_ref': breakout_ref,
            'breakout_pct': breakout_pct,
        }

        values = [
            last, fast, slow, slow_prev, momentum_base, breakout_ref,
            fast_slow_gap_pct, slow_slope_pct, momentum_pct, breakout_pct,
        ]
        if not all(math.isfinite(v) for v in values) or min(
            last, fast, slow, slow_prev, momentum_base, breakout_ref
        ) <= 0:
            return Decision('SKIP', 'ongeldige_trendwaarden', metrics)

        if not (fast > slow and last > slow):
            return Decision('SKIP', 'trend_niet_opwaarts', metrics)
        if slow_slope_pct < self.MIN_SLOW_SLOPE_PCT:
            return Decision('SKIP', 'trend_slope_te_zwak', metrics)
        if momentum_pct < self.MIN_MOMENTUM_PCT:
            return Decision('SKIP', 'momentum_te_zwak', metrics)
        if momentum_pct > self.MAX_MOMENTUM_PCT:
            return Decision('SKIP', 'momentum_te_hoog', metrics)
        if last <= breakout_ref:
            return Decision('SKIP', 'geen_breakout', metrics)
        return Decision('BUY', 'trend_breakout', metrics)
