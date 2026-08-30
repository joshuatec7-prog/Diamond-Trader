from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from adaptive_ls_strategy import AdaptiveLongShortStrategy
from adaptive_ls_strict_strategy import StrictAdaptiveLongShortStrategy
from bitvavo_public import BitvavoPublic
from config import Settings
from crypto_scanner import (
    _direction_score,
    _fmt_price,
    _movement_proxy_pct,
    _price_plan,
    _reasons,
    _sideways_score,
)
from strategy import BandReentryStrategy

logger = logging.getLogger('cryptobot_scanner_v2')
STOP = False

EUR_TAKER_FEE_PCT = 0.25
USDC_TAKER_FEE_PCT = 0.05
TRADE_GRADE_SCORE = 80.0
TRADE_GRADE_COST_MULTIPLE = 3.0
WATCH_SCORE = 65.0
WATCH_COST_MULTIPLE = 2.0
TRADE_GRADE_MAX_SPREAD_PCT = 0.20


def _report_path() -> Path:
    raw = os.getenv('SCANNER_V2_REPORT_PATH')
    if raw:
        return Path(raw)
    data = Path('/var/data')
    if data.exists() and os.access(data, os.W_OK):
        return data / 'cryptobot_scanner_v2.json'
    return Path('data') / 'cryptobot_scanner_v2.json'


def _stop(signum: int, frame: object) -> None:
    global STOP
    STOP = True
    logger.info('stop-signaal ontvangen: %s', signum)


def _base_asset(market: str) -> str:
    return market.rsplit('-', 1)[0].upper()


def _quote_asset(market: str) -> str:
    return market.rsplit('-', 1)[-1].upper()


def _taker_fee_pct(quote: str) -> float:
    if quote == 'USDC':
        return USDC_TAKER_FEE_PCT
    return EUR_TAKER_FEE_PCT


def _roundtrip_cost_pct(settings: Settings, spread_pct: float, quote: str) -> float:
    return 2.0 * _taker_fee_pct(quote) + 2.0 * settings.slippage_pct + max(0.0, spread_pct)


def _grade_action(
    regime: str,
    decision_action: str,
    score: float,
    cost_multiple: float,
    spread_pct: float,
) -> str:
    desired = 'LONG' if regime == 'BULL' else 'SHORT' if regime == 'BEAR' else ''
    if not desired:
        return 'GEEN TRADE'
    if (
        decision_action == desired
        and score >= TRADE_GRADE_SCORE
        and cost_multiple >= TRADE_GRADE_COST_MULTIPLE
        and spread_pct <= TRADE_GRADE_MAX_SPREAD_PCT
    ):
        return f'{desired} TRADE-GRADE'
    if score >= WATCH_SCORE and cost_multiple >= WATCH_COST_MULTIPLE:
        return f'{desired} WATCH'
    return 'GEEN TRADE'


def _pair_snapshot(
    *,
    api: BitvavoPublic,
    settings: Settings,
    analyzer: StrictAdaptiveLongShortStrategy,
    directional: AdaptiveLongShortStrategy,
    band: BandReentryStrategy,
    market: str,
    regime: str,
    bull_breadth: float,
    bear_breadth: float,
    candle_limit: int,
    cached_candles: list | None = None,
    cached_metrics: dict[str, float] | None = None,
) -> dict[str, Any]:
    quote = _quote_asset(market)
    candles = cached_candles or api.closed_candles(market, settings.interval, candle_limit)
    metrics = cached_metrics or analyzer.analyze(candles)
    if not metrics:
        raise RuntimeError('onvoldoende analyse-data')

    book = api.book(market)
    spread_pct = book.spread_pct
    cost_pct = _roundtrip_cost_pct(settings, spread_pct, quote)
    movement_proxy = _movement_proxy_pct(metrics)
    cost_multiple = movement_proxy / cost_pct if cost_pct > 0 else 0.0
    close = float(metrics['close'])
    atr_pct = float(metrics['atr_pct'])

    side = 'NONE'
    score = 0.0
    decision_action = 'SKIP'
    decision_reason = ''
    action = 'GEEN TRADE'

    if regime == 'BULL':
        side = 'LONG'
        score = _direction_score(metrics, side, cost_pct, spread_pct, settings.max_spread_pct)
        decision = directional.evaluate_metrics(metrics, regime, bull_breadth, bear_breadth)
        decision_action = decision.action
        decision_reason = decision.reason
        action = _grade_action(regime, decision_action, score, cost_multiple, spread_pct)
    elif regime == 'BEAR':
        side = 'SHORT'
        score = _direction_score(metrics, side, cost_pct, spread_pct, settings.max_spread_pct)
        decision = analyzer.evaluate_metrics(metrics, regime, bull_breadth, bear_breadth)
        decision_action = decision.action
        decision_reason = decision.reason
        action = _grade_action(regime, decision_action, score, cost_multiple, spread_pct)
    elif regime == 'SIDEWAYS':
        side = 'LONG'
        band_decision = band.evaluate(candles)
        score = _sideways_score(
            band_decision.metrics,
            movement_proxy,
            cost_pct,
            spread_pct,
            settings.max_spread_pct,
        )
        decision_action = band_decision.action
        decision_reason = band_decision.reason
        if band_decision.action == 'BUY' and score >= 55.0 and cost_multiple >= WATCH_COST_MULTIPLE:
            action = 'SIDEWAYS WATCH'

    plan = _price_plan(close, atr_pct, cost_pct, side if side != 'NONE' else 'LONG')
    reasons = _reasons(metrics, side, cost_multiple, spread_pct)
    if quote == 'USDC':
        reasons.insert(0, 'USDC fee 0,05% per kant')
    if regime == 'SIDEWAYS' and action == 'SIDEWAYS WATCH':
        reasons.insert(0, 'sideways alleen observatie')

    return {
        'market': market,
        'base': _base_asset(market),
        'quote': quote,
        'action': action,
        'side': side,
        'score': round(score, 1),
        'decision_action': decision_action,
        'decision_reason': decision_reason,
        'spread_pct': round(spread_pct, 4),
        'taker_fee_pct': _taker_fee_pct(quote),
        'roundtrip_cost_pct': round(cost_pct, 4),
        'movement_proxy_pct': round(movement_proxy, 4),
        'cost_multiple': round(cost_multiple, 2),
        'three_x_cost_margin_pct': round(movement_proxy - 3.0 * cost_pct, 4),
        'atr_pct': round(atr_pct, 4),
        'momentum_pct': round(float(metrics.get('momentum_pct', 0.0)), 4),
        'reasons': reasons[:5],
        **{key: round(value, 8) for key, value in plan.items()},
    }


def _display_action(action: object) -> str:
    text = str(action or 'GEEN TRADE')
    if text == 'LONG TRADE-GRADE':
        return 'ZELDZAME KANS LONG'
    if text == 'SHORT TRADE-GRADE':
        return 'ZELDZAME KANS SHORT'
    return text


def _rank_key(row: dict[str, Any]) -> tuple[float, float, float, float]:
    action = str(row.get('action', ''))
    grade = 2.0 if 'TRADE-GRADE' in action else 1.0 if 'WATCH' in action else 0.0
    return (
        grade,
        float(row.get('three_x_cost_margin_pct', -999.0)),
        float(row.get('score', 0.0)),
        float(row.get('cost_multiple', 0.0)),
    )


def scan_once(settings: Settings) -> dict[str, object]:
    api = BitvavoPublic(
        settings.api_base_url,
        settings.request_timeout_seconds,
        settings.request_retries,
    )
    analyzer = StrictAdaptiveLongShortStrategy(settings)
    directional = AdaptiveLongShortStrategy(settings)
    band = BandReentryStrategy(settings)

    eur_markets = api.top_markets_by_quote_volume('EUR', settings.universe_size)
    usdc_markets = set(api.trading_markets('USDC'))
    candle_limit = max(settings.candle_limit, analyzer.required_candles() + 8)

    eur_candles: dict[str, list] = {}
    eur_metrics: dict[str, dict[str, float]] = {}
    errors: list[str] = []

    for market in eur_markets:
        try:
            candles = api.closed_candles(market, settings.interval, candle_limit)
            metrics = analyzer.analyze(candles)
            if not metrics:
                errors.append(f'{market}: onvoldoende analyse-data')
                continue
            eur_candles[market] = candles
            eur_metrics[market] = metrics
        except Exception as exc:
            errors.append(f'{market}: {type(exc).__name__}: {exc}')

    regime, bull_breadth, bear_breadth = analyzer.market_regime(eur_metrics)
    chosen: list[dict[str, Any]] = []
    all_pairs: list[dict[str, Any]] = []
    usdc_available = 0

    for eur_market in eur_markets:
        if eur_market not in eur_metrics:
            continue
        base = _base_asset(eur_market)
        pair_options = [eur_market]
        usdc_market = f'{base}-USDC'
        if usdc_market in usdc_markets:
            pair_options.append(usdc_market)
            usdc_available += 1

        rows: list[dict[str, Any]] = []
        for market in pair_options:
            try:
                if market == eur_market:
                    row = _pair_snapshot(
                        api=api,
                        settings=settings,
                        analyzer=analyzer,
                        directional=directional,
                        band=band,
                        market=market,
                        regime=regime,
                        bull_breadth=bull_breadth,
                        bear_breadth=bear_breadth,
                        candle_limit=candle_limit,
                        cached_candles=eur_candles[market],
                        cached_metrics=eur_metrics[market],
                    )
                else:
                    row = _pair_snapshot(
                        api=api,
                        settings=settings,
                        analyzer=analyzer,
                        directional=directional,
                        band=band,
                        market=market,
                        regime=regime,
                        bull_breadth=bull_breadth,
                        bear_breadth=bear_breadth,
                        candle_limit=candle_limit,
                    )
                rows.append(row)
                all_pairs.append(row)
            except Exception as exc:
                errors.append(f'{market}: {type(exc).__name__}: {exc}')

        if rows:
            best = max(rows, key=_rank_key)
            best = {**best, 'pair_options': [row['market'] for row in rows]}
            chosen.append(best)

    chosen.sort(key=_rank_key, reverse=True)
    trade_grade = [row for row in chosen if 'TRADE-GRADE' in str(row.get('action', ''))]
    generated_ms = int(time.time() * 1000)
    return {
        'version': '2.0',
        'mode': 'READ_ONLY_SCANNER',
        'generated_at_ms': generated_ms,
        'generated_at_utc': datetime.fromtimestamp(generated_ms / 1000.0, tz=timezone.utc).isoformat(),
        'interval': settings.interval,
        'reference_universe': eur_markets,
        'regime': regime,
        'bull_breadth_pct': round(bull_breadth, 1),
        'bear_breadth_pct': round(bear_breadth, 1),
        'valid_reference_markets': len(eur_metrics),
        'usdc_available_for_reference_assets': usdc_available,
        'trade_grade_count': len(trade_grade),
        'trade_grade': trade_grade,
        'rare_opportunity_count': len(trade_grade),
        'rare_opportunities': trade_grade,
        'top3': chosen[:3],
        'candidates': chosen,
        'all_pair_snapshots': all_pairs,
        'errors': errors,
        'rules': {
            'trade_grade_score_min': TRADE_GRADE_SCORE,
            'trade_grade_cost_multiple_min': TRADE_GRADE_COST_MULTIPLE,
            'trade_grade_max_spread_pct': TRADE_GRADE_MAX_SPREAD_PCT,
            'eur_taker_fee_pct': EUR_TAKER_FEE_PCT,
            'usdc_taker_fee_pct': USDC_TAKER_FEE_PCT,
        },
        'note': 'Zeldzame kansen zijn een strenge beslisfilter, geen koop- of verkoopadvies. De scanner opent nooit orders.',
    }


def _write_report(report: dict[str, object]) -> None:
    path = _report_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def _load_report() -> dict[str, object] | None:
    path = _report_path()
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def print_report(report: dict[str, object]) -> None:
    print('=== CRYPTO SCANNER v2 | EUR + USDC | READ ONLY ===')
    print(f"UTC             : {report.get('generated_at_utc', 'n/a')}")
    print(f"REGIME          : {report.get('regime', 'UNKNOWN')}")
    print(f"BULL BREADTH    : {float(report.get('bull_breadth_pct', 0.0)):.1f}%")
    print(f"BEAR BREADTH    : {float(report.get('bear_breadth_pct', 0.0)):.1f}%")
    print(f"MARKTEN GELDIG  : {report.get('valid_reference_markets', 0)}/{len(report.get('reference_universe', []))}")
    print(f"USDC BESCHIKBAAR: {report.get('usdc_available_for_reference_assets', 0)}/{len(report.get('reference_universe', []))}")
    print(f"ZELDZAME KANSEN : {report.get('rare_opportunity_count', report.get('trade_grade_count', 0))}")
    print('BESLISSING       : ALTIJD ZELF | dit is geen koop- of verkoopadvies')
    print('ORDERS           : ONMOGELIJK | scanner heeft geen private trading-capability')
    print()
    print('=== ZELDZAME KANSEN — ZELF BESLISSEN ===')
    rare = report.get('rare_opportunities', report.get('trade_grade', []))
    if not isinstance(rare, list) or not rare:
        print('geen uitzonderlijke kans; niets doen is nu de standaard')
    else:
        for index, row in enumerate(rare, 1):
            if not isinstance(row, dict):
                continue
            print(
                f"{index}. {str(row.get('market','?')):12s} | {_display_action(row.get('action'))}"
                f" | score {float(row.get('score',0.0)):.1f}/100"
                f" | xkosten {float(row.get('cost_multiple',0.0)):.2f}"
                f" | spread {float(row.get('spread_pct',0.0)):.3f}%"
            )
            print(
                f"   besliszone {_fmt_price(row.get('entry_zone_low'))} - {_fmt_price(row.get('entry_zone_high'))}"
                f" | ongeldig onder/boven {_fmt_price(row.get('stop_hint'))}"
                f" | technisch doel {_fmt_price(row.get('target_hint'))}"
            )
            print('   controleer zelf: actueel nieuws, orderboek, positieomvang en of de koers nog in de besliszone ligt')
    print()
    print('=== TOP 3 OBSERVATIES ===')
    top3 = report.get('top3', [])
    if not isinstance(top3, list) or not top3:
        print('geen kandidaten')
    else:
        for index, row in enumerate(top3, 1):
            if not isinstance(row, dict):
                continue
            print(
                f"{index}. {str(row.get('market','?')):12s} | {_display_action(row.get('action')):20s}"
                f" | score {float(row.get('score',0.0)):5.1f}/100"
                f" | kosten {float(row.get('roundtrip_cost_pct',0.0)):.2f}%"
                f" | beweging {float(row.get('movement_proxy_pct',0.0)):.2f}%"
                f" | xkosten {float(row.get('cost_multiple',0.0)):.2f}"
            )
            print(
                f"   opties {', '.join(str(x) for x in row.get('pair_options', []))}"
                f" | zone {_fmt_price(row.get('entry_zone_low'))} - {_fmt_price(row.get('entry_zone_high'))}"
                f" | stop {_fmt_price(row.get('stop_hint'))} | target {_fmt_price(row.get('target_hint'))}"
            )
            reasons = row.get('reasons', [])
            if isinstance(reasons, list) and reasons:
                print('   reden: ' + '; '.join(str(x) for x in reasons))
    errors = report.get('errors', [])
    if isinstance(errors, list) and errors:
        print()
        print(f'WAARSCHUWINGEN   : {len(errors)}')
        for text in errors[:5]:
            print(f'  - {text}')
    print()
    print('LET OP: een ZELDZAME KANS is een strenge beslisfilter, geen bewezen winstverwachting.')


def main() -> int:
    parser = argparse.ArgumentParser(description='Crypto Scanner v2 - EUR/USDC cost aware, read only')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()

    settings = Settings()
    settings.validate()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    if args.status:
        report = _load_report()
        if report is None:
            print('=== CRYPTO SCANNER v2 | EUR + USDC | READ ONLY ===')
            print('STATUS          : nog geen rapport beschikbaar')
            print(f'RAPPORT         : {_report_path()}')
            return 1
        print_report(report)
        return 0

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    poll_seconds = max(60, int(os.getenv('SCANNER_V2_POLL_SECONDS', '900')))

    while not STOP:
        try:
            report = scan_once(settings)
            _write_report(report)
            print_report(report)
        except Exception as exc:
            logger.exception('scanner-v2-cyclus mislukt: %s', exc)
            if args.once:
                return 2
        if args.once:
            return 0
        for _ in range(poll_seconds):
            if STOP:
                break
            time.sleep(1)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
