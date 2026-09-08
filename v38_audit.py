from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from bitvavo_public import BitvavoPublic
from v38_discovery import human_discovery_decision, movement_features, proposed_paper_size


AMSTERDAM = ZoneInfo('Europe/Amsterdam')
MINUTE_MS = 60_000
DEFAULT_DB = Path('/var/data/cryptobot_autonomous_v38.db')
DEFAULT_REPORT = Path('/var/data/cryptobot_autonomous_v38.json')


@dataclass(frozen=True)
class MinuteCandle:
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def close_ms(self) -> int:
        return self.timestamp_ms + MINUTE_MS


def _local_ms(value: str) -> int:
    parsed = datetime.strptime(value, '%Y-%m-%d %H:%M').replace(tzinfo=AMSTERDAM)
    return int(parsed.timestamp() * 1000)


def _fmt_local(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, AMSTERDAM).strftime('%Y-%m-%d %H:%M:%S %Z')


def _finite_positive(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _parse_candles(payload: object) -> list[MinuteCandle]:
    parsed: dict[int, MinuteCandle] = {}
    if not isinstance(payload, list):
        return []
    for row in payload:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        values = [_finite_positive(value) for value in row[1:6]]
        try:
            timestamp_ms = int(row[0])
        except (TypeError, ValueError, OverflowError):
            continue
        if all(value is not None for value in values):
            parsed[timestamp_ms] = MinuteCandle(timestamp_ms, *values)  # type: ignore[arg-type]
    return [parsed[key] for key in sorted(parsed)]


def fetch_minute_candles(
    api: BitvavoPublic, market: str, start_ms: int, end_ms: int
) -> list[MinuteCandle]:
    candles: dict[int, MinuteCandle] = {}
    cursor = start_ms
    chunk_ms = 1_400 * MINUTE_MS
    while cursor < end_ms:
        chunk_end = min(end_ms, cursor + chunk_ms)
        payload = api._get(
            f'/{market}/candles',
            {'interval': '1m', 'limit': 1440, 'start': cursor, 'end': chunk_end},
        )
        for candle in _parse_candles(payload):
            if start_ms <= candle.timestamp_ms <= end_ms:
                candles[candle.timestamp_ms] = candle
        cursor = chunk_end + 1
    return [candles[key] for key in sorted(candles)]


def runtime_audit(
    api: BitvavoPublic,
    db_path: Path,
    report_path: Path,
    now_ms: int | None = None,
) -> dict[str, Any]:
    current = int(time.time() * 1000) if now_ms is None else int(now_ms)
    active = set(api.trading_markets('EUR'))
    valid_tickers = {str(row['market']) for row in api.quote_market_tickers('EUR')}
    if not db_path.exists():
        raise RuntimeError(f'v3.8-database ontbreekt: {db_path}')
    with sqlite3.connect(db_path) as conn:
        first_ms, last_ms, scans = conn.execute(
            'SELECT MIN(captured_ms),MAX(captured_ms),COUNT(DISTINCT captured_ms) FROM v38_prices'
        ).fetchone()
        if first_ms is None or last_ms is None:
            raise RuntimeError('v3.8-database bevat nog geen prijsscans')
        tracked = {
            str(row[0]) for row in conn.execute(
                'SELECT market FROM v38_prices WHERE captured_ms=? ORDER BY market', (last_ms,)
            )
        }
        vet_rows = int(conn.execute(
            "SELECT COUNT(*) FROM v38_prices WHERE market='VET-EUR'"
        ).fetchone()[0])
    report = json.loads(report_path.read_text(encoding='utf-8')) if report_path.exists() else {}
    span_minutes = (int(last_ms) - int(first_ms)) / MINUTE_MS
    return {
        'first_scan_ms': int(first_ms),
        'last_scan_ms': int(last_ms),
        'scan_count': int(scans),
        'span_minutes': span_minutes,
        'last_scan_age_seconds': (current - int(last_ms)) / 1000,
        'active_count': len(active),
        'valid_ticker_count': len(valid_tickers),
        'tracked_count': len(tracked),
        'missing_active': sorted(active - tracked),
        'missing_valid_tickers': sorted(valid_tickers - tracked),
        'extra_tracked': sorted(tracked - active),
        'active_without_valid_ticker': sorted(active - valid_tickers),
        'vet_active': 'VET-EUR' in active,
        'vet_tracked': 'VET-EUR' in tracked,
        'vet_rows': vet_rows,
        'mode': report.get('mode'),
        'paper_execution': report.get('paper_execution'),
        'live_orders': report.get('live_orders'),
        'execution_enabled': report.get('execution_enabled'),
        'passed_20_minutes': span_minutes >= 20.0,
        'complete': active == tracked,
    }


def replay_vet(
    candles: Iterable[MinuteCandle], entry_ms: int, exit_ms: int,
    roundtrip_cost_pct: float = 0.78,
) -> dict[str, Any]:
    rows = sorted(candles, key=lambda item: item.timestamp_ms)
    evaluation_start_ms = entry_ms - 60 * MINUTE_MS
    decisions: list[dict[str, Any]] = []
    for candle in rows:
        now_ms = candle.close_ms
        if not (evaluation_start_ms <= now_ms <= exit_ms + MINUTE_MS):
            continue
        prices = [
            (item.close_ms, item.close) for item in rows
            if now_ms - 65 * MINUTE_MS <= item.close_ms <= now_ms
        ]
        quote_volume_24h = sum(
            item.close * item.volume for item in rows
            if now_ms - 24 * 60 * MINUTE_MS < item.close_ms <= now_ms
        )
        features = movement_features(prices, now_ms)
        decision = human_discovery_decision(
            'VET-EUR', features, volume_quote=quote_volume_24h
        )
        decision['evaluated_ms'] = now_ms
        decision['proposed_paper_eur'] = proposed_paper_size(decision)
        decisions.append(decision)
    signals = [item for item in decisions if item['action'] == 'DOOR_NAAR_MENSELIJKE_JURY']
    before_entry = [item for item in signals if item['evaluated_ms'] <= entry_ms]
    during_trade = [item for item in signals if entry_ms < item['evaluated_ms'] <= exit_ms]
    entry_candle = next((item for item in rows if item.timestamp_ms <= entry_ms < item.close_ms), None)
    exit_candle = next((item for item in rows if item.timestamp_ms <= exit_ms < item.close_ms), None)
    gross_pct = None
    net_pct = None
    net_eur_2500 = None
    max_up_pct = None
    max_down_pct = None
    if entry_candle and exit_candle:
        entry_price, exit_price = entry_candle.open, exit_candle.close
        gross_pct = (exit_price / entry_price - 1.0) * 100.0
        net_pct = gross_pct - roundtrip_cost_pct
        net_eur_2500 = 2_500.0 * net_pct / 100.0
        trade_rows = [item for item in rows if entry_candle.timestamp_ms <= item.timestamp_ms <= exit_candle.timestamp_ms]
        if trade_rows:
            max_up_pct = (max(item.high for item in trade_rows) / entry_price - 1.0) * 100.0
            max_down_pct = (min(item.low for item in trade_rows) / entry_price - 1.0) * 100.0
    return {
        'candles': len(rows),
        'entry_ms': entry_ms,
        'exit_ms': exit_ms,
        'decisions': len(decisions),
        'jury_signals': len(signals),
        'first_signal': signals[0] if signals else None,
        'first_signal_before_entry': before_entry[0] if before_entry else None,
        'first_signal_during_trade': during_trade[0] if during_trade else None,
        'entry_candle_open': entry_candle.open if entry_candle else None,
        'exit_candle_close': exit_candle.close if exit_candle else None,
        'gross_pct': gross_pct,
        'roundtrip_cost_pct': roundtrip_cost_pct,
        'net_pct_estimate': net_pct,
        'net_eur_2500_estimate': net_eur_2500,
        'max_up_pct': max_up_pct,
        'max_down_pct': max_down_pct,
    }


def _print_runtime(result: dict[str, Any]) -> None:
    print('=== v3.8 RUNTIME-AUDIT | READ ONLY ===')
    print(f"eerste scan            : {_fmt_local(result['first_scan_ms'])}")
    print(f"laatste scan           : {_fmt_local(result['last_scan_ms'])}")
    print(f"historie               : {result['span_minutes']:.1f} min | {result['scan_count']} scans")
    print(f"laatste scan oud       : {result['last_scan_age_seconds']:.0f} sec")
    print(f"actief / geldige ticker: {result['active_count']} / {result['valid_ticker_count']}")
    print(f"laatste scan gevolgd   : {result['tracked_count']}")
    print(f"VET-EUR                : actief={result['vet_active']} gevolgd={result['vet_tracked']} rijen={result['vet_rows']}")
    print(f"minimaal 20 minuten    : {'JA' if result['passed_20_minutes'] else 'NEE'}")
    print(f"alle actieve gevolgd   : {'JA' if result['complete'] else 'NEE'}")
    print(f"modus                   : {result['mode']} | PAPER={result['paper_execution']} | live={result['live_orders']}")
    for label, key in (
        ('ontbrekend actief', 'missing_active'),
        ('ontbrekend geldig', 'missing_valid_tickers'),
        ('actief zonder ticker', 'active_without_valid_ticker'),
        ('niet meer actief', 'extra_tracked'),
    ):
        values = result[key]
        print(f"{label:<23}: {', '.join(values) if values else '-'}")


def _print_replay(result: dict[str, Any]) -> None:
    print('=== VET-EUR HANDMATIGE TRADE REPLAY | 1M PUBLIC DATA ===')
    print(f"periode                : {_fmt_local(result['entry_ms'])} -> {_fmt_local(result['exit_ms'])}")
    print(f"candles / beslissingen : {result['candles']} / {result['decisions']}")
    print(f"jury-signalen          : {result['jury_signals']}")
    for label, key in (
        ('eerste signaal', 'first_signal'),
        ('vóór/om instap', 'first_signal_before_entry'),
        ('tijdens trade', 'first_signal_during_trade'),
    ):
        item = result[key]
        if item:
            features = item['features']
            print(
                f"{label:<23}: {_fmt_local(item['evaluated_ms'])} | score {item['discovery_score']:.1f}"
                f" | 5m {features.get('momentum_5m_pct', 0):+.2f}%"
                f" | 15m {features.get('momentum_15m_pct', 0):+.2f}%"
                f" | voorstel EUR {item['proposed_paper_eur']:.0f}"
            )
        else:
            print(f'{label:<23}: geen')
    if result['gross_pct'] is not None:
        print(f"candle instap/uitstap  : {result['entry_candle_open']:.8f} / {result['exit_candle_close']:.8f}")
        print(f"bruto candle-resultaat : {result['gross_pct']:+.3f}%")
        print(f"netto schatting        : {result['net_pct_estimate']:+.3f}% | EUR {result['net_eur_2500_estimate']:+.2f} bij EUR 2500")
        print(f"max omhoog / omlaag    : {result['max_up_pct']:+.3f}% / {result['max_down_pct']:+.3f}%")
    print('LET OP: candle-replay; geen historische L2-spread en geen exacte handmatige uitvoeringsprijzen.')


def main() -> int:
    parser = argparse.ArgumentParser(description='Read-only v3.8 runtime-audit en VET-replay')
    parser.add_argument('--runtime', action='store_true')
    parser.add_argument('--vet-replay', action='store_true')
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    parser.add_argument('--report', type=Path, default=DEFAULT_REPORT)
    parser.add_argument('--entry', default='2026-09-08 09:54')
    parser.add_argument('--exit', dest='exit_time', default='2026-09-08 12:48')
    parser.add_argument('--roundtrip-cost-pct', type=float, default=0.78)
    args = parser.parse_args()
    if not args.runtime and not args.vet_replay:
        args.runtime = args.vet_replay = True
    api = BitvavoPublic('https://api.bitvavo.com/v2')
    if args.runtime:
        _print_runtime(runtime_audit(api, args.db, args.report))
    if args.vet_replay:
        entry_ms, exit_ms = _local_ms(args.entry), _local_ms(args.exit_time)
        history_start = entry_ms - 25 * 60 * MINUTE_MS
        candles = fetch_minute_candles(api, 'VET-EUR', history_start, exit_ms + MINUTE_MS)
        _print_replay(replay_vet(candles, entry_ms, exit_ms, args.roundtrip_cost_pct))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
