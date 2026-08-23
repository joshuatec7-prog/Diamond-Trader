#!/usr/bin/env python3
"""Read-only coverage diagnostic for sideways-market scanner signals.

Looks at the last N days in /var/data/diamond_market_signals.csv and reports
NEUTRAL mean-reversion/range-breakout coverage, eligibility and rejection
reasons. No network, orders, private API, config or LIVE changes.
"""

from __future__ import annotations

import argparse
import csv
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SIGNALS = Path('/var/data/diamond_market_signals.csv')


def f(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def b(value: Any) -> bool:
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'ja', 'on'}


def dt(value: Any) -> Optional[datetime]:
    try:
        x = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if x.tzinfo is None:
            x = x.replace(tzinfo=timezone.utc)
        return x.astimezone(timezone.utc)
    except Exception:
        return None


def rejection_parts(row: Dict[str, str]) -> List[str]:
    raw = str(row.get('shadow_rejection_reasons') or '').strip()
    if not raw:
        return []
    return [part.strip() for part in raw.split('|') if part.strip()]


def stats(values: List[float]) -> str:
    if not values:
        return 'n/a'
    values = sorted(values)
    mid = values[len(values) // 2]
    return f'min={values[0]:.3f} med={mid:.3f} max={values[-1]:.3f}'


def run(days: int) -> int:
    if not SIGNALS.is_file():
        print(f'FOUT: {SIGNALS} ontbreekt')
        return 2

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    groups = defaultdict(list)

    with SIGNALS.open('r', encoding='utf-8-sig', newline='') as handle:
        for row in csv.DictReader(handle):
            detected = dt(row.get('detected_at'))
            if detected is None or detected < cutoff:
                continue
            regime = str(row.get('market_regime') or '').upper()
            strategy = str(row.get('strategy') or '')
            side = str(row.get('side') or '').upper()
            if regime != 'NEUTRAL':
                continue
            if strategy not in {'mean_reversion', 'range_breakout'}:
                continue
            groups[(strategy, side)].append(row)

    print('=' * 96)
    print(' DIAMOND SIDEWAYS COVERAGE DIAGNOSE')
    print('=' * 96)
    print(f'Periode: laatste {days} dagen')

    order = [
        ('mean_reversion', 'LONG'),
        ('mean_reversion', 'SHORT'),
        ('range_breakout', 'LONG'),
        ('range_breakout', 'SHORT'),
    ]

    total = 0
    for key in order:
        rows = groups.get(key, [])
        total += len(rows)
        eligible = [r for r in rows if b(r.get('shadow_eligible'))]
        rejected = [r for r in rows if not b(r.get('shadow_eligible'))]
        reasons = Counter()
        for row in rejected:
            parts = rejection_parts(row)
            if not parts:
                reasons['ONBEKEND'] += 1
            else:
                for part in parts:
                    reasons[part] += 1

        scores = [f(r.get('score')) for r in rows]
        spreads = [f(r.get('spread_pct')) for r in rows]
        rrs = [f(r.get('reward_risk')) for r in rows]

        print(f'\n--- {key[0]} {key[1]} ---')
        print(f'totaal   : {len(rows)}')
        print(f'eligible : {len(eligible)}')
        print(f'afgewezen: {len(rejected)}')
        print(f'score    : {stats(scores)}')
        print(f'spread % : {stats(spreads)}')
        print(f'RR       : {stats(rrs)}')
        if reasons:
            print('top afwijzingen:')
            for reason, count in reasons.most_common(8):
                print(f'  {count:3}x {reason}')
        else:
            print('top afwijzingen: geen')

    print('\n--- SAMENVATTING ---')
    print(f'NEUTRAL mean-reversion/range signalen totaal: {total}')
    if total == 0:
        print('Conclusie: scanner genereert in deze periode geen sideways-signalen.')
    else:
        print('Conclusie: dekking aanwezig; beoordeel vooral eligibility/afwijzingen hierboven.')

    print('\nVEILIGHEID: read-only | orders/private API/config/LIVE = NEE')
    return 0


def self_test() -> int:
    assert b('True') and not b('False')
    assert dt('2026-08-23T00:00:00+00:00') is not None
    assert rejection_parts({'shadow_rejection_reasons': 'score laag | spread hoog'}) == ['score laag', 'spread hoog']
    print('DIAMOND_SIDEWAYS_COVERAGE_DIAGNOSE_SELF_TEST_OK')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    return self_test() if args.self_test else run(args.days)


if __name__ == '__main__':
    raise SystemExit(main())
