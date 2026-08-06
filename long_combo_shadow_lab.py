#!/usr/bin/env python3
"""
Diamond Trader LONG Combo Shadow Lab v1.0

Doel
----
Volgt vanaf de nulmeting ieder NIEUW geldig Diamond Trader LONG-signaal
voor de normale LONG-symbolen uit config.yaml en vergelijkt drie varianten:

1. CURRENT
   - entry: eerstvolgende 15m-candle na het signaal
   - minimum netto winst voor winstexit: €1.00
   - trailing mag winst vastzetten vanaf: €1.00 netto

2. WAIT30_100
   - entry: 30 minuten later
   - minimum netto winst voor winstexit: €1.00
   - trailing mag winst vastzetten vanaf: €1.00 netto

3. WAIT30_050
   - entry: 30 minuten later
   - minimum netto winst voor winstexit: €0.50
   - trailing mag winst vastzetten vanaf: €1.00 netto

Alle overige LONG-regels blijven gelijk:
- zelfde LONG-signaal
- zelfde virtuele inzet
- zelfde taker fee
- zelfde spreadproxy
- zelfde ATR stop-loss
- zelfde harde stop-loss
- zelfde ATR take-profit
- zelfde profit trailing trigger/pullback
- zelfde ATR trailing
- zelfde trend-break exit
- zelfde 15m closed-candle verwerking

Veiligheid
----------
- GEEN orders
- GEEN private exchange-methoden
- GEEN API-sleutels worden aan ccxt gegeven
- config.yaml wordt alleen gelezen
- diamond_state.json wordt nooit gelezen of gewijzigd
- diamond_transactions.csv wordt nooit gewijzigd
- uitsluitend eigen Combo Shadow-bestanden in /var/data

Gebruik
-------
python3 long_combo_shadow_lab.py --self-test
python3 long_combo_shadow_lab.py --update
python3 long_combo_shadow_lab.py --status

De eerste --update maakt de nulmeting.
Alleen signalen waarvan de signaalcandle NA die nulmeting sluit, tellen mee.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import ccxt
import pandas as pd

from diamond_bot import (
    enrich_indicators,
    get_cfg,
    load_yaml,
    to_bool,
    to_float,
)

VERSION = "1.0"
MODE = "READ_ONLY_LONG_COMBO_SHADOW"

TARGET_SIGNALS = 20
TIMEFRAME = "15m"
TIMEFRAME_MS = 15 * 60 * 1000
MAX_HOLD_MINUTES = 48 * 60
MAX_SIGNAL_KEYS = 5000

PROJECT_DIR = Path(
    os.getenv("DIAMOND_PROJECT_DIR", "/opt/render/project/src")
)
DATA_DIR = Path(
    os.getenv("DIAMOND_DATA_DIR", "/var/data")
)
CONFIG_FILE = Path(
    os.getenv("CFG_FILE", str(PROJECT_DIR / "config.yaml"))
)

BASELINE_FILE = DATA_DIR / "diamond_long_combo_shadow_baseline.json"
STATE_FILE = DATA_DIR / "diamond_long_combo_shadow_state.json"
REPORT_FILE = DATA_DIR / "diamond_long_combo_shadow_report.json"
TRADES_FILE = DATA_DIR / "diamond_long_combo_shadow_trades.csv"

VARIANTS = {
    "CURRENT": {
        "wait_minutes": 0,
        "exit_min_profit_eur": 1.00,
        "trail_lock_min_eur": 1.00,
    },
    "WAIT30_100": {
        "wait_minutes": 30,
        "exit_min_profit_eur": 1.00,
        "trail_lock_min_eur": 1.00,
    },
    "WAIT30_050": {
        "wait_minutes": 30,
        "exit_min_profit_eur": 0.50,
        "trail_lock_min_eur": 1.00,
    },
}

SAFETY = {
    "orders_possible": False,
    "private_exchange_calls": False,
    "api_keys_loaded": False,
    "config_write": False,
    "bot_state_write": False,
    "transactions_write": False,
    "own_files_only": True,
}


def now_dt() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_dt().isoformat()


def parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(
        ms / 1000.0,
        tz=timezone.utc,
    ).isoformat()


def save_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
    ) as tmp:
        json.dump(
            data,
            tmp,
            indent=2,
            ensure_ascii=False,
        )
        tmp_name = tmp.name

    os.replace(tmp_name, path)


def load_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default

        return json.loads(
            path.read_text(encoding="utf-8")
        )
    except Exception:
        return default


def append_trade(row: Dict[str, Any]) -> None:
    columns = [
        "closed_at",
        "signal_id",
        "signal_candle",
        "symbol",
        "variant",
        "wait_minutes",
        "exit_min_profit_eur",
        "trail_lock_min_eur",
        "entry_at",
        "entry_price",
        "exit_at",
        "exit_price",
        "exit_reason",
        "holding_minutes",
        "signal_atr",
        "signal_atr_pct",
        "signal_rsi",
        "spread_proxy_pct",
        "stake_eur",
        "buy_fee_eur",
        "sell_fee_eur",
        "net_pnl_eur",
    ]

    exists = TRADES_FILE.exists()

    TRADES_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with TRADES_FILE.open(
        "a",
        encoding="utf-8",
        newline="",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=columns,
        )

        if not exists:
            writer.writeheader()

        writer.writerow(
            {
                key: row.get(key, "")
                for key in columns
            }
        )


def load_settings() -> Dict[str, Any]:
    cfg = load_yaml(str(CONFIG_FILE))

    symbols = [
        str(item).strip().upper()
        for item in (cfg.get("symbols") or [])
        if str(item).strip()
    ]

    return {
        "symbols": symbols,
        "timeframe": str(
            cfg.get("timeframe", TIMEFRAME)
        ),
        "stake_eur": to_float(
            get_cfg(
                cfg,
                "risk.fixed_stake_quote",
                120,
            ),
            120.0,
        ),
        "fee_pct": to_float(
            get_cfg(
                cfg,
                "fees.taker_fee_pct",
                0.25,
            ),
            0.25,
        ),
        "config_min_profit_eur": to_float(
            get_cfg(
                cfg,
                "risk.min_profit_eur",
                1.00,
            ),
            1.00,
        ),
        "sma_fast": int(
            to_float(
                get_cfg(
                    cfg,
                    "signals.sma_fast",
                    20,
                ),
                20,
            )
        ),
        "sma_slow": int(
            to_float(
                get_cfg(
                    cfg,
                    "signals.sma_slow",
                    60,
                ),
                60,
            )
        ),
        "require_crossover": to_bool(
            get_cfg(
                cfg,
                "signals.require_crossover",
                True,
            ),
            True,
        ),
        "use_sma": to_bool(
            get_cfg(
                cfg,
                "signals.use_sma",
                True,
            ),
            True,
        ),
        "rsi_len": int(
            to_float(
                get_cfg(
                    cfg,
                    "signals.rsi_len",
                    14,
                ),
                14,
            )
        ),
        "use_rsi": to_bool(
            get_cfg(
                cfg,
                "signals.use_rsi",
                True,
            ),
            True,
        ),
        "rsi_min": to_float(
            get_cfg(
                cfg,
                "signals.rsi_buy_min",
                55,
            ),
            55.0,
        ),
        "rsi_max": to_float(
            get_cfg(
                cfg,
                "signals.rsi_buy_max",
                70,
            ),
            70.0,
        ),
        "atr_len": int(
            to_float(
                get_cfg(
                    cfg,
                    "signals.atr_len",
                    14,
                ),
                14,
            )
        ),
        "use_atr": to_bool(
            get_cfg(
                cfg,
                "signals.use_atr_filter",
                True,
            ),
            True,
        ),
        "min_atr_pct": to_float(
            get_cfg(
                cfg,
                "signals.min_atr_pct",
                0.30,
            ),
            0.30,
        ),
        "atr_tp_mult": to_float(
            get_cfg(
                cfg,
                "signals.atr_tp_mult",
                2.6,
            ),
            2.6,
        ),
        "atr_sl_mult": to_float(
            get_cfg(
                cfg,
                "signals.atr_sl_mult",
                1.2,
            ),
            1.2,
        ),
        "hard_sl_pct": to_float(
            get_cfg(
                cfg,
                "signals.hard_stop_loss_pct",
                3.0,
            ),
            3.0,
        ),
        "trailing_enabled": to_bool(
            get_cfg(
                cfg,
                "signals.trailing_enabled",
                True,
            ),
            True,
        ),
        "trailing_atr_mult": to_float(
            get_cfg(
                cfg,
                "signals.trailing_atr_mult",
                1.2,
            ),
            1.2,
        ),
        "profit_trigger_pct": to_float(
            get_cfg(
                cfg,
                "signals.profit_trailing_trigger_pct",
                1.0,
            ),
            1.0,
        ),
        "profit_pullback_pct": to_float(
            get_cfg(
                cfg,
                "signals.profit_trailing_pullback_pct",
                0.5,
            ),
            0.5,
        ),
        "exit_on_trend_break": to_bool(
            get_cfg(
                cfg,
                "signals.exit_on_trend_break",
                True,
            ),
            True,
        ),
    }


def public_exchange() -> ccxt.Exchange:
    exchange = ccxt.bitvavo(
        {
            "enableRateLimit": True,
            "options": {
                "fetchMarkets": {
                    "types": ["spot"],
                }
            },
        }
    )

    exchange.load_markets()
    return exchange


def fetch_frame(
    exchange: ccxt.Exchange,
    symbol: str,
    since_ms: Optional[int] = None,
    limit: int = 500,
) -> pd.DataFrame:
    rows = exchange.fetch_ohlcv(
        symbol,
        TIMEFRAME,
        since=since_ms,
        limit=limit,
    ) or []

    if not rows:
        raise RuntimeError(
            f"geen OHLCV voor {symbol}"
        )

    frame = pd.DataFrame(
        rows,
        columns=[
            "ts",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ],
    )

    for col in [
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]:
        frame[col] = pd.to_numeric(
            frame[col],
            errors="coerce",
        )

    frame["ts"] = pd.to_numeric(
        frame["ts"],
        errors="coerce",
    )

    return (
        frame
        .dropna()
        .drop_duplicates("ts")
        .sort_values("ts")
        .reset_index(drop=True)
    )


def enrich(
    frame: pd.DataFrame,
    settings: Dict[str, Any],
) -> pd.DataFrame:
    return enrich_indicators(
        frame,
        settings["sma_fast"],
        settings["sma_slow"],
        settings["rsi_len"],
        settings["atr_len"],
    )


def closed_only(frame: pd.DataFrame) -> pd.DataFrame:
    cutoff = int(time.time() * 1000)

    return frame[
        (frame["ts"] + TIMEFRAME_MS) <= cutoff
    ].copy()


def is_long_signal(
    prev: pd.Series,
    row: pd.Series,
    settings: Dict[str, Any],
) -> bool:
    fast_prev = to_float(
        prev["sma_fast"],
        0.0,
    )
    slow_prev = to_float(
        prev["sma_slow"],
        0.0,
    )
    fast_now = to_float(
        row["sma_fast"],
        0.0,
    )
    slow_now = to_float(
        row["sma_slow"],
        0.0,
    )
    close_now = to_float(
        row["close"],
        0.0,
    )
    rsi_now = to_float(
        row["rsi"],
        50.0,
    )
    atr_now = to_float(
        row["atr"],
        0.0,
    )
    atr_pct = to_float(
        row["atr_pct"],
        0.0,
    )

    cross_up = (
        fast_prev <= slow_prev
        and fast_now > slow_now
    )

    trend_ok = (
        fast_now > slow_now
        and close_now > fast_now
    )

    if settings["use_sma"]:
        if settings["require_crossover"]:
            sma_ok = (
                cross_up
                and trend_ok
            )
        else:
            sma_ok = trend_ok
    else:
        sma_ok = True

    if settings["use_rsi"]:
        rsi_ok = (
            settings["rsi_min"]
            <= rsi_now
            <= settings["rsi_max"]
        )
    else:
        rsi_ok = True

    if settings["use_atr"]:
        atr_ok = (
            atr_pct
            >= settings["min_atr_pct"]
        )
    else:
        atr_ok = True

    return bool(
        sma_ok
        and rsi_ok
        and atr_ok
        and atr_now > 0
    )


def signal_id(
    symbol: str,
    candle_ts: int,
) -> str:
    return f"{symbol}|{int(candle_ts)}"


def ensure_baseline(
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    existing = load_json(
        BASELINE_FILE,
        {},
    )

    if (
        isinstance(existing, dict)
        and existing.get("started_at")
    ):
        return existing

    baseline = {
        "version": VERSION,
        "mode": MODE,
        "started_at": now_iso(),
        "target_signals": TARGET_SIGNALS,
        "variants": VARIANTS,
        "settings_at_start": settings,
        "safety": SAFETY,
    }

    save_json_atomic(
        BASELINE_FILE,
        baseline,
    )

    return baseline


def default_state(
    baseline: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "version": VERSION,
        "mode": MODE,
        "started_at": baseline["started_at"],
        "last_update_at": None,
        "processed_signal_keys": [],
        "signals": {},
        "positions": {},
        "closed": [],
        "errors": [],
        "settings": {},
    }


def load_state(
    baseline: Dict[str, Any],
) -> Dict[str, Any]:
    state = load_json(
        STATE_FILE,
        default_state(baseline),
    )

    if not isinstance(state, dict):
        state = default_state(baseline)

    base = default_state(baseline)
    base.update(state)

    expected_types = [
        ("processed_signal_keys", list),
        ("signals", dict),
        ("positions", dict),
        ("closed", list),
        ("errors", list),
    ]

    for key, expected in expected_types:
        if not isinstance(
            base.get(key),
            expected,
        ):
            base[key] = expected()

    base["version"] = VERSION
    base["mode"] = MODE
    base["started_at"] = baseline["started_at"]

    return base


def ticker_spread_pct(
    exchange: ccxt.Exchange,
    symbol: str,
) -> float:
    try:
        ticker = exchange.fetch_ticker(symbol)

        bid = to_float(
            ticker.get("bid"),
            0.0,
        )
        ask = to_float(
            ticker.get("ask"),
            0.0,
        )

        if bid > 0 and ask > 0:
            mid = (ask + bid) / 2.0

            if mid > 0:
                return max(
                    0.0,
                    (ask - bid) / mid * 100.0,
                )

    except Exception:
        pass

    return 0.0


def ingest_signals(
    exchange: ccxt.Exchange,
    state: Dict[str, Any],
    baseline_dt: datetime,
    settings: Dict[str, Any],
    frames: Dict[str, pd.DataFrame],
) -> None:
    processed = set(
        str(item)
        for item in state.get(
            "processed_signal_keys",
            [],
        )
    )

    baseline_ms = int(
        baseline_dt.timestamp() * 1000
    )

    for symbol in settings["symbols"]:
        frame = frames.get(symbol)

        if frame is None or frame.empty:
            continue

        closed = closed_only(
            enrich(
                frame,
                settings,
            )
        ).reset_index(drop=True)

        minimum_rows = max(
            settings["sma_slow"] + 2,
            80,
        )

        if len(closed) < minimum_rows:
            continue

        for idx in range(
            1,
            len(closed),
        ):
            prev = closed.iloc[idx - 1]
            row = closed.iloc[idx]

            candle_ts = int(
                row["ts"]
            )

            candle_close_ms = (
                candle_ts
                + TIMEFRAME_MS
            )

            if candle_close_ms <= baseline_ms:
                continue

            key = signal_id(
                symbol,
                candle_ts,
            )

            if key in processed:
                continue

            if not is_long_signal(
                prev,
                row,
                settings,
            ):
                continue

            spread = ticker_spread_pct(
                exchange,
                symbol,
            )

            signal = {
                "signal_id": key,
                "symbol": symbol,
                "signal_candle_ms": candle_ts,
                "signal_candle": ms_to_iso(candle_ts),
                "signal_closed_at": ms_to_iso(
                    candle_close_ms
                ),
                "signal_close": to_float(
                    row["close"],
                    0.0,
                ),
                "atr": to_float(
                    row["atr"],
                    0.0,
                ),
                "atr_pct": to_float(
                    row["atr_pct"],
                    0.0,
                ),
                "rsi": to_float(
                    row["rsi"],
                    50.0,
                ),
                "sma_fast": to_float(
                    row["sma_fast"],
                    0.0,
                ),
                "sma_slow": to_float(
                    row["sma_slow"],
                    0.0,
                ),
                "spread_proxy_pct": spread,
                "detected_at": now_iso(),
            }

            state["signals"][key] = signal
            processed.add(key)

            for variant, spec in VARIANTS.items():
                pos_key = f"{key}|{variant}"

                wait_minutes = int(
                    spec["wait_minutes"]
                )

                state["positions"][pos_key] = {
                    "position_id": pos_key,
                    "signal_id": key,
                    "symbol": symbol,
                    "variant": variant,
                    "wait_minutes": wait_minutes,
                    "exit_min_profit_eur": float(
                        spec["exit_min_profit_eur"]
                    ),
                    "trail_lock_min_eur": float(
                        spec["trail_lock_min_eur"]
                    ),
                    "signal_candle_ms": candle_ts,
                    "entry_target_ms": (
                        candle_close_ms
                        + wait_minutes * 60 * 1000
                    ),
                    "signal_atr": signal["atr"],
                    "signal_atr_pct": signal["atr_pct"],
                    "signal_rsi": signal["rsi"],
                    "spread_proxy_pct": spread,
                    "status": "PENDING_ENTRY",
                    "entry_at_ms": None,
                    "entry_price": None,
                    "amount": None,
                    "quote_amount": None,
                    "buy_fee": None,
                    "stop_loss": None,
                    "take_profit": None,
                    "highest": None,
                    "last_processed_candle_ms": None,
                }

    state["processed_signal_keys"] = list(
        sorted(processed)
    )[-MAX_SIGNAL_KEYS:]


def minimum_profitable_exit_price(
    position: Dict[str, Any],
    settings: Dict[str, Any],
    min_profit_eur: float,
) -> float:
    amount = to_float(
        position.get("amount"),
        0.0,
    )
    quote = to_float(
        position.get("quote_amount"),
        0.0,
    )
    buy_fee = to_float(
        position.get("buy_fee"),
        0.0,
    )

    if amount <= 0:
        return float("inf")

    sell_multiplier = (
        1.0
        - settings["fee_pct"] / 100.0
    )

    if sell_multiplier <= 0:
        return float("inf")

    required_net_proceeds = (
        quote
        + buy_fee
        + max(0.0, min_profit_eur)
    )

    return (
        required_net_proceeds
        / (amount * sell_multiplier)
    )


def estimated_pnl(
    position: Dict[str, Any],
    exit_price: float,
    settings: Dict[str, Any],
) -> Dict[str, float]:
    amount = to_float(
        position.get("amount"),
        0.0,
    )
    quote = to_float(
        position.get("quote_amount"),
        0.0,
    )
    buy_fee = to_float(
        position.get("buy_fee"),
        0.0,
    )

    gross = (
        amount
        * max(exit_price, 0.0)
    )

    sell_fee = (
        gross
        * settings["fee_pct"]
        / 100.0
    )

    return {
        "sell_fee": sell_fee,
        "net_pnl": (
            gross
            - sell_fee
            - quote
            - buy_fee
        ),
    }


def try_fill_pending(
    position: Dict[str, Any],
    frame: pd.DataFrame,
    settings: Dict[str, Any],
) -> None:
    if position.get("status") != "PENDING_ENTRY":
        return

    target = int(
        position["entry_target_ms"]
    )

    candidates = frame[
        frame["ts"] >= target
    ]

    if candidates.empty:
        return

    candle = candidates.iloc[0]
    candle_ts = int(
        candle["ts"]
    )

    if candle_ts > int(time.time() * 1000):
        return

    mid_open = to_float(
        candle["open"],
        0.0,
    )

    if mid_open <= 0:
        return

    spread_pct = to_float(
        position.get(
            "spread_proxy_pct"
        ),
        0.0,
    )

    entry_price = (
        mid_open
        * (
            1.0
            + spread_pct / 200.0
        )
    )

    stake = settings["stake_eur"]

    amount = (
        stake
        / entry_price
    )

    quote_amount = (
        amount
        * entry_price
    )

    buy_fee = (
        quote_amount
        * settings["fee_pct"]
        / 100.0
    )

    atr = to_float(
        position["signal_atr"],
        0.0,
    )

    atr_stop = (
        entry_price
        - atr
        * settings["atr_sl_mult"]
    )

    hard_stop = (
        entry_price
        * (
            1.0
            - settings["hard_sl_pct"] / 100.0
        )
    )

    position.update(
        {
            "status": "OPEN",
            "entry_at_ms": candle_ts,
            "entry_at": ms_to_iso(candle_ts),
            "entry_price": entry_price,
            "amount": amount,
            "quote_amount": quote_amount,
            "buy_fee": buy_fee,
            "stop_loss": max(
                atr_stop,
                hard_stop,
            ),
            "take_profit": (
                entry_price
                + atr
                * settings["atr_tp_mult"]
            ),
            "highest": entry_price,
            "last_processed_candle_ms": None,
        }
    )


def close_position(
    state: Dict[str, Any],
    pos_key: str,
    position: Dict[str, Any],
    exit_ms: int,
    exit_price: float,
    reason: str,
    settings: Dict[str, Any],
) -> None:
    pnl = estimated_pnl(
        position,
        exit_price,
        settings,
    )

    entry_ms = int(
        position.get("entry_at_ms")
        or exit_ms
    )

    row = {
        "closed_at": now_iso(),
        "signal_id": position["signal_id"],
        "signal_candle": ms_to_iso(
            int(position["signal_candle_ms"])
        ),
        "symbol": position["symbol"],
        "variant": position["variant"],
        "wait_minutes": int(
            position["wait_minutes"]
        ),
        "exit_min_profit_eur": round(
            to_float(
                position["exit_min_profit_eur"]
            ),
            2,
        ),
        "trail_lock_min_eur": round(
            to_float(
                position["trail_lock_min_eur"]
            ),
            2,
        ),
        "entry_at": ms_to_iso(entry_ms),
        "entry_price": round(
            to_float(
                position["entry_price"]
            ),
            12,
        ),
        "exit_at": ms_to_iso(exit_ms),
        "exit_price": round(
            exit_price,
            12,
        ),
        "exit_reason": reason,
        "holding_minutes": round(
            max(
                0.0,
                (exit_ms - entry_ms)
                / 60000.0,
            ),
            2,
        ),
        "signal_atr": round(
            to_float(
                position["signal_atr"]
            ),
            12,
        ),
        "signal_atr_pct": round(
            to_float(
                position["signal_atr_pct"]
            ),
            6,
        ),
        "signal_rsi": round(
            to_float(
                position["signal_rsi"]
            ),
            4,
        ),
        "spread_proxy_pct": round(
            to_float(
                position["spread_proxy_pct"]
            ),
            6,
        ),
        "stake_eur": round(
            to_float(
                position["quote_amount"]
            ),
            8,
        ),
        "buy_fee_eur": round(
            to_float(
                position["buy_fee"]
            ),
            8,
        ),
        "sell_fee_eur": round(
            pnl["sell_fee"],
            8,
        ),
        "net_pnl_eur": round(
            pnl["net_pnl"],
            8,
        ),
    }

    append_trade(row)

    state["closed"].append(row)

    state["positions"].pop(
        pos_key,
        None,
    )


def evaluate_open(
    state: Dict[str, Any],
    pos_key: str,
    position: Dict[str, Any],
    enriched_frame: pd.DataFrame,
    settings: Dict[str, Any],
) -> None:
    if position.get("status") != "OPEN":
        return

    entry_ms = int(
        position["entry_at_ms"]
    )

    last_processed = position.get(
        "last_processed_candle_ms"
    )

    closed = closed_only(
        enriched_frame
    )

    candles = closed[
        (closed["ts"] + TIMEFRAME_MS)
        > entry_ms
    ]

    if last_processed is not None:
        candles = candles[
            candles["ts"]
            > int(last_processed)
        ]

    if candles.empty:
        return

    entry_price = to_float(
        position["entry_price"],
        0.0,
    )

    spread_pct = to_float(
        position["spread_proxy_pct"],
        0.0,
    )

    exit_min_profit_eur = to_float(
        position["exit_min_profit_eur"],
        1.0,
    )

    trail_lock_min_eur = to_float(
        position["trail_lock_min_eur"],
        1.0,
    )

    exit_min_price = (
        minimum_profitable_exit_price(
            position,
            settings,
            exit_min_profit_eur,
        )
    )

    trail_lock_price = (
        minimum_profitable_exit_price(
            position,
            settings,
            trail_lock_min_eur,
        )
    )

    for _, candle in candles.iterrows():
        candle_ts = int(
            candle["ts"]
        )

        close_ms = (
            candle_ts
            + TIMEFRAME_MS
        )

        close_mid = to_float(
            candle["close"],
            0.0,
        )

        atr = to_float(
            candle["atr"],
            0.0,
        )

        fast = to_float(
            candle["sma_fast"],
            0.0,
        )

        slow = to_float(
            candle["sma_slow"],
            0.0,
        )

        if close_mid <= 0:
            position[
                "last_processed_candle_ms"
            ] = candle_ts
            continue

        bid = (
            close_mid
            * (
                1.0
                - spread_pct / 200.0
            )
        )

        position["highest"] = max(
            to_float(
                position.get(
                    "highest"
                ),
                entry_price,
            ),
            close_mid,
        )

        highest = to_float(
            position["highest"],
            entry_price,
        )

        if entry_price > 0:
            profit_pct = (
                (
                    close_mid
                    - entry_price
                )
                / entry_price
                * 100.0
            )
        else:
            profit_pct = 0.0

        stop_loss = to_float(
            position["stop_loss"],
            0.0,
        )

        # Profit trailing: pas winst vastzetten vanaf trail_lock_min_eur.
        if (
            profit_pct
            >= settings["profit_trigger_pct"]
            and highest > 0
        ):
            tight_stop = (
                highest
                * (
                    1.0
                    - settings[
                        "profit_pullback_pct"
                    ]
                    / 100.0
                )
            )

            if (
                tight_stop
                >= trail_lock_price
                and tight_stop
                > stop_loss
            ):
                position["stop_loss"] = (
                    tight_stop
                )
                stop_loss = tight_stop

        # ATR trailing gebruikt dezelfde afzonderlijke trail-lock grens.
        if (
            settings["trailing_enabled"]
            and atr > 0
            and highest > 0
        ):
            atr_trail = (
                highest
                - atr
                * settings[
                    "trailing_atr_mult"
                ]
            )

            if (
                atr_trail
                >= trail_lock_price
                and atr_trail
                > stop_loss
            ):
                position["stop_loss"] = (
                    atr_trail
                )
                stop_loss = atr_trail

        hard_stop = (
            entry_price
            * (
                1.0
                - settings["hard_sl_pct"]
                / 100.0
            )
        )

        reason = None

        if close_mid <= hard_stop:
            reason = "hard_stop_loss"

        elif (
            stop_loss > 0
            and close_mid <= stop_loss
        ):
            if (
                stop_loss >= trail_lock_price
                and highest > entry_price
            ):
                reason = "trailing_stop"
            else:
                reason = "stop_loss"

        else:
            trailing_active = (
                stop_loss >= trail_lock_price
                and math.isfinite(
                    trail_lock_price
                )
            )

            take_profit = to_float(
                position["take_profit"],
                0.0,
            )

            if (
                take_profit > 0
                and close_mid >= take_profit
                and not trailing_active
            ):
                reason = "take_profit"

            elif (
                settings["exit_on_trend_break"]
                and fast < slow
            ):
                reason = "trend_break"

        if reason is not None:
            net_pnl = estimated_pnl(
                position,
                bid,
                settings,
            )["net_pnl"]

            # Stop-loss/hard-stop altijd toegestaan.
            # Winstexit/trend/trailing pas als minimaal exit_min_profit_eur
            # NETTO na fees overblijft.
            if (
                reason
                not in {
                    "stop_loss",
                    "hard_stop_loss",
                }
                and net_pnl + 1e-12
                < exit_min_profit_eur
            ):
                reason = None

        holding_min = (
            close_ms - entry_ms
        ) / 60000.0

        if (
            reason is None
            and holding_min
            >= MAX_HOLD_MINUTES
        ):
            reason = "max_hold_48h"

        position[
            "last_processed_candle_ms"
        ] = candle_ts

        if reason is not None:
            close_position(
                state,
                pos_key,
                position,
                close_ms,
                bid,
                reason,
                settings,
            )
            return


def prepare_frames(
    exchange: ccxt.Exchange,
    settings: Dict[str, Any],
    baseline_dt: datetime,
) -> Dict[str, pd.DataFrame]:
    since_ms = int(
        (
            baseline_dt.timestamp()
            - 36 * 60 * 60
        )
        * 1000
    )

    frames: Dict[str, pd.DataFrame] = {}

    for symbol in settings["symbols"]:
        try:
            frames[symbol] = fetch_frame(
                exchange,
                symbol,
                since_ms=since_ms,
                limit=500,
            )
        except Exception:
            continue

    return frames


def update_positions(
    state: Dict[str, Any],
    settings: Dict[str, Any],
    frames: Dict[str, pd.DataFrame],
) -> None:
    enriched_cache: Dict[
        str,
        pd.DataFrame,
    ] = {}

    for pos_key in list(
        state["positions"].keys()
    ):
        position = state["positions"].get(
            pos_key
        )

        if not isinstance(
            position,
            dict,
        ):
            continue

        symbol = position.get(
            "symbol"
        )

        frame = frames.get(
            symbol
        )

        if (
            frame is None
            or frame.empty
        ):
            continue

        try_fill_pending(
            position,
            frame,
            settings,
        )

        if position.get("status") != "OPEN":
            continue

        if symbol not in enriched_cache:
            enriched_cache[symbol] = enrich(
                frame,
                settings,
            )

        evaluate_open(
            state,
            pos_key,
            position,
            enriched_cache[symbol],
            settings,
        )


def summary_for_variant(
    closed: List[Dict[str, Any]],
    variant: str,
) -> Dict[str, Any]:
    rows = [
        row
        for row in closed
        if row.get("variant")
        == variant
    ]

    pnls = [
        to_float(
            row.get(
                "net_pnl_eur"
            ),
            0.0,
        )
        for row in rows
    ]

    wins = sum(
        pnl > 0
        for pnl in pnls
    )

    losses = sum(
        pnl <= 0
        for pnl in pnls
    )

    gross_profit = sum(
        pnl
        for pnl in pnls
        if pnl > 0
    )

    gross_loss = abs(
        sum(
            pnl
            for pnl in pnls
            if pnl < 0
        )
    )

    reasons = Counter(
        str(
            row.get(
                "exit_reason",
                "",
            )
        )
        for row in rows
    )

    spec = VARIANTS[variant]

    return {
        "wait_minutes": int(
            spec["wait_minutes"]
        ),
        "exit_min_profit_eur": float(
            spec["exit_min_profit_eur"]
        ),
        "trail_lock_min_eur": float(
            spec["trail_lock_min_eur"]
        ),
        "closed": len(rows),
        "wins": wins,
        "losses": losses,
        "winrate_pct": round(
            (
                wins
                / len(rows)
                * 100.0
            )
            if rows
            else 0.0,
            2,
        ),
        "net_pnl_eur": round(
            sum(pnls),
            6,
        ),
        "average_pnl_eur": round(
            (
                sum(pnls)
                / len(rows)
            )
            if rows
            else 0.0,
            6,
        ),
        "profit_factor": (
            round(
                gross_profit
                / gross_loss,
                4,
            )
            if gross_loss > 0
            else None
        ),
        "exit_reasons": dict(
            reasons
        ),
    }


def build_report(
    state: Dict[str, Any],
    baseline: Dict[str, Any],
) -> Dict[str, Any]:
    signals = state.get("signals") or {}
    positions = state.get("positions") or {}
    closed = state.get("closed") or []

    pending_by_variant = Counter()
    open_by_variant = Counter()

    for position in positions.values():
        if not isinstance(
            position,
            dict,
        ):
            continue

        variant = str(
            position.get(
                "variant",
                "",
            )
        )

        status = str(
            position.get(
                "status",
                "",
            )
        )

        if status == "PENDING_ENTRY":
            pending_by_variant[
                variant
            ] += 1

        elif status == "OPEN":
            open_by_variant[
                variant
            ] += 1

    per_variant = {}

    for variant in VARIANTS:
        per_variant[variant] = {
            **summary_for_variant(
                closed,
                variant,
            ),
            "open": open_by_variant[
                variant
            ],
            "pending_entry": (
                pending_by_variant[
                    variant
                ]
            ),
        }

    current = per_variant["CURRENT"]

    comparisons = {}

    for variant in [
        "WAIT30_100",
        "WAIT30_050",
    ]:
        row = per_variant[variant]

        comparisons[variant] = {
            "delta_net_pnl_vs_current_eur": round(
                to_float(
                    row.get(
                        "net_pnl_eur"
                    ),
                    0.0,
                )
                - to_float(
                    current.get(
                        "net_pnl_eur"
                    ),
                    0.0,
                ),
                6,
            ),
            "delta_wins_vs_current": (
                int(
                    row.get(
                        "wins",
                        0,
                    )
                )
                - int(
                    current.get(
                        "wins",
                        0,
                    )
                )
            ),
            "delta_losses_vs_current": (
                int(
                    row.get(
                        "losses",
                        0,
                    )
                )
                - int(
                    current.get(
                        "losses",
                        0,
                    )
                )
            ),
        }

    wait100 = per_variant[
        "WAIT30_100"
    ]
    wait050 = per_variant[
        "WAIT30_050"
    ]

    comparisons[
        "WAIT30_050_vs_WAIT30_100"
    ] = {
        "delta_net_pnl_eur": round(
            to_float(
                wait050.get(
                    "net_pnl_eur"
                ),
                0.0,
            )
            - to_float(
                wait100.get(
                    "net_pnl_eur"
                ),
                0.0,
            ),
            6,
        ),
        "delta_wins": (
            int(
                wait050.get(
                    "wins",
                    0,
                )
            )
            - int(
                wait100.get(
                    "wins",
                    0,
                )
            )
        ),
        "delta_losses": (
            int(
                wait050.get(
                    "losses",
                    0,
                )
            )
            - int(
                wait100.get(
                    "losses",
                    0,
                )
            )
        ),
    }

    signal_count = len(signals)

    return {
        "version": VERSION,
        "mode": MODE,
        "generated_at": now_iso(),
        "started_at": baseline["started_at"],
        "target_signals": TARGET_SIGNALS,
        "progress": {
            "signals_detected": signal_count,
            "target_signals": TARGET_SIGNALS,
            "target_reached": (
                signal_count
                >= TARGET_SIGNALS
            ),
            "progress_pct": round(
                min(
                    100.0,
                    signal_count
                    / TARGET_SIGNALS
                    * 100.0,
                ),
                2,
            ),
        },
        "variants": per_variant,
        "comparisons": comparisons,
        "settings": state.get(
            "settings"
        ),
        "rules": {
            "CURRENT": (
                "entry direct; exit-min €1.00; trail-lock €1.00"
            ),
            "WAIT30_100": (
                "entry +30m; exit-min €1.00; trail-lock €1.00"
            ),
            "WAIT30_050": (
                "entry +30m; exit-min €0.50; trail-lock €1.00"
            ),
            "same_signal_for_all_variants": True,
            "max_hold_minutes": MAX_HOLD_MINUTES,
        },
        "safety": SAFETY,
        "errors": (
            state.get(
                "errors",
                [],
            )[-10:]
        ),
    }


def print_report(
    report: Dict[str, Any],
) -> None:
    progress = (
        report.get(
            "progress"
        )
        or {}
    )

    variants = (
        report.get(
            "variants"
        )
        or {}
    )

    comparisons = (
        report.get(
            "comparisons"
        )
        or {}
    )

    print("=" * 78)
    print(
        " DIAMOND TRADER LONG COMBO SHADOW LAB"
    )
    print("=" * 78)

    print(
        f"Versie                 : {report.get('version', '-')}"
    )
    print(
        f"Modus                  : {report.get('mode', '-')}"
    )
    print(
        f"Gestart                : {report.get('started_at', '-')}"
    )
    print(
        f"Laatste update         : {report.get('generated_at', '-')}"
    )
    print(
        "Nieuwe LONG-signalen   : "
        f"{progress.get('signals_detected', 0)}/"
        f"{progress.get('target_signals', TARGET_SIGNALS)}"
    )
    print(
        f"Voortgang              : "
        f"{to_float(progress.get('progress_pct'), 0.0):.1f}%"
    )

    print()
    print("VARIANTEN")
    print("-" * 78)

    for variant in VARIANTS:
        row = (
            variants.get(
                variant
            )
            or {}
        )

        print(
            f"{variant:12s} "
            f"wait={int(row.get('wait_minutes', 0)):2d}m "
            f"exit=€{to_float(row.get('exit_min_profit_eur'), 0.0):.2f} "
            f"lock=€{to_float(row.get('trail_lock_min_eur'), 0.0):.2f} "
            f"closed={int(row.get('closed', 0)):2d} "
            f"wins={int(row.get('wins', 0)):2d} "
            f"losses={int(row.get('losses', 0)):2d} "
            f"open={int(row.get('open', 0)):2d} "
            f"pending={int(row.get('pending_entry', 0)):2d} "
            f"pnl=€{to_float(row.get('net_pnl_eur'), 0.0):+.4f}"
        )

    print()
    print("VERSCHILLEN")
    print("-" * 78)

    for variant in [
        "WAIT30_100",
        "WAIT30_050",
    ]:
        comp = (
            comparisons.get(
                variant
            )
            or {}
        )

        print(
            f"{variant:12s} vs CURRENT "
            f"delta_pnl=€{to_float(comp.get('delta_net_pnl_vs_current_eur'), 0.0):+.4f} "
            f"delta_wins={int(comp.get('delta_wins_vs_current', 0)):+d} "
            f"delta_losses={int(comp.get('delta_losses_vs_current', 0)):+d}"
        )

    comp = (
        comparisons.get(
            "WAIT30_050_vs_WAIT30_100"
        )
        or {}
    )

    print(
        f"{'WAIT30_050':12s} vs WAIT30_100 "
        f"delta_pnl=€{to_float(comp.get('delta_net_pnl_eur'), 0.0):+.4f} "
        f"delta_wins={int(comp.get('delta_wins', 0)):+d} "
        f"delta_losses={int(comp.get('delta_losses', 0)):+d}"
    )

    print()
    print("VEILIGHEID")
    print("-" * 78)
    print("Orders mogelijk         : NEE")
    print("Private API             : NEE")
    print("Bot-state gewijzigd     : NEE")
    print("Config gewijzigd        : NEE")
    print("Transacties gewijzigd   : NEE")


def run_update() -> Dict[str, Any]:
    settings = load_settings()

    if settings["timeframe"] != TIMEFRAME:
        raise ValueError(
            "LONG Combo Shadow verwacht timeframe 15m"
        )

    baseline = ensure_baseline(
        settings
    )

    baseline_dt = parse_dt(
        baseline.get(
            "started_at"
        )
    )

    if baseline_dt is None:
        raise ValueError(
            "ongeldige baseline started_at"
        )

    state = load_state(
        baseline
    )

    state["settings"] = settings
    state["errors"] = []

    exchange = public_exchange()

    try:
        frames = prepare_frames(
            exchange,
            settings,
            baseline_dt,
        )

        missing = [
            symbol
            for symbol in settings[
                "symbols"
            ]
            if symbol not in frames
        ]

        if missing:
            state["errors"].append(
                "Geen candles voor: "
                + ", ".join(missing)
            )

        ingest_signals(
            exchange,
            state,
            baseline_dt,
            settings,
            frames,
        )

        update_positions(
            state,
            settings,
            frames,
        )

    except Exception as exc:
        state["errors"].append(
            f"{type(exc).__name__}: {exc}"
        )
        raise

    finally:
        state["last_update_at"] = now_iso()

        save_json_atomic(
            STATE_FILE,
            state,
        )

    report = build_report(
        state,
        baseline,
    )

    save_json_atomic(
        REPORT_FILE,
        report,
    )

    return report


def self_test() -> None:
    settings = {
        "fee_pct": 0.25,
        "stake_eur": 120.0,
        "use_sma": True,
        "require_crossover": True,
        "use_rsi": True,
        "rsi_min": 55.0,
        "rsi_max": 70.0,
        "use_atr": True,
        "min_atr_pct": 0.30,
        "atr_sl_mult": 1.2,
        "atr_tp_mult": 2.6,
        "hard_sl_pct": 3.0,
        "trailing_enabled": True,
        "trailing_atr_mult": 1.2,
        "profit_trigger_pct": 1.0,
        "profit_pullback_pct": 0.5,
        "exit_on_trend_break": True,
    }

    prev = pd.Series(
        {
            "sma_fast": 99.0,
            "sma_slow": 100.0,
            "close": 100.0,
            "rsi": 60.0,
            "atr": 1.0,
            "atr_pct": 1.0,
        }
    )

    good = pd.Series(
        {
            "sma_fast": 101.0,
            "sma_slow": 100.0,
            "close": 102.0,
            "rsi": 60.0,
            "atr": 1.0,
            "atr_pct": 0.98,
        }
    )

    assert is_long_signal(
        prev,
        good,
        settings,
    )

    assert (
        VARIANTS["CURRENT"]["wait_minutes"]
        == 0
    )

    assert (
        VARIANTS["WAIT30_100"]["wait_minutes"]
        == 30
    )

    assert (
        VARIANTS["WAIT30_050"]["wait_minutes"]
        == 30
    )

    assert (
        VARIANTS["CURRENT"][
            "exit_min_profit_eur"
        ]
        == 1.00
    )

    assert (
        VARIANTS["WAIT30_100"][
            "exit_min_profit_eur"
        ]
        == 1.00
    )

    assert (
        VARIANTS["WAIT30_050"][
            "exit_min_profit_eur"
        ]
        == 0.50
    )

    assert (
        VARIANTS["WAIT30_050"][
            "trail_lock_min_eur"
        ]
        == 1.00
    )

    base_position = {
        "amount": 1.0,
        "quote_amount": 100.0,
        "buy_fee": 0.25,
    }

    p050 = minimum_profitable_exit_price(
        base_position,
        settings,
        0.50,
    )

    p100 = minimum_profitable_exit_price(
        base_position,
        settings,
        1.00,
    )

    assert p100 > p050

    assert SAFETY["orders_possible"] is False
    assert SAFETY["private_exchange_calls"] is False
    assert SAFETY["api_keys_loaded"] is False
    assert SAFETY["config_write"] is False
    assert SAFETY["bot_state_write"] is False

    print(
        "LONG_COMBO_SHADOW_SELF_TEST_OK"
    )
    print(
        "CURRENT      : entry direct | exit €1.00 | trail-lock €1.00"
    )
    print(
        "WAIT30_100   : entry +30m   | exit €1.00 | trail-lock €1.00"
    )
    print(
        "WAIT30_050   : entry +30m   | exit €0.50 | trail-lock €1.00"
    )
    print(
        "Alle overige LONG-regels: ONGEWIJZIGD"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Diamond Trader LONG Combo Shadow Lab"
        )
    )

    parser.add_argument(
        "--update",
        action="store_true",
        help="Verwerk nieuwe LONG-signalen.",
    )

    parser.add_argument(
        "--status",
        action="store_true",
        help=(
            "Toon opgeslagen rapport zonder netwerk."
        ),
    )

    parser.add_argument(
        "--self-test",
        action="store_true",
        help=(
            "Interne test zonder netwerk of /var/data-write."
        ),
    )

    parser.add_argument(
        "--no-print",
        action="store_true",
        help=(
            "Alleen compacte update-uitvoer."
        ),
    )

    args = parser.parse_args()

    if args.self_test:
        self_test()
        return

    if args.status:
        report = load_json(
            REPORT_FILE,
            {},
        )

        if not report:
            print(
                "Nog geen LONG Combo Shadow rapport. "
                "Voer eerst --update uit."
            )
            return

        print_report(report)
        return

    report = run_update()

    if args.no_print:
        progress = (
            report.get(
                "progress"
            )
            or {}
        )

        variants = (
            report.get(
                "variants"
            )
            or {}
        )

        print(
            "LONG Combo Shadow bijgewerkt | "
            f"signals={progress.get('signals_detected', 0)}/"
            f"{TARGET_SIGNALS} | "
            f"CURRENT_closed="
            f"{(variants.get('CURRENT') or {}).get('closed', 0)} | "
            f"WAIT30_100_closed="
            f"{(variants.get('WAIT30_100') or {}).get('closed', 0)} | "
            f"WAIT30_050_closed="
            f"{(variants.get('WAIT30_050') or {}).get('closed', 0)}"
        )

    else:
        print_report(report)


if __name__ == "__main__":
    main()
