from __future__ import annotations

import math
from statistics import fmean
from typing import Sequence

from config import Settings
from models import Candle, Decision


class TrendMomentumStrategy:
    """Long-only trend/momentum entry for rising markets.

    De regels staan bewust vast voor de prospectieve PAPER-meting:
    - 12-bar gemiddelde boven 48-bar gemiddelde;
    - 48-bar gemiddelde stijgt over de laatste 8 bars;
    - 1-uurs momentum (4 x 15m) tussen +0,30% en +6,00%;
    - laatste close breekt boven de hoogste close van de vorige 8 bars.

    De bovengrens op momentum voorkomt blind instappen in een extreem uitgerekte
    candle/pump. Execution, kosten, stop, take-profit en stake blijven gelijk aan
    strategie A zodat het verschil vooral uit de entry komt.
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

    def evaluate(self, candles: Sequence[Candle]) -> Decision:
        need = self.SLOW_WINDOW + self.SLOPE_LOOKBACK
        if len(candles) < need:
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

        metrics = {
            'close': last,
            'fast_sma': fast,
            'slow_sma': slow,
            'slow_slope_pct': slow_slope_pct,
            'momentum_pct': momentum_pct,
            'breakout_ref': breakout_ref,
        }

        values = [last, fast, slow, slow_prev, momentum_base, breakout_ref,
                  slow_slope_pct, momentum_pct]
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
