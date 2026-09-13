from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from bitvavo_public import BitvavoPublic
from v40_human_engine import MIN_VOLUME_QUOTE_EUR, V40Settings, evaluate_entry


def scan_all_eur(api: BitvavoPublic, *, output_path: str | None = None) -> dict:
    """Publieke snapshot van alle EUR-markten; geen sleutels en geen orders."""
    active_markets = api.trading_markets('EUR')
    tickers = {
        str(item['market']): item for item in api.quote_market_tickers('EUR')
    }
    btc = api.closed_candles('BTC-EUR', '5m', 120)
    decisions = []
    errors = []
    for market in active_markets:
        ticker = tickers.get(market)
        if ticker is None:
            decisions.append({
                'market': market,
                'action': 'AFWIJZEN',
                'route': 'GEEN_SETUP',
                'score': 0.0,
                'reasons': ['actieve_markt_zonder_bruikbare_ticker'],
                'proposed_paper_eur': 0.0,
                'execution_enabled': False,
                'live_orders_possible': False,
            })
            continue
        try:
            volume_quote = float(ticker['volume_quote'])
            if volume_quote < MIN_VOLUME_QUOTE_EUR:
                candles = []
                spread_pct = 0.0
            else:
                candles = api.closed_candles(market, '5m', 120)
                spread_pct = api.book(market).spread_pct
            decisions.append(evaluate_entry(
                market,
                candles,
                btc,
                volume_quote_eur=volume_quote,
                spread_pct=spread_pct,
                settings=V40Settings(),
            ))
        except Exception as exc:
            errors.append(f'{market}: {type(exc).__name__}: {exc}')
    priority = {'KOOPKANS': 0, 'VOLGEN': 1, 'PUMP_TE_LAAT': 2, 'AFWIJZEN': 3}
    decisions.sort(key=lambda item: (priority.get(str(item['action']), 9), -float(item['score'])))
    report = {
        'version': '4.0-phase-1',
        'component': 'OFFLINE_FULL_EUR_HUMAN_ENGINE',
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'mode': 'OFFLINE_OBSERVE_ONLY',
        'execution_enabled': False,
        'live_orders_possible': False,
        'markets_seen': len(active_markets),
        'markets_evaluated': len(decisions),
        'errors': errors,
        'decisions': decisions,
    }
    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    return report


def print_status(report: dict) -> None:
    print('=== CRYPTOBOT v4.0 FASE 1 | OFFLINE MENSELIJKE ENGINE ===')
    print('UITVOERING             : UIT / TECHNISCH ONMOGELIJK')
    print(f"EUR-markten gezien     : {report['markets_seen']}")
    print(f"EUR-markten beoordeeld : {report['markets_evaluated']}")
    for item in report['decisions'][:15]:
        print(
            f"{item['market']:<14} | {item['action']:<13} | {item['route']:<22}"
            f" | score {float(item['score']):>5.1f} | voorstel €{float(item.get('proposed_paper_eur', 0)):>3.0f}"
        )
    if report['errors']:
        print(f"Dataproblemen          : {len(report['errors'])}")


def main() -> int:
    parser = argparse.ArgumentParser(description='CryptoBot v4.0 offline brede EUR-scan')
    parser.add_argument('--output', default='data/cryptobot_v40_offline.json')
    args = parser.parse_args()
    api = BitvavoPublic('https://api.bitvavo.com/v2')
    report = scan_all_eur(api, output_path=args.output)
    print_status(report)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
