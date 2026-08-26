from __future__ import annotations

from dataclasses import dataclass

from config import Settings
from storage import Storage


@dataclass(frozen=True)
class Performance:
    trades: int
    wins: int
    losses: int
    pnl_eur: float
    profit_factor: float
    max_drawdown_pct: float
    span_days: float


def performance(db: Storage, paper_start_eur: float) -> Performance:
    rows = db.trade_rows()
    wins = sum(1 for r in rows if float(r['pnl_eur']) > 0)
    losses = sum(1 for r in rows if float(r['pnl_eur']) < 0)
    pnl = sum(float(r['pnl_eur']) for r in rows)
    gross_profit = sum(max(0.0, float(r['pnl_eur'])) for r in rows)
    gross_loss = abs(sum(min(0.0, float(r['pnl_eur'])) for r in rows))
    pf = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)

    equity = paper_start_eur
    peak = equity
    max_dd = 0.0
    for r in rows:
        equity += float(r['pnl_eur'])
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak-equity)/peak*100.0)

    span_days = 0.0
    if len(rows) >= 2:
        # Prospectieve meetduur loopt van eerste close tot laatste close;
        # de houdtijd van de eerste trade mag de observatieperiode niet verlengen.
        span_days = (int(rows[-1]['closed_at_ms']) - int(rows[0]['closed_at_ms'])) / 86_400_000.0
    return Performance(len(rows), wins, losses, pnl, pf, max_dd, span_days)


def verdict(p: Performance, s: Settings) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if p.trades < s.eval_min_trades:
        reasons.append(f'trades {p.trades}/{s.eval_min_trades}')
    if p.span_days < s.eval_min_span_days:
        reasons.append(f'dagen {p.span_days:.1f}/{s.eval_min_span_days:.1f}')
    if reasons:
        return 'WAIT', reasons
    if p.pnl_eur <= 0:
        reasons.append('netto_pnl_niet_positief')
    if p.profit_factor < s.eval_min_profit_factor:
        reasons.append(f'pf {p.profit_factor:.2f} < {s.eval_min_profit_factor:.2f}')
    if p.max_drawdown_pct > s.eval_max_drawdown_pct:
        reasons.append(f'drawdown {p.max_drawdown_pct:.2f}% > {s.eval_max_drawdown_pct:.2f}%')
    return ('PASS' if not reasons else 'FAIL'), reasons


def print_report(db: Storage, s: Settings) -> None:
    p = performance(db, s.paper_start_eur)
    result, reasons = verdict(p, s)
    print('=== CRYPTOBOT CLEAN-ROOM v1 ===')
    print(f'UNIVERSE        : {", ".join(db.universe()) or "nog niet gekozen"}')
    print(f'CLOSED TRADES   : {p.trades}')
    print(f'W/L             : {p.wins}/{p.losses}')
    print(f'NET PNL         : €{p.pnl_eur:+.2f}')
    print(f'PROFIT FACTOR   : {"INF" if p.profit_factor >= 999 else f"{p.profit_factor:.3f}"}')
    print(f'MAX DRAWDOWN    : {p.max_drawdown_pct:.2f}%')
    print(f'MEASURED DAYS   : {p.span_days:.1f}')
    print(f'VERDICT         : {result}')
    if reasons:
        print('REASONS         : ' + '; '.join(reasons))
    counts = db.decision_reason_counts()
    if counts:
        print('DECISIONS       :')
        for action, reason, n in counts[:10]:
            print(f'  {action:4s} {reason:24s} {n}')
