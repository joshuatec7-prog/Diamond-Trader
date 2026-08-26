from __future__ import annotations

import math
from statistics import fmean, pstdev
from typing import Sequence

from config import Settings
from models import Candle, Decision


def _bands(closes: Sequence[float], window: int, stddev_mult: float) -> tuple[float, float, float]:
    sample = list(closes[-window:])
    if len(sample) < window:
        raise ValueError('te weinig waarden voor banden')
    middle = fmean(sample)
    sigma = pstdev(sample)
    lower = middle - stddev_mult * sigma
    upper = middle + stddev_mult * sigma
    return lower, middle, upper


class BandReentryStrategy:
    """Long-only mean-reversion entry after price re-enters the lower statistical band."""

    def __init__(self, settings: Settings) -> None:
        self.s = settings

    def evaluate(self, candles: Sequence[Candle]) -> Decision:
        need = self.s.band_window + 1
        if len(candles) < need:
            return Decision('SKIP', 'onvoldoende_data', {})

        closes = [c.close for c in candles]
        prev_lower, prev_mid, _ = _bands(closes[:-1], self.s.band_window, self.s.band_stddev)
        last_lower, last_mid, last_upper = _bands(closes, self.s.band_window, self.s.band_stddev)
        prev_close = closes[-2]
        last_close = closes[-1]

        metrics = {
            'close': last_close,
            'lower_band': last_lower,
            'middle_band': last_mid,
            'upper_band': last_upper,
            'band_width_pct': 0.0 if last_mid <= 0 else ((last_upper - last_lower) / last_mid) * 100.0,
        }

        values = [prev_lower, prev_mid, last_lower, last_mid, last_upper, prev_close, last_close]
        if not all(math.isfinite(v) and v > 0 for v in values):
            return Decision('SKIP', 'ongeldige_bandwaarden', metrics)

        was_below = prev_close < prev_lower
        reentered = last_close >= last_lower
        still_below_mid = last_close < last_mid

        if was_below and reentered and still_below_mid:
            return Decision('BUY', 'lower_band_reentry', metrics)
        return Decision('SKIP', 'geen_reentry', metrics)
