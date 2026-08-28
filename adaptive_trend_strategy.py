from __future__ import annotations

import math
from statistics import fmean
from typing import Sequence

from config import Settings
from models import Candle, Decision


class AdaptiveTrendStrategy:
    """Long-only marktvolgende strategie met 15m + synthetische 1h trend.

    De regels zijn bewust vast. De strategie verandert zichzelf niet; hij past
    alleen zijn deelname aan op het actuele marktregime en de volatiliteit.
    """

    FAST_15M = 12
    SLOW_15M = 48
    SLOPE_LOOKBACK_15M = 8
    ATR_WINDOW = 14
    MOMENTUM_BARS = 8
    PULLBACK_LOOKBACK = 12
    BREAKOUT_LOOKBACK = 4

    FAST_1H = 6
    SLOW_1H = 24
    SLOPE_LOOKBACK_1H = 3

    MIN_REGIME_MARKETS = 12
    BULL_BREADTH_PCT = 45.0
    BEAR_BREADTH_PCT = 30.0
    MIN_15M_SLOPE_PCT = 0.08
    MIN_1H_SLOPE_PCT = 0.08
    MIN_MOMENTUM_PCT = 0.20
    MAX_MOMENTUM_PCT = 8.00
    MIN_ATR_PCT = 0.20
    MAX_ATR_PCT = 6.00
    MIN_PULLBACK_PCT = 0.20
    MAX_PULLBACK_PCT = 2.75

    def __init__(self, settings: Settings) -> None:
        self.s = settings

    @classmethod
    def required_candles(cls) -> int:
        return max(
            (cls.SLOW_1H + cls.SLOPE_LOOKBACK_1H) * 4,
            cls.SLOW_15M + cls.SLOPE_LOOKBACK_15M,
            cls.ATR_WINDOW + 2,
        )

    @staticmethod
    def _hourly_closes(candles: Sequence[Candle]) -> list[float]:
        buckets: dict[int, list[Candle]] = {}
        for candle in candles:
            hour = candle.timestamp_ms // 3_600_000
            buckets.setdefault(hour, []).append(candle)

        result: list[float] = []
        for hour in sorted(buckets):
            rows = sorted(buckets[hour], key=lambda c: c.timestamp_ms)
            if len(rows) == 4:
                result.append(rows[-1].close)
        return result

    @classmethod
    def _atr_pct(cls, candles: Sequence[Candle]) -> float:
        rows = candles[-(cls.ATR_WINDOW + 1):]
        if len(rows) < cls.ATR_WINDOW + 1:
            return float('nan')
        tr_values: list[float] = []
        for prev, current in zip(rows, rows[1:]):
            tr_values.append(
                max(
                    current.high - current.low,
                    abs(current.high - prev.close),
                    abs(current.low - prev.close),
                )
            )
        atr = fmean(tr_values)
        last = rows[-1].close
        return (atr / last) * 100.0 if last > 0 else float('nan')

    def analyze(self, candles: Sequence[Candle]) -> dict[str, float]:
        if len(candles) < self.required_candles():
            return {}

        closes = [c.close for c in candles]
        hourly = self._hourly_closes(candles)
        if len(hourly) < self.SLOW_1H + self.SLOPE_LOOKBACK_1H:
            return {}

        last = closes[-1]
        prev = closes[-2]
        fast15 = fmean(closes[-self.FAST_15M:])
        slow15 = fmean(closes[-self.SLOW_15M:])
        slow15_prev = fmean(
            closes[-self.SLOW_15M-self.SLOPE_LOOKBACK_15M:-self.SLOPE_LOOKBACK_15M]
        )

        fast1h = fmean(hourly[-self.FAST_1H:])
        slow1h = fmean(hourly[-self.SLOW_1H:])
        slow1h_prev = fmean(
            hourly[-self.SLOW_1H-self.SLOPE_LOOKBACK_1H:-self.SLOPE_LOOKBACK_1H]
        )

        momentum_base = closes[-1-self.MOMENTUM_BARS]
        prior_high = max(c.high for c in candles[-1-self.PULLBACK_LOOKBACK:-1])
        breakout_ref = max(closes[-1-self.BREAKOUT_LOOKBACK:-1])
        atr_pct = self._atr_pct(candles)

        fast_gap_pct = ((fast15 / slow15) - 1.0) * 100.0 if slow15 > 0 else float('nan')
        slope15_pct = ((slow15 / slow15_prev) - 1.0) * 100.0 if slow15_prev > 0 else float('nan')
        slope1h_pct = ((slow1h / slow1h_prev) - 1.0) * 100.0 if slow1h_prev > 0 else float('nan')
        momentum_pct = ((last / momentum_base) - 1.0) * 100.0 if momentum_base > 0 else float('nan')
        pullback_pct = ((prior_high - last) / prior_high) * 100.0 if prior_high > 0 else float('nan')
        breakout_pct = ((last / breakout_ref) - 1.0) * 100.0 if breakout_ref > 0 else float('nan')
        one_hour_gap_pct = ((fast1h / slow1h) - 1.0) * 100.0 if slow1h > 0 else float('nan')

        metrics = {
            'close': last,
            'prev_close': prev,
            'fast15': fast15,
            'slow15': slow15,
            'fast_gap_pct': fast_gap_pct,
            'slope15_pct': slope15_pct,
            'fast1h': fast1h,
            'slow1h': slow1h,
            'one_hour_gap_pct': one_hour_gap_pct,
            'slope1h_pct': slope1h_pct,
            'momentum_pct': momentum_pct,
            'prior_high': prior_high,
            'pullback_pct': pullback_pct,
            'breakout_ref': breakout_ref,
            'breakout_pct': breakout_pct,
            'atr_pct': atr_pct,
        }
        if not all(math.isfinite(v) for v in metrics.values()):
            return {}
        if min(last, prev, fast15, slow15, fast1h, slow1h, prior_high, breakout_ref) <= 0:
            return {}
        return metrics

    @classmethod
    def trend_aligned(cls, metrics: dict[str, float]) -> bool:
        if not metrics:
            return False
        return (
            metrics['close'] > metrics['slow15']
            and metrics['fast15'] > metrics['slow15']
            and metrics['slope15_pct'] > 0.0
            and metrics['fast1h'] > metrics['slow1h']
            and metrics['slope1h_pct'] > 0.0
        )

    @classmethod
    def market_regime(cls, metrics_by_market: dict[str, dict[str, float]]) -> tuple[str, float]:
        valid = [m for m in metrics_by_market.values() if m]
        if len(valid) < cls.MIN_REGIME_MARKETS:
            return 'UNKNOWN', 0.0
        aligned = sum(1 for metrics in valid if cls.trend_aligned(metrics))
        breadth_pct = aligned / len(valid) * 100.0
        if breadth_pct >= cls.BULL_BREADTH_PCT:
            return 'BULL', breadth_pct
        if breadth_pct < cls.BEAR_BREADTH_PCT:
            return 'BEAR', breadth_pct
        return 'SIDEWAYS', breadth_pct

    @classmethod
    def rank_score(cls, decision: Decision) -> float:
        if decision.action != 'BUY':
            return float('-inf')
        try:
            score = (
                1.5 * float(decision.metrics['fast_gap_pct'])
                + 2.0 * float(decision.metrics['one_hour_gap_pct'])
                + 2.0 * float(decision.metrics['slope1h_pct'])
                + 0.6 * float(decision.metrics['momentum_pct'])
                + max(0.0, float(decision.metrics['breakout_pct']))
            )
        except (KeyError, TypeError, ValueError):
            return float('-inf')
        return score if math.isfinite(score) else float('-inf')

    def evaluate_metrics(
        self,
        metrics: dict[str, float],
        regime: str,
        breadth_pct: float,
    ) -> Decision:
        if not metrics:
            return Decision('SKIP', 'onvoldoende_adaptive_data', {})

        enriched = {
            **metrics,
            'market_breadth_pct': float(breadth_pct),
            'regime_code': 1.0 if regime == 'BULL' else (-1.0 if regime == 'BEAR' else 0.0),
        }

        if regime != 'BULL':
            return Decision('SKIP', 'marktregime_niet_bullish', enriched)
        if not self.trend_aligned(metrics):
            return Decision('SKIP', 'cointrend_niet_aligned', enriched)
        if metrics['slope15_pct'] < self.MIN_15M_SLOPE_PCT:
            return Decision('SKIP', 'trend_15m_te_zwak', enriched)
        if metrics['slope1h_pct'] < self.MIN_1H_SLOPE_PCT:
            return Decision('SKIP', 'trend_1h_te_zwak', enriched)
        if metrics['atr_pct'] < self.MIN_ATR_PCT:
            return Decision('SKIP', 'volatiliteit_te_laag', enriched)
        if metrics['atr_pct'] > self.MAX_ATR_PCT:
            return Decision('SKIP', 'volatiliteit_te_hoog', enriched)
        if metrics['momentum_pct'] < self.MIN_MOMENTUM_PCT:
            return Decision('SKIP', 'momentum_te_zwak', enriched)
        if metrics['momentum_pct'] > self.MAX_MOMENTUM_PCT:
            return Decision('SKIP', 'momentum_te_hoog', enriched)

        pullback_resume = (
            self.MIN_PULLBACK_PCT <= metrics['pullback_pct'] <= self.MAX_PULLBACK_PCT
            and metrics['close'] > metrics['prev_close']
            and metrics['close'] >= metrics['fast15'] * 0.995
        )
        breakout_resume = (
            metrics['close'] > metrics['breakout_ref']
            and metrics['breakout_pct'] > 0.0
        )

        if pullback_resume:
            return Decision('BUY', 'adaptive_pullback_resume', enriched)
        if breakout_resume:
            return Decision('BUY', 'adaptive_breakout_resume', enriched)
        return Decision('SKIP', 'nog_geen_trend_hervatting', enriched)

    @classmethod
    def exit_reason(cls, metrics: dict[str, float], regime: str) -> str | None:
        if not metrics:
            return None
        if metrics['close'] < metrics['slow15']:
            return 'adaptive_trend_break_15m'
        if metrics['fast1h'] <= metrics['slow1h']:
            return 'adaptive_trend_break_1h'
        if regime == 'BEAR' and metrics['close'] < metrics['fast15']:
            return 'adaptive_market_regime_exit'
        return None
