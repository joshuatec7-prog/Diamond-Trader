from __future__ import annotations

from adaptive_ls_strategy import AdaptiveLongShortStrategy
from models import Decision


class StrictAdaptiveLongShortStrategy(AdaptiveLongShortStrategy):
    """Researchvariant van D v2 met strengere SHORT-selectie.

    LONG blijft gelijk aan D v2. SHORT vereist een breder/sterker BEAR-regime,
    sterkere 15m+1h daling en alleen een bevestigde breakdown-entry. De actieve
    D v2-regels worden hiermee niet gewijzigd.
    """

    STRICT_BEAR_BREADTH_PCT = 60.0
    STRICT_SHORT_SLOPE_15M_PCT = 0.12
    STRICT_SHORT_SLOPE_1H_PCT = 0.12
    STRICT_SHORT_MIN_MOMENTUM_PCT = 0.35
    STRICT_SHORT_MAX_MOMENTUM_PCT = 6.00
    STRICT_MAX_BELOW_FAST15_PCT = 3.00

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
        if bear_pct >= cls.STRICT_BEAR_BREADTH_PCT:
            return 'BEAR', bull_pct, bear_pct
        return 'SIDEWAYS', bull_pct, bear_pct

    def evaluate_metrics(
        self,
        metrics: dict[str, float],
        regime: str,
        bull_breadth_pct: float,
        bear_breadth_pct: float,
    ) -> Decision:
        # LONG-kant exact gelijk houden aan D v2 zodat alleen SHORT-selectie
        # onderwerp van deze researchvariant is.
        if regime != 'BEAR':
            return super().evaluate_metrics(metrics, regime, bull_breadth_pct, bear_breadth_pct)

        if not metrics:
            return Decision('SKIP', 'onvoldoende_adaptive_data', {})

        enriched = {
            **metrics,
            'bull_breadth_pct': float(bull_breadth_pct),
            'bear_breadth_pct': float(bear_breadth_pct),
            'regime_code': -1.0,
            'strict_short': 1.0,
        }

        if bear_breadth_pct < self.STRICT_BEAR_BREADTH_PCT:
            return Decision('SKIP', 'strict_bear_breadth_te_laag', enriched)
        if metrics['atr_pct'] < self.MIN_ATR_PCT:
            return Decision('SKIP', 'volatiliteit_te_laag', enriched)
        if metrics['atr_pct'] > self.MAX_ATR_PCT:
            return Decision('SKIP', 'volatiliteit_te_hoog', enriched)
        if not self.bearish_aligned(metrics):
            return Decision('SKIP', 'strict_short_cointrend_niet_aligned', enriched)
        if metrics['slope15_pct'] > -self.STRICT_SHORT_SLOPE_15M_PCT:
            return Decision('SKIP', 'strict_short_trend_15m_te_zwak', enriched)
        if metrics['slope1h_pct'] > -self.STRICT_SHORT_SLOPE_1H_PCT:
            return Decision('SKIP', 'strict_short_trend_1h_te_zwak', enriched)
        if not -self.STRICT_SHORT_MAX_MOMENTUM_PCT <= metrics['momentum_pct'] <= -self.STRICT_SHORT_MIN_MOMENTUM_PCT:
            return Decision('SKIP', 'strict_short_momentum_buiten_band', enriched)

        below_fast_pct = ((metrics['fast15'] / metrics['close']) - 1.0) * 100.0
        enriched['below_fast15_pct'] = below_fast_pct
        if below_fast_pct > self.STRICT_MAX_BELOW_FAST15_PCT:
            return Decision('SKIP', 'strict_short_te_ver_uitgerekt', enriched)

        # Geen bounce-entry in de strikte variant: alleen nieuwe neerwaartse
        # bevestiging via een echte breakdown.
        breakdown_resume = metrics['close'] < metrics['breakdown_ref'] and metrics['breakdown_pct'] > 0.0
        if breakdown_resume:
            return Decision('SHORT', 'adaptive_v2s_short_breakdown_confirmed', enriched)
        return Decision('SKIP', 'strict_short_wacht_op_breakdown', enriched)
