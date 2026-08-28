from __future__ import annotations

import math
from typing import Sequence

from adaptive_trend_strategy import AdaptiveTrendStrategy
from models import Candle, Decision


class AdaptiveLongShortStrategy(AdaptiveTrendStrategy):
    """Tweezijdige trendvolger: BULL=LONG, BEAR=SHORT, SIDEWAYS=geen entry."""

    REGIME_BREADTH_PCT = 45.0

    def analyze(self, candles: Sequence[Candle]) -> dict[str, float]:
        metrics = super().analyze(candles)
        if not metrics:
            return {}

        closes = [c.close for c in candles]
        last = closes[-1]
        prior_low = min(c.low for c in candles[-1-self.PULLBACK_LOOKBACK:-1])
        breakdown_ref = min(closes[-1-self.BREAKOUT_LOOKBACK:-1])
        bounce_pct = ((last / prior_low) - 1.0) * 100.0 if prior_low > 0 else float('nan')
        breakdown_pct = ((breakdown_ref / last) - 1.0) * 100.0 if last > 0 else float('nan')

        extra = {
            'prior_low': prior_low,
            'bounce_pct': bounce_pct,
            'breakdown_ref': breakdown_ref,
            'breakdown_pct': breakdown_pct,
        }
        if not all(math.isfinite(v) for v in extra.values()):
            return {}
        if prior_low <= 0 or breakdown_ref <= 0:
            return {}
        return {**metrics, **extra}

    @classmethod
    def bearish_aligned(cls, metrics: dict[str, float]) -> bool:
        if not metrics:
            return False
        return (
            metrics['close'] < metrics['slow15']
            and metrics['fast15'] < metrics['slow15']
            and metrics['slope15_pct'] < 0.0
            and metrics['fast1h'] < metrics['slow1h']
            and metrics['slope1h_pct'] < 0.0
        )

    @classmethod
    def market_regime(
        cls, metrics_by_market: dict[str, dict[str, float]]
    ) -> tuple[str, float, float]:
        valid = [m for m in metrics_by_market.values() if m]
        if len(valid) < cls.MIN_REGIME_MARKETS:
            return 'UNKNOWN', 0.0, 0.0

        bull = sum(1 for metrics in valid if cls.trend_aligned(metrics))
        bear = sum(1 for metrics in valid if cls.bearish_aligned(metrics))
        bull_pct = bull / len(valid) * 100.0
        bear_pct = bear / len(valid) * 100.0

        if bull_pct >= cls.REGIME_BREADTH_PCT:
            return 'BULL', bull_pct, bear_pct
        if bear_pct >= cls.REGIME_BREADTH_PCT:
            return 'BEAR', bull_pct, bear_pct
        return 'SIDEWAYS', bull_pct, bear_pct

    @classmethod
    def rank_score(cls, decision: Decision) -> float:
        if decision.action == 'LONG':
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

        if decision.action == 'SHORT':
            try:
                score = (
                    1.5 * -float(decision.metrics['fast_gap_pct'])
                    + 2.0 * -float(decision.metrics['one_hour_gap_pct'])
                    + 2.0 * -float(decision.metrics['slope1h_pct'])
                    + 0.6 * -float(decision.metrics['momentum_pct'])
                    + max(0.0, float(decision.metrics['breakdown_pct']))
                )
            except (KeyError, TypeError, ValueError):
                return float('-inf')
            return score if math.isfinite(score) else float('-inf')

        return float('-inf')

    def evaluate_metrics(
        self,
        metrics: dict[str, float],
        regime: str,
        bull_breadth_pct: float,
        bear_breadth_pct: float,
    ) -> Decision:
        if not metrics:
            return Decision('SKIP', 'onvoldoende_adaptive_data', {})

        enriched = {
            **metrics,
            'bull_breadth_pct': float(bull_breadth_pct),
            'bear_breadth_pct': float(bear_breadth_pct),
            'regime_code': 1.0 if regime == 'BULL' else (-1.0 if regime == 'BEAR' else 0.0),
        }

        if regime == 'SIDEWAYS':
            return Decision('SKIP', 'marktregime_sideways', enriched)
        if regime not in {'BULL', 'BEAR'}:
            return Decision('SKIP', 'marktregime_onbekend', enriched)

        if metrics['atr_pct'] < self.MIN_ATR_PCT:
            return Decision('SKIP', 'volatiliteit_te_laag', enriched)
        if metrics['atr_pct'] > self.MAX_ATR_PCT:
            return Decision('SKIP', 'volatiliteit_te_hoog', enriched)

        if regime == 'BULL':
            if not self.trend_aligned(metrics):
                return Decision('SKIP', 'long_cointrend_niet_aligned', enriched)
            if metrics['slope15_pct'] < self.MIN_15M_SLOPE_PCT:
                return Decision('SKIP', 'long_trend_15m_te_zwak', enriched)
            if metrics['slope1h_pct'] < self.MIN_1H_SLOPE_PCT:
                return Decision('SKIP', 'long_trend_1h_te_zwak', enriched)
            if not self.MIN_MOMENTUM_PCT <= metrics['momentum_pct'] <= self.MAX_MOMENTUM_PCT:
                return Decision('SKIP', 'long_momentum_buiten_band', enriched)

            pullback_resume = (
                self.MIN_PULLBACK_PCT <= metrics['pullback_pct'] <= self.MAX_PULLBACK_PCT
                and metrics['close'] > metrics['prev_close']
                and metrics['close'] >= metrics['fast15'] * 0.995
            )
            breakout_resume = metrics['close'] > metrics['breakout_ref'] and metrics['breakout_pct'] > 0.0
            if pullback_resume:
                return Decision('LONG', 'adaptive_v2_long_pullback_resume', enriched)
            if breakout_resume:
                return Decision('LONG', 'adaptive_v2_long_breakout_resume', enriched)
            return Decision('SKIP', 'long_nog_geen_trend_hervatting', enriched)

        if not self.bearish_aligned(metrics):
            return Decision('SKIP', 'short_cointrend_niet_aligned', enriched)
        if metrics['slope15_pct'] > -self.MIN_15M_SLOPE_PCT:
            return Decision('SKIP', 'short_trend_15m_te_zwak', enriched)
        if metrics['slope1h_pct'] > -self.MIN_1H_SLOPE_PCT:
            return Decision('SKIP', 'short_trend_1h_te_zwak', enriched)
        if not -self.MAX_MOMENTUM_PCT <= metrics['momentum_pct'] <= -self.MIN_MOMENTUM_PCT:
            return Decision('SKIP', 'short_momentum_buiten_band', enriched)

        bounce_resume = (
            self.MIN_PULLBACK_PCT <= metrics['bounce_pct'] <= self.MAX_PULLBACK_PCT
            and metrics['close'] < metrics['prev_close']
            and metrics['close'] <= metrics['fast15'] * 1.005
        )
        breakdown_resume = metrics['close'] < metrics['breakdown_ref'] and metrics['breakdown_pct'] > 0.0
        if bounce_resume:
            return Decision('SHORT', 'adaptive_v2_short_bounce_resume', enriched)
        if breakdown_resume:
            return Decision('SHORT', 'adaptive_v2_short_breakdown_resume', enriched)
        return Decision('SKIP', 'short_nog_geen_trend_hervatting', enriched)

    @classmethod
    def exit_reason(cls, metrics: dict[str, float], regime: str, side: str) -> str | None:
        if not metrics:
            return None
        if side == 'LONG':
            if regime == 'BEAR':
                return 'market_reversal'
            if metrics['close'] < metrics['slow15']:
                return 'trend_break_15m'
            if metrics['fast1h'] <= metrics['slow1h']:
                return 'trend_break_1h'
            return None
        if side == 'SHORT':
            if regime == 'BULL':
                return 'market_reversal'
            if metrics['close'] > metrics['slow15']:
                return 'trend_break_15m'
            if metrics['fast1h'] >= metrics['slow1h']:
                return 'trend_break_1h'
            return None
        return 'side_onbekend'
