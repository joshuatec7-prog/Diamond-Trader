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

from adaptive_ls_strategy import AdaptiveLongShortStrategy
from adaptive_ls_strict_strategy import StrictAdaptiveLongShortStrategy
from bitvavo_public import BitvavoPublic
from config import Settings
from strategy import BandReentryStrategy

logger = logging.getLogger('cryptobot_scanner_v1')
STOP = False


def _report_path() -> Path:
    raw = os.getenv('SCANNER_REPORT_PATH')
    if raw:
        return Path(raw)
    data = Path('/var/data')
    if data.exists() and os.access(data, os.W_OK):
        return data / 'cryptobot_scanner_v1.json'
    return Path('data') / 'cryptobot_scanner_v1.json'


def _stop(signum: int, frame: object) -> None:
    global STOP
    STOP = True
    logger.info('stop-signaal ontvangen: %s', signum)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _roundtrip_cost_pct(settings: Settings, spread_pct: float) -> float:
    return (
        2.0 * settings.taker_fee_pct
        + 2.0 * settings.slippage_pct
        + max(0.0, spread_pct)
    )


def _movement_proxy_pct(metrics: dict[str, float]) -> float:
    # Geen voorspelling: alleen een transparante technische bewegingsmaat.
    return max(
        abs(float(metrics.get('momentum_pct', 0.0))),
        2.0 * abs(float(metrics.get('atr_pct', 0.0))),
    )


def _direction_score(
    metrics: dict[str, float],
    side: str,
    cost_pct: float,
    spread_pct: float,
    max_spread_pct: float,
) -> float:
    if not metrics or side not in {'LONG', 'SHORT'}:
        return 0.0

    sign = 1.0 if side == 'LONG' else -1.0
    aligned = (
        AdaptiveLongShortStrategy.trend_aligned(metrics)
        if side == 'LONG'
        else AdaptiveLongShortStrategy.bearish_aligned(metrics)
    )

    one_hour_gap = sign * float(metrics.get('one_hour_gap_pct', 0.0))
    slope_1h = sign * float(metrics.get('slope1h_pct', 0.0))
    slope_15m = sign * float(metrics.get('slope15_pct', 0.0))
    momentum = sign * float(metrics.get('momentum_pct', 0.0))
    setup_move = float(
        metrics.get('breakout_pct', 0.0)
        if side == 'LONG'
        else metrics.get('breakdown_pct', 0.0)
    )
    movement = _movement_proxy_pct(metrics)
    cost_multiple = movement / cost_pct if cost_pct > 0 else 0.0

    score = 0.0
    score += 25.0 if aligned else 0.0
    score += 15.0 * _clamp(one_hour_gap / 0.75)
    score += 15.0 * _clamp(slope_1h / 0.30)
    score += 10.0 * _clamp(slope_15m / 0.30)
    score += 10.0 * _clamp(momentum / 2.0)
    score += 10.0 * _clamp(setup_move / 0.50)
    score += 10.0 * _clamp((cost_multiple - 1.0) / 2.0)
    score += 5.0 * _clamp(1.0 - spread_pct / max(max_spread_pct, 1e-9))
    return round(_clamp(score, 0.0, 100.0), 1)


def _sideways_score(
    band_metrics: dict[str, float],
    movement_proxy_pct: float,
    cost_pct: float,
    spread_pct: float,
    max_spread_pct: float,
) -> float:
    if not band_metrics:
        return 0.0
    close = float(band_metrics.get('close', 0.0))
    lower = float(band_metrics.get('lower_band', 0.0))
    middle = float(band_metrics.get('middle_band', 0.0))
    prev_close = float(band_metrics.get('prev_close', 0.0))
    if min(close, lower, middle, prev_close) <= 0 or middle <= lower:
        return 0.0

    position = (close - lower) / (middle - lower)
    near_lower = 1.0 - _clamp(position)
    recovery = 1.0 if close > prev_close else 0.0
    cost_multiple = movement_proxy_pct / cost_pct if cost_pct > 0 else 0.0

    # Sideways blijft bewust onder 75: alleen WATCH, nooit automatisch een trade-label.
    score = (
        30.0 * near_lower
        + 15.0 * recovery
        + 15.0 * _clamp((cost_multiple - 1.0) / 2.0)
        + 10.0 * _clamp(1.0 - spread_pct / max(max_spread_pct, 1e-9))
    )
    return round(_clamp(score, 0.0, 70.0), 1)


def _reasons(metrics: dict[str, float], side: str, cost_multiple: float, spread_pct: float) -> list[str]:
    reasons: list[str] = []
    if side == 'LONG':
        if AdaptiveLongShortStrategy.trend_aligned(metrics):
            reasons.append('15m+1h trend omhoog')
        if float(metrics.get('breakout_pct', 0.0)) > 0:
            reasons.append('breakout bevestigd')
        if float(metrics.get('momentum_pct', 0.0)) > 0:
            reasons.append('positief momentum')
    elif side == 'SHORT':
        if AdaptiveLongShortStrategy.bearish_aligned(metrics):
            reasons.append('15m+1h trend omlaag')
        if float(metrics.get('breakdown_pct', 0.0)) > 0:
            reasons.append('breakdown bevestigd')
        if float(metrics.get('momentum_pct', 0.0)) < 0:
            reasons.append('negatief momentum')
    if cost_multiple >= 2.0:
        reasons.append('bewegingsruimte > 2x kosten')
    elif cost_multiple < 1.5:
        reasons.append('bewegingsruimte krap t.o.v. kosten')
    if spread_pct > 0.25:
        reasons.append('spread relatief hoog')
    return reasons[:4]


def _price_plan(close: float, atr_pct: float, cost_pct: float, side: str) -> dict[str, float]:
    atr_price = close * max(atr_pct, 0.0) / 100.0
    zone_half = 0.15 * atr_price
    stop_pct = _clamp(1.5 * atr_pct, 1.25, 3.50)
    target_pct = _clamp(max(2.5 * atr_pct, 2.5 * cost_pct), 2.00, 8.00)
    if side == 'SHORT':
        stop = close * (1.0 + stop_pct / 100.0)
        target = close * (1.0 - target_pct / 100.0)
    else:
        stop = close * (1.0 - stop_pct / 100.0)
        target = close * (1.0 + target_pct / 100.0)
    return {
        'entry_zone_low': close - zone_half,
        'entry_zone_high': close + zone_half,
        'reference_price': close,
        'stop_hint': stop,
        'stop_hint_pct': stop_pct,
        'target_hint': target,
        'target_hint_pct': target_pct,
    }


def scan_once(settings: Settings) -> dict[str, object]:
    api = BitvavoPublic(
        settings.api_base_url,
        settings.request_timeout_seconds,
        settings.request_retries,
    )
    markets = api.top_markets_by_quote_volume(settings.quote_currency, settings.universe_size)
    analyzer = StrictAdaptiveLongShortStrategy(settings)
    directional = AdaptiveLongShortStrategy(settings)
    band = BandReentryStrategy(settings)

    metrics_by_market: dict[str, dict[str, float]] = {}
    candles_by_market: dict[str, list] = {}
    errors: list[str] = []

    candle_limit = max(settings.candle_limit, analyzer.required_candles() + 8)
    for market in markets:
        try:
            candles = api.closed_candles(market, settings.interval, candle_limit)
            metrics = analyzer.analyze(candles)
            if not metrics:
                errors.append(f'{market}: onvoldoende analyse-data')
                continue
            candles_by_market[market] = candles
            metrics_by_market[market] = metrics
        except Exception as exc:
            errors.append(f'{market}: {type(exc).__name__}: {exc}')

    regime, bull_breadth, bear_breadth = analyzer.market_regime(metrics_by_market)
    rows: list[dict[str, object]] = []

    for market, metrics in metrics_by_market.items():
        try:
            book = api.book(market)
            spread_pct = book.spread_pct
        except Exception as exc:
            errors.append(f'{market} book: {type(exc).__name__}: {exc}')
            continue

        cost_pct = _roundtrip_cost_pct(settings, spread_pct)
        movement_proxy = _movement_proxy_pct(metrics)
        cost_multiple = movement_proxy / cost_pct if cost_pct > 0 else 0.0
        close = float(metrics['close'])
        atr_pct = float(metrics['atr_pct'])

        side = 'NONE'
        score = 0.0
        action = 'GEEN TRADE'
        decision_reason = ''

        if regime == 'BULL':
            side = 'LONG'
            score = _direction_score(metrics, side, cost_pct, spread_pct, settings.max_spread_pct)
            decision = directional.evaluate_metrics(metrics, regime, bull_breadth, bear_breadth)
            decision_reason = decision.reason
            if (
                decision.action == 'LONG'
                and score >= 75.0
                and cost_multiple >= 2.0
                and spread_pct <= settings.max_spread_pct
            ):
                action = 'LONG KANS'
            elif score >= 60.0:
                action = 'LONG WATCH'

        elif regime == 'BEAR':
            side = 'SHORT'
            score = _direction_score(metrics, side, cost_pct, spread_pct, settings.max_spread_pct)
            decision = analyzer.evaluate_metrics(metrics, regime, bull_breadth, bear_breadth)
            decision_reason = decision.reason
            if (
                decision.action == 'SHORT'
                and score >= 75.0
                and cost_multiple >= 2.0
                and spread_pct <= settings.max_spread_pct
            ):
                action = 'SHORT KANS'
            elif score >= 60.0:
                action = 'SHORT WATCH'

        elif regime == 'SIDEWAYS':
            side = 'LONG'
            band_decision = band.evaluate(candles_by_market[market])
            score = _sideways_score(
                band_decision.metrics,
                movement_proxy,
                cost_pct,
                spread_pct,
                settings.max_spread_pct,
            )
            decision_reason = band_decision.reason
            if band_decision.action == 'BUY' and score >= 55.0 and cost_multiple >= 2.0:
                action = 'SIDEWAYS WATCH'

        plan = _price_plan(close, atr_pct, cost_pct, side if side != 'NONE' else 'LONG')
        reasons = _reasons(metrics, side, cost_multiple, spread_pct)
        if regime == 'SIDEWAYS' and action == 'SIDEWAYS WATCH':
            reasons.insert(0, 'band-recovery alleen ter observatie')

        rows.append({
            'market': market,
            'action': action,
            'side': side,
            'score': score,
            'decision_reason': decision_reason,
            'spread_pct': round(spread_pct, 4),
            'roundtrip_cost_pct': round(cost_pct, 4),
            'movement_proxy_pct': round(movement_proxy, 4),
            'cost_multiple': round(cost_multiple, 2),
            'atr_pct': round(atr_pct, 4),
            'momentum_pct': round(float(metrics.get('momentum_pct', 0.0)), 4),
            'reasons': reasons,
            **{k: round(v, 8) for k, v in plan.items()},
        })

    rows.sort(key=lambda item: (float(item['score']), float(item['cost_multiple'])), reverse=True)
    generated_ms = int(time.time() * 1000)
    return {
        'version': '1.0',
        'mode': 'READ_ONLY_SCANNER',
        'generated_at_ms': generated_ms,
        'generated_at_utc': datetime.fromtimestamp(generated_ms / 1000.0, tz=timezone.utc).isoformat(),
        'interval': settings.interval,
        'universe': markets,
        'regime': regime,
        'bull_breadth_pct': round(bull_breadth, 1),
        'bear_breadth_pct': round(bear_breadth, 1),
        'valid_markets': len(metrics_by_market),
        'top3': rows[:3],
        'candidates': rows,
        'errors': errors,
        'note': 'Technische scanner; score is geen winstverwachting en opent geen orders.',
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


def _fmt_price(value: object) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 'n/a'
    if not math.isfinite(number):
        return 'n/a'
    if number >= 1000:
        return f'{number:.2f}'
    if number >= 1:
        return f'{number:.4f}'
    return f'{number:.8f}'


def print_report(report: dict[str, object]) -> None:
    print('=== CRYPTO SCANNER v1 | READ ONLY ===')
    print(f"UTC             : {report.get('generated_at_utc', 'n/a')}")
    print(f"REGIME          : {report.get('regime', 'UNKNOWN')}")
    print(f"BULL BREADTH    : {float(report.get('bull_breadth_pct', 0.0)):.1f}%")
    print(f"BEAR BREADTH    : {float(report.get('bear_breadth_pct', 0.0)):.1f}%")
    print(f"MARKTEN GELDIG  : {report.get('valid_markets', 0)}/{len(report.get('universe', []))}")
    print('ORDERS          : ONMOGELIJK | scanner heeft geen private trading-capability')
    print()
    print('=== TOP 3 ===')
    top3 = report.get('top3', [])
    if not isinstance(top3, list) or not top3:
        print('geen kandidaten')
    else:
        for index, row in enumerate(top3, 1):
            if not isinstance(row, dict):
                continue
            print(
                f"{index}. {row.get('market','?'):12s} | {row.get('action','GEEN TRADE'):14s}"
                f" | score {float(row.get('score',0.0)):5.1f}/100"
                f" | kosten {float(row.get('roundtrip_cost_pct',0.0)):.2f}%"
                f" | beweging {float(row.get('movement_proxy_pct',0.0)):.2f}%"
                f" | xkosten {float(row.get('cost_multiple',0.0)):.2f}"
            )
            print(
                f"   zone {_fmt_price(row.get('entry_zone_low'))} - {_fmt_price(row.get('entry_zone_high'))}"
                f" | stop-hint {_fmt_price(row.get('stop_hint'))}"
                f" | target-hint {_fmt_price(row.get('target_hint'))}"
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
    print('LET OP: score = technische rangschikking, geen bewezen winstverwachting.')


def main() -> int:
    parser = argparse.ArgumentParser(description='Crypto Scanner v1 - public data, read only')
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
            print('=== CRYPTO SCANNER v1 | READ ONLY ===')
            print('STATUS          : nog geen rapport beschikbaar')
            print(f'RAPPORT         : {_report_path()}')
            return 1
        print_report(report)
        return 0

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    poll_seconds = max(60, int(os.getenv('SCANNER_POLL_SECONDS', '900')))

    while not STOP:
        try:
            report = scan_once(settings)
            _write_report(report)
            print_report(report)
        except Exception as exc:
            logger.exception('scanner-cyclus mislukt: %s', exc)
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
