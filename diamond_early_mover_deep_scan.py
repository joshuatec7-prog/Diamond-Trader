#!/usr/bin/env python3
import argparse, csv, json, os, tempfile, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import ccxt
import market_scanner as ms
from diamond_selective_rules import (
    selective_accepts,
    selective_candidate_key,
)

SOURCE = Path("/var/data/diamond_marketwide_1m_early_movers.json")
OUTPUT = Path("/var/data/diamond_early_mover_deep_scan.json")
BRIDGE_SIGNALS = Path(
    "/var/data/diamond_early_mover_selective_signals.csv"
)
BRIDGE_STATE = Path(
    "/var/data/diamond_early_mover_selective_bridge_state.json"
)
HOT_WATCH_QUEUE = Path(
    "/var/data/diamond_early_mover_hot_watch.json"
)
HOT_WATCH_TTL_SECONDS = 15 * 60
LIQUIDITY_PROMOTIONS = Path(
    "/var/data/diamond_early_mover_liquidity_promotions.json"
)
MAX_LIQUIDITY_PROMOTIONS = 8

SIGNAL_WATCH_STATE = Path(
    "/var/data/diamond_early_mover_signal_watch.json"
)
SIGNAL_WATCH_TTL_SECONDS = 75 * 60
MAX_SIGNAL_WATCH_DUE = 8

BRIDGE_HEADER = list(ms.CSV_HEADER)
if "selection_reason" not in BRIDGE_HEADER:
    BRIDGE_HEADER.append("selection_reason")

NL = ZoneInfo("Europe/Amsterdam")
MAX_MARKETS = 10
MAX_SOURCE_AGE = 180

def atomic(path, data):
    fd,tmp=tempfile.mkstemp(dir=str(path.parent), prefix="."+path.name)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as f:
            json.dump(data,f,indent=2,ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def bridge_row(signal):
    eco = signal.get("economics") or {}
    expected = eco.get("expected_eur") or {}

    return {
        "detected_at": signal.get("detected_at"),
        "candle_timestamp": signal.get("candle_timestamp"),
        "symbol": signal.get("symbol"),
        "strategy": signal.get("strategy"),
        "side": signal.get("side"),
        "market_regime": signal.get("market_regime"),
        "regime_strength": signal.get("regime_strength"),
        "score": signal.get("score"),
        "entry_price": signal.get("entry_price"),
        "take_profit": signal.get("take_profit"),
        "stop_loss": signal.get("stop_loss"),
        "rsi": signal.get("rsi"),
        "atr_pct": signal.get("atr_pct"),
        "volume_ratio": signal.get("volume_ratio"),
        "spread_pct": signal.get("spread_pct"),
        "quote_volume": signal.get("quote_volume"),
        "change_pct_24h": signal.get("change_pct_24h"),
        "expected_net_pct": eco.get("expected_net_pct"),
        "risk_net_pct": eco.get("risk_net_pct"),
        "reward_risk": eco.get("reward_risk"),
        "expected_profit_eur": eco.get("expected_profit_eur"),
        "expected_loss_eur": eco.get("expected_loss_eur"),
        "expected_eur_120": expected.get("120"),
        "expected_eur_125": expected.get("125"),
        "expected_eur_130": expected.get("130"),
        "expected_eur_135": expected.get("135"),
        "shadow_eligible": signal.get("shadow_eligible"),
        "shadow_rejection_reasons": " | ".join(
            signal.get("shadow_rejection_reasons") or []
        ),
        "reasons": " | ".join(signal.get("reasons") or []),
        "selection_reason": signal.get(
            "selection_reason",
            "EARLY_MOVER_1M",
        ),
    }


def publish_bridge_signals(signals):
    eligible = []

    for signal in signals:
        if (
            selective_accepts(signal)
            and str(signal.get("side") or "").upper() == "LONG"
            and str(signal.get("strategy") or "") == "trend_breakout"
        ):
            eligible.append(signal)

    try:
        state = json.loads(BRIDGE_STATE.read_text())
        if not isinstance(state, dict):
            state = {}
    except Exception:
        state = {}

    current_keys = [
        selective_candidate_key(signal)
        for signal in eligible
        if selective_candidate_key(signal)
    ]

    # Eerste run is bewust alleen een baseline.
    # Zo kan een kandidaat van vóór deze brug niet ineens LIVE worden.
    if not state:
        state = {
            "version": 1,
            "initialized_at": datetime.now(
                timezone.utc
            ).isoformat(),
            "seen_keys": current_keys[-30000:],
            "emitted_total": 0,
            "last_mode": "BASELINE",
        }
        atomic(BRIDGE_STATE, state)
        return {
            "eligible": len(eligible),
            "emitted": 0,
            "mode": "BASELINE",
        }

    seen_order = list(dict.fromkeys(
        str(x)
        for x in state.get("seen_keys", [])
        if str(x)
    ))
    seen = set(seen_order)

    new_signals = []

    for signal in eligible:
        key = selective_candidate_key(signal)
        if not key or key in seen:
            continue
        seen.add(key)
        seen_order.append(key)
        new_signals.append(signal)

    if new_signals:
        BRIDGE_SIGNALS.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        needs_header = (
            not BRIDGE_SIGNALS.exists()
            or BRIDGE_SIGNALS.stat().st_size == 0
        )

        with BRIDGE_SIGNALS.open(
            "a",
            encoding="utf-8",
            newline="",
        ) as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=BRIDGE_HEADER,
                extrasaction="ignore",
            )
            if needs_header:
                writer.writeheader()

            for signal in new_signals:
                writer.writerow(bridge_row(signal))

    state["version"] = 1
    state["seen_keys"] = seen_order[-30000:]
    state["last_run_at"] = datetime.now(
        timezone.utc
    ).isoformat()
    state["last_mode"] = "ACTIVE"
    state["last_emitted"] = len(new_signals)
    state["emitted_total"] = (
        int(state.get("emitted_total", 0) or 0)
        + len(new_signals)
    )
    atomic(BRIDGE_STATE, state)

    return {
        "eligible": len(eligible),
        "emitted": len(new_signals),
        "mode": "ACTIVE",
    }



def parse_dt(value):
    try:
        dt=datetime.fromisoformat(
            str(value).replace("Z","+00:00")
        )
        if dt.tzinfo is None:
            dt=dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def spread_only_hot_watch_signal(signal, min_reward_risk):
    if str(signal.get("side") or "").upper() != "LONG":
        return False
    if str(signal.get("strategy") or "") != "trend_breakout":
        return False
    if bool(signal.get("shadow_eligible")):
        return False

    reasons=[
        str(x).strip().lower()
        for x in (
            signal.get("shadow_rejection_reasons") or []
        )
        if str(x).strip()
    ]
    if not reasons:
        return False

    # HOT WATCH mag uitsluitend een tijdelijke spread-blocker
    # overbruggen. Geen RR/volume/regime/score-fouten.
    if not all("spread" in reason for reason in reasons):
        return False

    eco=signal.get("economics") or {}
    try:
        rr=float(eco.get("reward_risk") or 0)
    except Exception:
        rr=0.0

    return rr + 1e-12 >= float(min_reward_risk)


def update_hot_watch(signals):
    now=datetime.now(timezone.utc)

    try:
        old=json.loads(HOT_WATCH_QUEUE.read_text())
        if not isinstance(old,dict):
            old={}
    except Exception:
        old={}

    items={}

    for item in old.get("items",[]) or []:
        if not isinstance(item,dict):
            continue
        key=str(item.get("candidate_key") or "")
        expires=parse_dt(item.get("expires_at"))
        if key and expires and expires > now:
            items[key]=item

    before=set(items)

    for signal in signals:
        key=selective_candidate_key(signal)
        symbol=str(signal.get("symbol") or "").upper()
        if not key or not symbol:
            continue

        existing=items.get(key)

        if existing:
            existing["last_seen_at"]=now.isoformat()
            existing["blocked_spread_pct"]=signal.get(
                "spread_pct"
            )
            continue

        eco=signal.get("economics") or {}

        items[key]={
            "candidate_key":key,
            "symbol":symbol,
            "detected_at":signal.get("detected_at"),
            "candle_timestamp":
                signal.get("candle_timestamp"),
            "first_seen_at":now.isoformat(),
            "last_seen_at":now.isoformat(),
            "expires_at":(
                now
                + timedelta(
                    seconds=HOT_WATCH_TTL_SECONDS
                )
            ).isoformat(),
            "reward_risk":eco.get("reward_risk"),
            "blocked_spread_pct":
                signal.get("spread_pct"),
            "reason":"spread_only",
        }

    state={
        "version":1,
        "updated_at":now.isoformat(),
        "ttl_seconds":HOT_WATCH_TTL_SECONDS,
        "count":len(items),
        "items":sorted(
            items.values(),
            key=lambda x:str(
                x.get("first_seen_at") or ""
            ),
        ),
    }
    atomic(HOT_WATCH_QUEUE,state)

    return {
        "active":len(items),
        "added":len(set(items)-before),
    }



def load_liquidity_promotions():
    now=datetime.now(timezone.utc)

    try:
        data=json.loads(
            LIQUIDITY_PROMOTIONS.read_text()
        )
        if not isinstance(data,dict):
            return []
    except Exception:
        return []

    rows=[]

    for item in (
        data.get("items",{}) or {}
    ).values():
        if not isinstance(item,dict):
            continue

        ready_until=parse_dt(
            item.get("ready_until")
        )
        if (
            ready_until is None
            or ready_until <= now
        ):
            continue

        row=dict(item)
        row["liquidity_promoted"]=True
        rows.append(row)

    rows.sort(
        key=lambda x:(
            -float(x.get("priority") or 0),
            str(x.get("promoted_at") or ""),
        )
    )

    return rows[
        :MAX_LIQUIDITY_PROMOTIONS
    ]


def merge_promoted_candidates(
    normal,
    promoted,
):
    result=list(normal)
    seen={
        str(x.get("symbol") or "").upper()
        for x in result
    }

    for row in promoted:
        symbol=str(
            row.get("symbol") or ""
        ).upper()
        if not symbol or symbol in seen:
            continue
        result.append(row)
        seen.add(symbol)

    return result



def next_15m_check(now=None):
    now=now or datetime.now(timezone.utc)
    ts=int(now.timestamp())
    next_close=((ts // 900) + 1) * 900
    return datetime.fromtimestamp(
        next_close + 30,
        tz=timezone.utc,
    )


def load_signal_watch_due():
    now=datetime.now(timezone.utc)

    try:
        state=json.loads(
            SIGNAL_WATCH_STATE.read_text()
        )
        if not isinstance(state,dict):
            state={}
    except Exception:
        state={}

    items=state.get("items")
    if not isinstance(items,dict):
        items={}

    due=[]

    for symbol in list(items):
        item=items[symbol]

        expires=parse_dt(
            item.get("expires_at")
        )
        if expires is None or expires <= now:
            del items[symbol]
            continue

        check=parse_dt(
            item.get("next_check_at")
        )

        if check is not None and check <= now:
            row=dict(item.get("source") or {})
            row["symbol"]=symbol
            row["signal_watch"]=True
            due.append(row)

    due.sort(
        key=lambda x:float(
            x.get("priority") or 0
        ),
        reverse=True,
    )

    return state,items,due[:MAX_SIGNAL_WATCH_DUE]


def save_signal_watch(
    state,
    items,
):
    state["version"]=1
    state["updated_at"]=datetime.now(
        timezone.utc
    ).isoformat()
    state["ttl_seconds"] = (
        SIGNAL_WATCH_TTL_SECONDS
    )
    state["count"]=len(items)
    state["items"]=items
    atomic(SIGNAL_WATCH_STATE,state)


def update_signal_watch_item(
    items,
    early,
    analysis,
    execution_prefilter_except_spread,
    has_valid_tb,
):
    symbol=str(
        early.get("symbol") or ""
    ).upper()

    if not symbol:
        return

    # Ook een geldige TB-LONG blijft tijdelijk gevolgd.
    # Het individuele execution-contract mag na 20 minuten
    # verlopen, maar op een volgende afgesloten 15m-candle
    # krijgt de munt opnieuw de volledige strategiecheck.
    regime=str(
        analysis.get("market_regime") or ""
    ).upper()

    # Alleen een liquide early mover in een
    # bullish hoger-timeframe regime vasthouden.
    if (
        not execution_prefilter_except_spread
        or not regime.startswith("BULLISH")
    ):
        return

    now=datetime.now(timezone.utc)
    existing=items.get(symbol)

    if existing is None:
        existing={
            "symbol":symbol,
            "first_seen_at":now.isoformat(),
            "expires_at":(
                now
                + timedelta(
                    seconds=
                        SIGNAL_WATCH_TTL_SECONDS
                )
            ).isoformat(),
        }
        items[symbol]=existing

    source={
        "symbol":symbol,
        "liquidity":early.get(
            "liquidity","PASS"
        ),
        "priority":early.get(
            "priority",0
        ),
        "move_1m_pct":early.get(
            "move_1m_pct"
        ),
        "move_5m_pct":early.get(
            "move_5m_pct"
        ),
        "move_15m_pct":early.get(
            "move_15m_pct"
        ),
        "volume_quote_24h":
            early.get("volume_quote_24h"),
        "spread_pct":early.get(
            "spread_pct"
        ),
    }

    existing["source"]=source
    existing["last_scanned_at"]=now.isoformat()
    existing["last_regime"]=regime
    existing["next_check_at"]=(
        next_15m_check(now).isoformat()
    )


def self_test():
    rows=[
        {"symbol":"A/EUR","liquidity":"LOW","priority":9},
        {"symbol":"B/EUR","liquidity":"PASS","priority":3},
        {"symbol":"C/EUR","liquidity":"WATCH","priority":5},
    ]
    chosen=[
        x for x in rows
        if x["liquidity"] in {"PASS","WATCH"}
    ]
    chosen.sort(key=lambda x:x["priority"],reverse=True)
    assert [x["symbol"] for x in chosen] == ["C/EUR","B/EUR"]
    fake = {
        "detected_at": "2026-08-25T08:00:00+00:00",
        "candle_timestamp": "2026-08-25T07:45:00+00:00",
        "symbol": "TEST/EUR",
        "strategy": "trend_breakout",
        "side": "LONG",
        "market_regime": "BULLISH",
        "regime_strength": 100,
        "score": 90,
        "entry_price": 100.0,
        "take_profit": 104.0,
        "stop_loss": 98.0,
        "rsi": 60.0,
        "atr_pct": 1.0,
        "volume_ratio": 2.0,
        "spread_pct": 0.05,
        "quote_volume": 500000,
        "change_pct_24h": 6.0,
        "shadow_eligible": True,
        "shadow_rejection_reasons": [],
        "reasons": ["test"],
        "selection_reason": "EARLY_MOVER_1M",
        "economics": {
            "expected_net_pct": 1.0,
            "risk_net_pct": 0.5,
            "reward_risk": 2.0,
            "expected_profit_eur": 1.30,
            "expected_loss_eur": 0.65,
            "expected_eur": {
                "120": 1.20,
                "125": 1.25,
                "130": 1.30,
                "135": 1.35,
            },
        },
    }
    assert selective_accepts(fake)
    assert bridge_row(fake)["selection_reason"] == "EARLY_MOVER_1M"
    assert selective_candidate_key(fake).startswith(
        "TEST/EUR|trend_breakout|LONG|"
    )

    blocked=dict(fake)
    blocked["shadow_eligible"]=False
    blocked["shadow_rejection_reasons"]=[
        "spread 0.3452% hoger dan 0.1000%"
    ]
    assert spread_only_hot_watch_signal(
        blocked,1.20
    )

    blocked_bad=dict(blocked)
    blocked_bad["shadow_rejection_reasons"]=[
        "spread 0.3452% hoger dan 0.1000%",
        "risico/winst 0.900 lager dan 1.200",
    ]
    assert not spread_only_hot_watch_signal(
        blocked_bad,1.20
    )

    merged=merge_promoted_candidates(
        [{"symbol":"AAA/EUR"}],
        [
            {
                "symbol":"AAA/EUR",
                "liquidity_promoted":True,
            },
            {
                "symbol":"BBB/EUR",
                "liquidity_promoted":True,
            },
        ],
    )
    assert [
        x["symbol"] for x in merged
    ] == ["AAA/EUR","BBB/EUR"]

    test_now=datetime(
        2026,8,25,12,13,0,
        tzinfo=timezone.utc,
    )
    assert next_15m_check(
        test_now
    ).isoformat() == (
        "2026-08-25T12:15:30+00:00"
    )

    print("SELF TEST: PASS")

def run():
    if not SOURCE.exists():
        raise SystemExit("STOP: early-mover bron ontbreekt")

    source=json.loads(SOURCE.read_text())

    generated=source.get("generated_at_utc")
    if generated:
        dt=datetime.fromisoformat(generated.replace("Z","+00:00"))
        age=(datetime.now(timezone.utc)-dt).total_seconds()
        if age > MAX_SOURCE_AGE:
            raise SystemExit(f"STOP: early-mover bron is {age:.0f}s oud")

    candidates=[
        x for x in source.get("candidates",[])
        if x.get("liquidity") in {"PASS","WATCH"}
    ]
    candidates.sort(
        key=lambda x: float(x.get("priority") or 0),
        reverse=True
    )
    candidates=candidates[:MAX_MARKETS]

    liquidity_promotions=(
        load_liquidity_promotions()
    )
    candidates=merge_promoted_candidates(
        candidates,
        liquidity_promotions,
    )

    signal_watch_state,signal_watch_items,signal_watch_due=(
        load_signal_watch_due()
    )
    candidates=merge_promoted_candidates(
        candidates,
        signal_watch_due,
    )

    ex=ccxt.bitvavo({
        "enableRateLimit":True,
        "timeout":30000,
    })
    ex.load_markets()
    tickers=ex.fetch_tickers()

    cfg=ms.settings(ms.load_yaml(ms.CFG_FILE),20)

    results=[]
    errors=[]
    bridge_candidates=[]
    hot_watch_candidates=[]

    for early in candidates:
        symbol=early["symbol"]
        try:
            record=ms.market_record_for_symbol(
                ex,tickers,symbol
            )
            if record is None:
                raise RuntimeError("geen geldige markt/ticker")

            if early.get("signal_watch"):
                selection_reason=(
                    "EARLY_MOVER_SIGNAL_WATCH"
                )
            elif early.get(
                "liquidity_promoted"
            ):
                selection_reason=(
                    "EARLY_MOVER_LIQUIDITY_PROMOTION"
                )
            else:
                selection_reason=(
                    "EARLY_MOVER_1M"
                )
            record["selection_reason"] = (
                selection_reason
            )

            base = str(record.get("base") or "").upper()
            execution_prefilter_except_spread = bool(
                base
                and base not in cfg["exclude_bases"]
                and not ms.leveraged_token(base)
                and float(record.get("quote_volume") or 0)
                    >= float(cfg["min_quote_volume"])
            )

            execution_prefilter = bool(
                execution_prefilter_except_spread
                and float(record.get("spread_pct") or 999)
                    <= float(cfg["max_spread_pct"])
            )

            analysis=ms.analyse_symbol(
                ex,
                record,
                cfg,
            )

            signals=[]
            full_signals=analysis.get("signals",[])

            has_valid_tb=any(
                selective_accepts(s)
                and str(
                    s.get("side") or ""
                ).upper()=="LONG"
                and str(
                    s.get("strategy") or ""
                )=="trend_breakout"
                for s in full_signals
            )

            update_signal_watch_item(
                signal_watch_items,
                early,
                analysis,
                execution_prefilter_except_spread,
                has_valid_tb,
            )

            for s in full_signals:
                s["selection_reason"]=(
                    selection_reason
                )

                if (
                    execution_prefilter_except_spread
                    and spread_only_hot_watch_signal(
                        s,
                        cfg["min_reward_risk"],
                    )
                ):
                    hot_watch_candidates.append(s)

                if (
                    execution_prefilter
                    and selective_accepts(s)
                    and str(s.get("side") or "").upper() == "LONG"
                    and str(s.get("strategy") or "")
                        == "trend_breakout"
                ):
                    bridge_candidates.append(s)

                eco=s.get("economics") or {}
                signals.append({
                    "strategy":s.get("strategy"),
                    "side":s.get("side"),
                    "score":s.get("score"),
                    "market_regime":s.get("market_regime"),
                    "shadow_eligible":s.get("shadow_eligible"),
                    "rejections":s.get(
                        "shadow_rejection_reasons",[]
                    ),
                    "reward_risk":eco.get("reward_risk"),
                    "expected_profit_eur":
                        eco.get("expected_profit_eur"),
                })

            results.append({
                "symbol":symbol,
                "liquidity":early.get("liquidity"),
                "move_1m_pct":early.get("move_1m_pct"),
                "move_5m_pct":early.get("move_5m_pct"),
                "move_15m_pct":early.get("move_15m_pct"),
                "spread_pct":early.get("spread_pct"),
                "volume_quote_24h":
                    early.get("volume_quote_24h"),
                "market_regime":
                    analysis.get("market_regime"),
                "rsi_15m":analysis.get("rsi_15m"),
                "atr_pct_15m":
                    analysis.get("atr_pct_15m"),
                "signal_count":len(signals),
                "execution_prefilter":execution_prefilter,
                "liquidity_promoted":bool(
                    early.get(
                        "liquidity_promoted"
                    )
                ),
                "signal_watch":bool(
                    early.get(
                        "signal_watch"
                    )
                ),
                "signals":signals,
            })

        except Exception as e:
            errors.append({
                "symbol":symbol,
                "error":f"{type(e).__name__}: {e}",
            })

    save_signal_watch(
        signal_watch_state,
        signal_watch_items,
    )

    hot_watch=update_hot_watch(
        hot_watch_candidates
    )
    bridge=publish_bridge_signals(
        bridge_candidates
    )

    now=datetime.now(timezone.utc)
    report={
        "generated_at_utc":now.isoformat(),
        "generated_at_nl":
            now.astimezone(NL).isoformat(),
        "source_candidates":
            source.get("candidate_count",0),
        "pass_watch_candidates":len(candidates),
        "deep_scanned":len(results),
        "liquidity_promotions_scanned":
            sum(
                1 for x in results
                if x.get(
                    "liquidity_promoted"
                )
            ),
        "signal_watch_active":
            len(signal_watch_items),
        "signal_watch_scanned":
            sum(
                1 for x in results
                if x.get("signal_watch")
            ),
        "errors":errors,
        "auto_candidate_bridge":True,
        "orders_placed_by_this_process":False,
        "live_rules_changed":False,
        "bridge":bridge,
        "hot_watch":hot_watch,
        "results":results,
    }
    atomic(OUTPUT,report)

    print("--- EARLY MOVER DEEP SCAN ---")
    print("early movers :", report["source_candidates"])
    print("PASS/WATCH   :", len(candidates))
    print("deep scanned :", len(results))
    print(
        "liq promoted :",
        report[
            "liquidity_promotions_scanned"
        ],
    )
    print(
        "signal watch :",
        report["signal_watch_active"],
    )
    print(
        "watch scan   :",
        report["signal_watch_scanned"],
    )
    print("errors       :", len(errors))
    print("bridge mode  :", bridge["mode"])
    print("bridge valid :", bridge["eligible"])
    print("bridge nieuw :", bridge["emitted"])
    print("hot watch    :", hot_watch["active"])
    print("hot nieuw    :", hot_watch["added"])
    print()

    for r in results:
        sig=[
            f"{x['strategy']}:{x['side']}"
            for x in r["signals"]
        ]
        print(
            f"{r['symbol']:12} "
            f"1m={r['move_1m_pct'] or 0:+.2f}% "
            f"5m={r['move_5m_pct'] or 0:+.2f}% "
            f"15m={r['move_15m_pct'] or 0:+.2f}% "
            f"{r['liquidity']:5} "
            f"signals={len(sig)} "
            f"{','.join(sig) if sig else '-'}"
        )

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--self-test",action="store_true")
    ap.add_argument("--once",action="store_true")
    ap.add_argument("--loop",action="store_true")
    a=ap.parse_args()

    if a.self_test:
        self_test()
        return

    if a.once:
        run()
        return

    while True:
        try:
            run()
        except Exception as e:
            print(
                "DEEP SCAN FOUT:",
                type(e).__name__,
                e,
                flush=True,
            )
        time.sleep(60)

if __name__=="__main__":
    main()
