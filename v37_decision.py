from __future__ import annotations

import hashlib
import json
import math
from statistics import median
from typing import Any, Sequence


GATE_ORDER = (
    'veiligheidsgrenzen',
    'data_integriteit',
    'marktcontext',
    'setup_expert',
    'l2_bevestiging',
    'netto_voordeel',
    'portefeuillerisico',
    'besliscontroller',
)

SETUP_EXPECTED_HORIZON_MINUTES = {
    '5m_breakout': 90,
    '5m_pullback_hervatting': 180,
    '5m_volume_hervatting': 120,
}


def _finite(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _gate(status: str, reasons: Sequence[str], evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    if status not in {'PASS', 'WAIT', 'BLOCK'}:
        raise ValueError(f'ongeldige poortstatus: {status}')
    return {
        'status': status,
        'hard_gate': True,
        'reasons': list(dict.fromkeys(str(reason) for reason in reasons if reason)),
        'evidence': evidence or {},
    }


def resolve_regime(
    *,
    previous_regime: str,
    raw_regime: str,
    pending_regime: str = '',
    pending_count: int = 0,
    confirmations: int = 2,
) -> dict[str, Any]:
    """Voorkom dat één 5m-meting het marktregime direct laat omslaan."""
    raw = str(raw_regime).upper()
    previous = str(previous_regime).upper()
    pending = str(pending_regime).upper()
    if raw not in {'BULL', 'SIDEWAYS', 'BEAR', 'DATA_UNCERTAIN'}:
        raw = 'DATA_UNCERTAIN'
    if previous not in {'BULL', 'SIDEWAYS', 'BEAR'}:
        return {
            'regime': raw,
            'stable_regime': raw,
            'pending_regime': '',
            'pending_count': 0,
            'transition': False,
        }
    if raw == previous:
        return {
            'regime': previous,
            'stable_regime': previous,
            'pending_regime': '',
            'pending_count': 0,
            'transition': False,
        }
    count = pending_count + 1 if pending == raw else 1
    if count >= max(1, int(confirmations)):
        return {
            'regime': raw,
            'stable_regime': raw,
            'pending_regime': '',
            'pending_count': 0,
            'transition': False,
        }
    return {
        'regime': 'TRANSITION',
        'stable_regime': previous,
        'pending_regime': raw,
        'pending_count': count,
        'transition': True,
    }


def market_cluster(market: str) -> str:
    base = str(market).upper().split('-', 1)[0]
    clusters = {
        'BTC': 'bitcoin',
        'ETH': 'ethereum_ecosysteem',
        'ARB': 'ethereum_ecosysteem',
        'OP': 'ethereum_ecosysteem',
        'UNI': 'defi',
        'AAVE': 'defi',
        'LINK': 'defi',
        'SOL': 'solana_ecosysteem',
        'RAY': 'solana_ecosysteem',
        'JUP': 'solana_ecosysteem',
        'SUI': 'laag1',
        'ADA': 'laag1',
        'NEAR': 'laag1',
        'AVAX': 'laag1',
        'XRP': 'betalingsnetwerken',
        'XLM': 'betalingsnetwerken',
        'DOGE': 'meme',
        'SHIB': 'meme',
        'PEPE': 'meme',
        'FARTCOIN': 'meme',
        'FET': 'ai',
        'TAO': 'ai',
        'RENDER': 'ai',
    }
    return clusters.get(base, f'overig:{base}')


def _safety_gate(invariants: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    mode = str(invariants.get('mode', '')).upper()
    start = _finite(invariants.get('paper_start_eur'))
    position = _finite(invariants.get('position_eur'))
    reserve = _finite(invariants.get('reserve_eur'))
    maximum = int(_finite(invariants.get('max_open_positions')))
    if mode != 'OBSERVE_ONLY':
        reasons.append('v37_niet_observe_only')
    if start != 3000.0:
        reasons.append('paper_startvermogen_niet_3000')
    if position != 500.0:
        reasons.append('positieomvang_niet_500')
    if reserve < 200.0:
        reasons.append('reserve_lager_dan_200')
    if maximum < 1 or maximum > 5:
        reasons.append('maximum_open_posities_buiten_1_tot_5')
    if position * maximum > start - reserve:
        reasons.append('maximale_inzet_schendt_reserve')
    if not bool(invariants.get('existing_assets_excluded')):
        reasons.append('bestaande_munten_niet_uitgesloten')
    return _gate('BLOCK' if reasons else 'PASS', reasons, {
        'mode': mode,
        'paper_start_eur': start,
        'position_eur': position,
        'reserve_eur': reserve,
        'max_open_positions': maximum,
        'existing_assets_excluded': bool(invariants.get('existing_assets_excluded')),
    })


def _data_gate(context: dict[str, Any]) -> dict[str, Any]:
    qualities = context.get('quality', {})
    reasons: list[str] = []
    warnings: list[str] = []
    statuses: dict[str, str] = {}
    for name in ('five', 'fifteen', 'hour', 'bitcoin_five', 'bitcoin_fifteen', 'bitcoin_hour'):
        item = qualities.get(name, {}) if isinstance(qualities, dict) else {}
        status = str(item.get('status', 'BLOCK')).upper()
        statuses[name] = status
        if not bool(item.get('valid')):
            reasons.append(f"{name}_{item.get('reason', 'data_ongeldig')}")
        elif status == 'WARN':
            warnings.append(f"{name}_{item.get('reason', 'waarschuwing')}")
    return _gate('BLOCK' if reasons else 'PASS', reasons, {
        'timeframes': statuses,
        'warnings': warnings,
        'feed_integrity_separate_from_activity': True,
    })


def _market_gate(context: dict[str, Any]) -> dict[str, Any]:
    regime = str(context.get('regime', 'DATA_UNCERTAIN')).upper()
    fifteen = context.get('fifteen', {})
    hour = context.get('hour', {})
    bitcoin = context.get('bitcoin', {})
    btc_five = bitcoin.get('five', {}) if isinstance(bitcoin, dict) else {}
    btc_fifteen = bitcoin.get('fifteen', {}) if isinstance(bitcoin, dict) else {}
    btc_hour = bitcoin.get('hour', {}) if isinstance(bitcoin, dict) else {}
    reasons: list[str] = []
    if regime not in {'BULL', 'SIDEWAYS'}:
        reasons.append(f'marktregime_{regime.lower()}_blokkeert_long')
    if not bool(fifteen.get('trend_up')) or not bool(hour.get('trend_up')):
        reasons.append('15m_en_1h_trend_niet_beide_omhoog')
    btc_five_momentum = _finite(btc_five.get('momentum_3_pct'))
    btc_shock = (
        btc_five_momentum <= -1.50
        or _finite(btc_five.get('last_bar_pct')) <= -1.00
        or _finite(btc_fifteen.get('last_bar_pct')) <= -1.50
    )
    if btc_shock:
        reasons.append('bitcoin_marktschok')
    if bool(btc_fifteen.get('trend_down')) and bool(btc_hour.get('trend_down')):
        reasons.append('bitcoin_15m_en_1h_beide_omlaag')
    return _gate('BLOCK' if reasons else 'PASS', reasons, {
        'regime': regime,
        'bull_breadth_pct': _finite(context.get('bull_breadth_pct')),
        'bear_breadth_pct': _finite(context.get('bear_breadth_pct')),
        'btc_5m_momentum_pct': round(btc_five_momentum, 4),
        'btc_15m_trend_up': bool(btc_fifteen.get('trend_up')),
        'btc_1h_trend_up': bool(btc_hour.get('trend_up')),
    })


def _setup_gate(context: dict[str, Any]) -> tuple[dict[str, Any], str, float]:
    five = context.get('five', {})
    trigger = str(five.get('trigger', 'geen_5m_trigger'))
    momentum = _finite(five.get('momentum_3_pct'))
    volume = _finite(five.get('volume_ratio'))
    extension = _finite(five.get('extension_atr'), 999.0)
    rsi = _finite(five.get('rsi'), -1.0)
    regime = str(context.get('regime', 'DATA_UNCERTAIN')).upper()
    reasons: list[str] = []
    if not bool(five.get('trend_up')):
        reasons.append('5m_trend_niet_omhoog')
    if momentum <= 0.0:
        reasons.append('5m_momentum_niet_positief')

    if trigger == '5m_breakout':
        if regime != 'BULL':
            reasons.append('breakout_vereist_bull_regime')
        if not 0.15 <= momentum <= 3.00:
            reasons.append('breakout_momentum_buiten_band')
        if volume < 1.20:
            reasons.append('breakout_volume_onvoldoende')
        if extension > 2.00:
            reasons.append('breakout_te_ver_uitgerekt')
        if not 52.0 <= rsi <= 78.0:
            reasons.append('breakout_rsi_buiten_band')
        priority = 300.0
    elif trigger == '5m_pullback_hervatting':
        if not 0.0 < momentum <= 2.50:
            reasons.append('pullback_momentum_buiten_band')
        if volume < 0.80:
            reasons.append('pullback_volume_onvoldoende')
        if extension > 1.50:
            reasons.append('pullback_te_ver_uitgerekt')
        if not 48.0 <= rsi <= 75.0:
            reasons.append('pullback_rsi_buiten_band')
        priority = 250.0
    elif trigger == '5m_volume_hervatting':
        if not 0.0 < momentum <= 3.00:
            reasons.append('volumehervatting_momentum_buiten_band')
        if volume < 1.35:
            reasons.append('volumehervatting_volume_onvoldoende')
        if extension > 2.00:
            reasons.append('volumehervatting_te_ver_uitgerekt')
        if not 50.0 <= rsi <= 78.0:
            reasons.append('volumehervatting_rsi_buiten_band')
        priority = 200.0
    else:
        reasons.append('herkenbare_5m_setup_ontbreekt')
        priority = 0.0

    priority += 20.0 if regime == 'BULL' else 0.0
    priority += min(20.0, max(0.0, volume) * 5.0)
    priority += min(10.0, max(0.0, momentum) * 3.0)
    priority -= min(10.0, max(0.0, extension) * 2.0)
    evidence = {
        'setup': trigger,
        'momentum_3_pct': round(momentum, 4),
        'volume_ratio': round(volume, 4),
        'extension_atr': round(extension, 4),
        'rsi': round(rsi, 2),
        'selectieprioriteit_niet_beslissend': round(priority, 3),
    }
    return _gate('BLOCK' if reasons else 'PASS', reasons, evidence), trigger, priority


def _l2_gate(
    snapshots: Sequence[dict[str, Any]],
    *,
    now_ms: int,
    minimum_samples: int,
    minimum_span_ms: int,
    maximum_age_ms: int,
    max_spread_pct: float,
) -> tuple[dict[str, Any], dict[str, float]]:
    valid: list[dict[str, float]] = []
    for item in snapshots:
        captured = int(_finite(item.get('captured_at_ms')))
        spread = _finite(item.get('execution_spread_pct'), 999.0)
        imbalance = _finite(item.get('near_book_imbalance'), -999.0)
        buy = _finite(item.get('buy_vwap'))
        sell = _finite(item.get('sell_vwap'))
        age = now_ms - captured
        if (
            captured > 0
            and -5_000 <= age <= maximum_age_ms
            and buy > 0.0
            and sell > 0.0
            and spread >= 0.0
        ):
            valid.append({
                'captured_at_ms': float(captured),
                'execution_spread_pct': spread,
                'near_book_imbalance': imbalance,
                'buy_vwap': buy,
                'sell_vwap': sell,
            })
    valid.sort(key=lambda item: item['captured_at_ms'])
    count = len(valid)
    span_ms = int(valid[-1]['captured_at_ms'] - valid[0]['captured_at_ms']) if count else 0
    summary: dict[str, float] = {
        'sample_count': float(count),
        'sample_span_seconds': round(span_ms / 1000.0, 1),
    }
    if count < minimum_samples or span_ms < minimum_span_ms:
        return _gate('WAIT', ['meer_l2_metingen_nodig'], {
            **summary,
            'minimum_samples': minimum_samples,
            'minimum_span_seconds': round(minimum_span_ms / 1000.0, 1),
        }), summary

    spreads = [item['execution_spread_pct'] for item in valid]
    imbalances = [item['near_book_imbalance'] for item in valid]
    buys = [item['buy_vwap'] for item in valid]
    median_spread = median(spreads)
    worst_spread = max(spreads)
    median_imbalance = median(imbalances)
    worst_imbalance = min(imbalances)
    price_drift = (max(buys) / min(buys) - 1.0) * 100.0
    latest = valid[-1]
    summary.update({
        'median_spread_pct': round(median_spread, 6),
        'worst_spread_pct': round(worst_spread, 6),
        'median_imbalance': round(median_imbalance, 6),
        'worst_imbalance': round(worst_imbalance, 6),
        'buy_vwap': round(latest['buy_vwap'], 10),
        'sell_vwap': round(latest['sell_vwap'], 10),
        'buy_vwap_drift_pct': round(price_drift, 6),
    })
    reasons: list[str] = []
    if median_spread > max_spread_pct:
        reasons.append('mediane_l2_spread_te_hoog')
    if worst_spread > max_spread_pct:
        reasons.append('l2_spread_niet_stabiel')
    if median_imbalance < -0.10:
        reasons.append('mediane_orderboekdruk_negatief')
    if worst_imbalance < -0.35:
        reasons.append('orderboek_toont_verkooppiek')
    if price_drift > 0.40:
        reasons.append('uitvoerprijs_te_instabiel')
    return _gate('BLOCK' if reasons else 'PASS', reasons, summary), summary


def _edge_gate(
    *,
    setup: str,
    five: dict[str, Any],
    l2_gate: dict[str, Any],
    l2: dict[str, float],
    taker_fee_pct: float,
    slippage_pct: float,
    minimum_rr: float,
) -> tuple[dict[str, Any], dict[str, float]]:
    if l2_gate['status'] == 'WAIT':
        return _gate('WAIT', ['wacht_op_l2_bevestiging']), {}
    if l2_gate['status'] == 'BLOCK':
        return _gate('BLOCK', ['l2_blokkeert_kostenberekening']), {}
    atr_pct = _finite(five.get('atr_pct'))
    reward_multiplier = {
        '5m_breakout': 3.00,
        '5m_pullback_hervatting': 2.50,
        '5m_volume_hervatting': 2.75,
    }.get(setup, 0.0)
    stop_multiplier = {
        '5m_breakout': 1.35,
        '5m_pullback_hervatting': 1.15,
        '5m_volume_hervatting': 1.25,
    }.get(setup, 0.0)
    gross_reward = min(6.0, max(1.50, reward_multiplier * atr_pct))
    gross_stop = min(3.0, max(1.00, stop_multiplier * atr_pct))
    roundtrip_cost = 2.0 * taker_fee_pct + 2.0 * slippage_pct + _finite(
        l2.get('median_spread_pct')
    )
    net_reward = gross_reward - roundtrip_cost
    net_risk = gross_stop + roundtrip_cost
    net_rr = net_reward / net_risk if net_reward > 0.0 and net_risk > 0.0 else 0.0
    cost_multiple = gross_reward / roundtrip_cost if roundtrip_cost > 0.0 else 0.0
    buy_vwap = _finite(l2.get('buy_vwap'))
    evidence = {
        'atr_pct': round(atr_pct, 6),
        'gross_reward_pct': round(gross_reward, 6),
        'gross_stop_pct': round(gross_stop, 6),
        'roundtrip_cost_pct': round(roundtrip_cost, 6),
        'net_reward_pct': round(net_reward, 6),
        'net_risk_pct': round(net_risk, 6),
        'net_reward_risk': round(net_rr, 6),
        'cost_multiple': round(cost_multiple, 6),
        'buy_vwap': round(buy_vwap, 10),
        'technical_stop_hint': round(buy_vwap * (1.0 - gross_stop / 100.0), 10),
        'technical_reward_hint': round(buy_vwap * (1.0 + gross_reward / 100.0), 10),
    }
    reasons: list[str] = []
    if net_rr < minimum_rr:
        reasons.append('netto_risico_opbrengst_te_laag')
    if cost_multiple < 3.0:
        reasons.append('beweging_te_klein_ten_opzichte_van_kosten')
    return _gate('BLOCK' if reasons else 'PASS', reasons, evidence), evidence


def _portfolio_gate(
    *,
    market: str,
    portfolio_state: dict[str, Any],
    edge: dict[str, float],
    position_eur: float,
    reserve_eur: float,
    max_open_positions: int,
    max_open_risk_eur: float,
    max_cluster_positions: int,
    max_entries_per_cycle: int,
    daily_loss_limit_eur: float,
) -> dict[str, Any]:
    net_risk_pct = _finite(edge.get('net_risk_pct'))
    planned_risk_eur = position_eur * net_risk_pct / 100.0
    open_count = int(_finite(portfolio_state.get('open_positions')))
    cash = _finite(portfolio_state.get('paper_cash_eur'), 3000.0)
    open_risk = _finite(portfolio_state.get('open_planned_risk_eur'))
    entries_cycle = int(_finite(portfolio_state.get('entries_this_cycle')))
    daily_pnl = _finite(portfolio_state.get('daily_realized_pnl_eur'))
    recent_stops = int(_finite(portfolio_state.get('recent_stops_two_hours')))
    cluster = market_cluster(market)
    counts = portfolio_state.get('cluster_counts', {})
    cluster_count = int(_finite(counts.get(cluster))) if isinstance(counts, dict) else 0
    reasons: list[str] = []
    if open_count >= max_open_positions:
        reasons.append('maximum_open_posities_bereikt')
    if cash - position_eur < reserve_eur:
        reasons.append('vaste_cashreserve_zou_worden_geschonden')
    if open_risk + planned_risk_eur > max_open_risk_eur:
        reasons.append('maximaal_gepland_open_risico_overschreden')
    if cluster_count >= max_cluster_positions:
        reasons.append('correlatiecluster_reeds_vol')
    if entries_cycle >= max_entries_per_cycle:
        reasons.append('maximum_nieuwe_posities_in_cyclus_bereikt')
    if daily_pnl <= -abs(daily_loss_limit_eur):
        reasons.append('dagverlieslimiet_bereikt')
    if recent_stops >= 2:
        reasons.append('globale_pauze_na_twee_stops')
    return _gate('BLOCK' if reasons else 'PASS', reasons, {
        'cluster': cluster,
        'cluster_open': cluster_count,
        'open_positions': open_count,
        'cash_eur': round(cash, 2),
        'reserve_eur': round(reserve_eur, 2),
        'planned_risk_eur': round(planned_risk_eur, 4),
        'open_risk_after_entry_eur': round(open_risk + planned_risk_eur, 4),
        'max_open_risk_eur': round(max_open_risk_eur, 2),
        'entries_this_cycle': entries_cycle,
        'daily_realized_pnl_eur': round(daily_pnl, 2),
        'recent_stops_two_hours': recent_stops,
    })


def evaluate_human_gates(
    *,
    context: dict[str, Any],
    snapshots: Sequence[dict[str, Any]],
    invariants: dict[str, Any],
    portfolio_state: dict[str, Any] | None = None,
    now_ms: int,
    minimum_l2_samples: int = 3,
    minimum_l2_span_ms: int = 45_000,
    maximum_l2_age_ms: int = 90_000,
    max_spread_pct: float = 0.25,
    taker_fee_pct: float = 0.25,
    slippage_pct: float = 0.08,
    minimum_rr: float = 1.20,
    max_open_risk_eur: float = 45.0,
    max_cluster_positions: int = 2,
    max_entries_per_cycle: int = 2,
    daily_loss_limit_eur: float = 45.0,
) -> dict[str, Any]:
    """Beoordeel een longkans via opeenvolgende poorten, zonder orderuitvoering."""
    market = str(context.get('market', ''))
    gates: dict[str, dict[str, Any]] = {}
    gates['veiligheidsgrenzen'] = _safety_gate(invariants)
    gates['data_integriteit'] = _data_gate(context)
    gates['marktcontext'] = _market_gate(context)
    setup_gate, setup, priority = _setup_gate(context)
    gates['setup_expert'] = setup_gate
    l2_gate, l2_summary = _l2_gate(
        snapshots,
        now_ms=now_ms,
        minimum_samples=minimum_l2_samples,
        minimum_span_ms=minimum_l2_span_ms,
        maximum_age_ms=maximum_l2_age_ms,
        max_spread_pct=max_spread_pct,
    )
    gates['l2_bevestiging'] = l2_gate
    edge_gate, edge = _edge_gate(
        setup=setup,
        five=context.get('five', {}),
        l2_gate=l2_gate,
        l2=l2_summary,
        taker_fee_pct=taker_fee_pct,
        slippage_pct=slippage_pct,
        minimum_rr=minimum_rr,
    )
    gates['netto_voordeel'] = edge_gate
    portfolio = portfolio_state or {
        'paper_cash_eur': _finite(invariants.get('paper_start_eur'), 3000.0),
        'open_positions': 0,
        'open_planned_risk_eur': 0.0,
        'cluster_counts': {},
        'entries_this_cycle': 0,
        'daily_realized_pnl_eur': 0.0,
        'recent_stops_two_hours': 0,
    }
    gates['portefeuillerisico'] = _portfolio_gate(
        market=market,
        portfolio_state=portfolio,
        edge=edge,
        position_eur=_finite(invariants.get('position_eur'), 500.0),
        reserve_eur=_finite(invariants.get('reserve_eur'), 200.0),
        max_open_positions=int(_finite(invariants.get('max_open_positions'), 5)),
        max_open_risk_eur=max_open_risk_eur,
        max_cluster_positions=max_cluster_positions,
        max_entries_per_cycle=max_entries_per_cycle,
        daily_loss_limit_eur=daily_loss_limit_eur,
    )

    pre_controller = [gates[name]['status'] for name in GATE_ORDER[:-1]]
    if 'BLOCK' in pre_controller:
        controller = _gate('BLOCK', ['een_of_meer_harde_poorten_blokkeren'])
        action = 'AFWIJZEN'
    elif 'WAIT' in pre_controller:
        controller = _gate('WAIT', ['bewijs_nog_niet_compleet'])
        action = 'L2 VERZAMELEN'
    else:
        controller = _gate('PASS', ['alle_harde_poorten_geslaagd'])
        action = 'SCHADUW-KANS'
    gates['besliscontroller'] = controller

    blockers = [
        reason
        for name in GATE_ORDER
        for reason in gates[name]['reasons']
        if gates[name]['status'] == 'BLOCK'
    ]
    warnings = list(gates['data_integriteit']['evidence'].get('warnings', []))
    context_fingerprint = {
        'market': market,
        'cycle_ms': int(_finite(context.get('cycle_ms'))),
        'regime': str(context.get('regime', '')),
        'setup': setup,
        'five': context.get('five', {}),
        'fifteen': context.get('fifteen', {}),
        'hour': context.get('hour', {}),
        'bitcoin': context.get('bitcoin', {}),
    }
    context_hash = hashlib.sha256(
        json.dumps(context_fingerprint, sort_keys=True, separators=(',', ':'), default=str).encode()
    ).hexdigest()[:20]
    first_four_pass = all(gates[name]['status'] == 'PASS' for name in GATE_ORDER[:4])
    would_enter = controller['status'] == 'PASS'
    return {
        'market': market,
        'action': action,
        'would_enter': would_enter,
        'eligible': False,
        'execution_enabled': False,
        'active_candidate': first_four_pass and l2_gate['status'] in {'WAIT', 'PASS'},
        'setup': setup,
        'selection_priority': round(priority, 3),
        'gates': gates,
        'blockers': list(dict.fromkeys(blockers)),
        'warnings': warnings,
        'data_quality_status': (
            'BLOCK' if gates['data_integriteit']['status'] == 'BLOCK'
            else 'WARN' if warnings else 'OK'
        ),
        'l2_summary': l2_summary,
        'edge': edge,
        'thesis': {
            'setup': setup,
            'context_hash': context_hash,
            'expected_horizon_minutes': SETUP_EXPECTED_HORIZON_MINUTES.get(setup, 0),
            'technical_stop_hint': edge.get('technical_stop_hint'),
            'technical_reward_hint': edge.get('technical_reward_hint'),
            'invalidation': 'setup_of_marktcontext_niet_meer_geldig',
        },
    }
