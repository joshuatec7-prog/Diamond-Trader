#!/usr/bin/env python3
"""
Diamond Market Scanner v1.1

Veilige TA-schaduwscanner voor Diamond Trader.

Doet wel:
- haalt actieve Bitvavo EUR-spotmarkten op;
- filtert op volume en spread;
- analyseert afgesloten candles op 15m, 1u en 4u;
- bepaalt bullish, bearish of neutraal marktregime;
- zoekt trend-breakout, momentum, pullback/retest,
  range-breakout en mean-reversion;
- berekent verwachte opbrengst na kosten voor €120, €125, €130 en €135;
- schrijft signalen naar eigen bestanden in /var/data;
- opent en sluit uitsluitend virtuele schaduwposities;
- gebruikt score, spread, netto-opbrengst en risico/winst als harde filters.

Doet nooit:
- orders plaatsen;
- diamond_state.json wijzigen;
- diamond_transactions.csv wijzigen;
- bestaande long- of shorttests beïnvloeden.

Gebruik:
    python3 market_scanner.py --self-test
    python3 market_scanner.py
    python3 market_scanner.py --loop

Schaduwbestanden:
    /var/data/diamond_market_signals.json
    /var/data/diamond_market_signals.csv
    /var/data/diamond_market_scanner_state.json
    /var/data/diamond_shadow_trades.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import logging.handlers
import math
import os
import random
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

import ccxt
import pandas as pd
import yaml
from dotenv import load_dotenv


VERSION = "1.1"

load_dotenv()

LOG = logging.getLogger("diamond_market_scanner")

CFG_FILE = os.getenv(
    "CFG_FILE",
    "/opt/render/project/src/config.yaml",
).strip()

DATA_DIR = Path(
    os.getenv("DIAMOND_DATA_DIR", "/var/data").strip()
)

REPORT_FILE = DATA_DIR / "diamond_market_signals.json"
STATE_FILE = DATA_DIR / "diamond_market_scanner_state.json"
SIGNALS_CSV_FILE = DATA_DIR / "diamond_market_signals.csv"
SIGNAL_MEASUREMENTS_FILE = DATA_DIR / "diamond_signal_measurements.jsonl"
SHADOW_TRADES_FILE = DATA_DIR / "diamond_shadow_trades.csv"
LOG_FILE = DATA_DIR / "diamond_market_scanner.log"

API_MAX_ATTEMPTS = 3
API_RETRY_DELAYS = (2.0, 5.0)

T = TypeVar("T")

TRANSIENT_CCXT_ERRORS = (
    ccxt.NetworkError,
    ccxt.RequestTimeout,
    ccxt.ExchangeNotAvailable,
    ccxt.DDoSProtection,
    ccxt.RateLimitExceeded,
)

CSV_HEADER = [
    "detected_at",
    "candle_timestamp",
    "symbol",
    "strategy",
    "side",
    "market_regime",
    "regime_strength",
    "score",
    "entry_price",
    "take_profit",
    "stop_loss",
    "rsi",
    "atr_pct",
    "volume_ratio",
    "spread_pct",
    "quote_volume",
    "change_pct_24h",
    "expected_net_pct",
    "risk_net_pct",
    "reward_risk",
    "expected_profit_eur",
    "expected_loss_eur",
    "expected_eur_120",
    "expected_eur_125",
    "expected_eur_130",
    "expected_eur_135",
    "shadow_eligible",
    "shadow_rejection_reasons",
    "reasons",
]

SHADOW_TRADE_HEADER = [
    "opened_at",
    "closed_at",
    "symbol",
    "strategy",
    "side",
    "market_regime",
    "signal_score",
    "entry_price",
    "exit_price",
    "stake_eur",
    "amount",
    "entry_fee_eur",
    "exit_fee_eur",
    "total_fees_eur",
    "entry_spread_pct",
    "exit_spread_pct",
    "atr_pct",
    "take_profit",
    "stop_loss",
    "exit_reason",
    "gross_pnl_eur",
    "net_pnl_eur",
    "return_pct",
    "duration_minutes",
    "entry_candle_timestamp_ms",
    "exit_candle_timestamp_ms",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def get_cfg(
    config: Dict[str, Any],
    path: str,
    default: Any = None,
) -> Any:
    current: Any = config

    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]

    return current


def setup_logging(verbose: bool) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    level = logging.DEBUG if verbose else logging.INFO
    LOG.setLevel(level)
    LOG.handlers.clear()
    LOG.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(formatter)

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    LOG.addHandler(console)
    LOG.addHandler(file_handler)


def load_yaml(path_str: str) -> Dict[str, Any]:
    path = Path(path_str)

    if not path.exists():
        raise FileNotFoundError(
            f"Configuratiebestand ontbreekt: {path}"
        )

    with path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}

    if not isinstance(data, dict):
        raise ValueError("config.yaml bevat geen geldige structuur")

    return data


def load_json(
    path: Path,
    default: Dict[str, Any],
) -> Dict[str, Any]:
    if not path.exists():
        return default.copy()

    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)

        if isinstance(data, dict):
            return data

    except Exception as exc:
        LOG.warning("JSON lezen mislukt | %s | %s", path, exc)

    return default.copy()


def save_json_atomic(
    path: Path,
    data: Dict[str, Any],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
    ) as temporary:
        json.dump(
            data,
            temporary,
            indent=2,
            ensure_ascii=False,
        )
        temporary_name = temporary.name

    os.replace(temporary_name, path)


def ensure_csv_schema(
    path: Path,
    expected_header: List[str],
) -> bool:
    """Bewaar een oud CSV-schema en start veilig met de nieuwe header."""
    if not path.exists() or path.stat().st_size == 0:
        return True

    try:
        with path.open("r", encoding="utf-8", newline="") as file:
            current_header = next(csv.reader(file), [])
    except Exception as exc:
        LOG.warning("CSV-header lezen mislukt | %s | %s", path, exc)
        current_header = []

    if current_header == expected_header:
        return False

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(
        f"{path.stem}_schema_backup_{timestamp}{path.suffix}"
    )
    os.replace(path, backup)
    LOG.info(
        "Oud CSV-schema veilig bewaard | oud=%s | backup=%s",
        path,
        backup,
    )
    return True

def default_state() -> Dict[str, Any]:
    return {
        "version": VERSION,
        "started_at": now_iso(),
        "last_scan_at": None,
        "scan_count": 0,
        "seen_signal_keys": [],
        "total_unique_signals": 0,
        "open_positions": {},
        "shadow_totals": {
            "opened": 0,
            "closed": 0,
            "wins": 0,
            "losses": 0,
            "neutral": 0,
            "net_pnl_eur": 0.0,
            "total_fees_eur": 0.0,
        },
    }



def normalize_state(state: Dict[str, Any]) -> Dict[str, Any]:
    defaults = default_state()

    for key, value in defaults.items():
        state.setdefault(key, value)

    if not isinstance(state.get("seen_signal_keys"), list):
        state["seen_signal_keys"] = []

    if not isinstance(state.get("open_positions"), dict):
        state["open_positions"] = {}

    if not isinstance(state.get("shadow_totals"), dict):
        state["shadow_totals"] = defaults["shadow_totals"].copy()

    for key, value in defaults["shadow_totals"].items():
        state["shadow_totals"].setdefault(key, value)

    state["version"] = VERSION
    return state



def settings(
    config: Dict[str, Any],
    top_override: Optional[int],
) -> Dict[str, Any]:
    raw_excluded = get_cfg(
        config,
        "market_scanner.exclude_bases",
        get_cfg(
            config,
            "scanner.exclude_bases",
            ["EUR", "USDT", "USDC", "DAI", "TUSD", "FDUSD"],
        ),
    )

    if not isinstance(raw_excluded, list):
        raw_excluded = []

    configured_top = to_int(
        get_cfg(
            config,
            "market_scanner.top_n_markets",
            get_cfg(config, "scanner.top_n_markets", 20),
        ),
        20,
    )

    return {
        "quote": str(
            get_cfg(config, "quote", "EUR")
        ).strip().upper(),
        "top_n": max(
            1,
            min(100, top_override or configured_top),
        ),
        "min_quote_volume": max(
            0.0,
            to_float(
                get_cfg(
                    config,
                    "market_scanner.min_quote_volume",
                    get_cfg(config, "scanner.min_quote_volume", 250_000),
                ),
                250_000,
            ),
        ),
        # Brede scanfilter. Een schaduwtrade heeft daarnaast de strengere
        # trade_max_spread_pct.
        "max_spread_pct": max(
            0.001,
            to_float(
                get_cfg(
                    config,
                    "market_scanner.max_spread_pct",
                    get_cfg(config, "risk.max_spread_pct", 0.25),
                ),
                0.25,
            ),
        ),
        "trade_max_spread_pct": max(
            0.001,
            to_float(
                get_cfg(
                    config,
                    "market_scanner.trade_max_spread_pct",
                    0.10,
                ),
                0.10,
            ),
        ),
        "exclude_bases": {
            str(item).strip().upper()
            for item in raw_excluded
            if str(item).strip()
        },
        "candles_limit": max(
            100,
            min(
                500,
                to_int(
                    get_cfg(config, "market_scanner.candles_limit", 240),
                    240,
                ),
            ),
        ),
        "loop_sleep_seconds": max(
            300,
            to_int(
                get_cfg(config, "market_scanner.loop_sleep_seconds", 900),
                900,
            ),
        ),
        "ema_fast": max(
            2,
            to_int(get_cfg(config, "market_scanner.ema_fast", 20), 20),
        ),
        "ema_slow": max(
            5,
            to_int(get_cfg(config, "market_scanner.ema_slow", 50), 50),
        ),
        "rsi_len": max(
            2,
            to_int(get_cfg(config, "market_scanner.rsi_len", 14), 14),
        ),
        "atr_len": max(
            2,
            to_int(get_cfg(config, "market_scanner.atr_len", 14), 14),
        ),
        "breakout_lookback": max(
            5,
            to_int(
                get_cfg(config, "market_scanner.breakout_lookback", 20),
                20,
            ),
        ),
        "min_atr_pct": max(
            0.0,
            to_float(
                get_cfg(config, "market_scanner.min_atr_pct", 0.20),
                0.20,
            ),
        ),
        "min_signal_score": max(
            0.0,
            min(
                100.0,
                to_float(
                    get_cfg(config, "market_scanner.min_signal_score", 70),
                    70,
                ),
            ),
        ),
        "min_volume_ratio": max(
            0.0,
            to_float(
                get_cfg(config, "market_scanner.min_volume_ratio", 1.10),
                1.10,
            ),
        ),
        "fee_pct_per_side": max(
            0.0,
            to_float(
                get_cfg(
                    config,
                    "market_scanner.fee_pct_per_side",
                    get_cfg(config, "fees.taker_fee_pct", 0.25),
                ),
                0.25,
            ),
        ),
        "atr_tp_mult": max(
            0.1,
            to_float(
                get_cfg(config, "market_scanner.atr_tp_mult", 2.6),
                2.6,
            ),
        ),
        "atr_sl_mult": max(
            0.1,
            to_float(
                get_cfg(config, "market_scanner.atr_sl_mult", 1.2),
                1.2,
            ),
        ),
        "min_expected_net_pct": to_float(
            get_cfg(config, "market_scanner.min_expected_net_pct", 0.10),
            0.10,
        ),
        "stake_eur": max(
            5.0,
            to_float(
                get_cfg(
                    config,
                    "market_scanner.stake_eur",
                    get_cfg(config, "risk.fixed_stake_quote", 120),
                ),
                120,
            ),
        ),
        "min_expected_profit_eur": max(
            0.0,
            to_float(
                get_cfg(
                    config,
                    "market_scanner.min_expected_profit_eur",
                    1.00,
                ),
                1.00,
            ),
        ),
        "min_reward_risk": max(
            0.0,
            to_float(
                get_cfg(config, "market_scanner.min_reward_risk", 1.20),
                1.20,
            ),
        ),
        "max_extension_atr": max(
            0.5,
            to_float(
                get_cfg(config, "market_scanner.max_extension_atr", 3.0),
                3.0,
            ),
        ),
        "max_shadow_positions": max(
            1,
            to_int(
                get_cfg(config, "market_scanner.max_shadow_positions", 5),
                5,
            ),
        ),
        "max_hold_minutes": max(
            60,
            to_int(
                get_cfg(config, "market_scanner.max_hold_minutes", 2880),
                2880,
            ),
        ),
    }



def create_exchange() -> ccxt.Exchange:
    exchange = ccxt.bitvavo({
        "apiKey": os.getenv("BITVAVO_API_KEY", "").strip(),
        "secret": os.getenv("BITVAVO_API_SECRET", "").strip(),
        "enableRateLimit": True,
        "timeout": 30_000,
        "options": {
            "fetchMarkets": {
                "types": ["spot"],
            },
        },
    })

    exchange.load_markets()

    if not exchange.has.get("fetchTickers"):
        raise RuntimeError("fetch_tickers wordt niet ondersteund")

    if not exchange.has.get("fetchOHLCV"):
        raise RuntimeError("fetch_ohlcv wordt niet ondersteund")

    return exchange


def api_call(
    description: str,
    call: Callable[[], T],
) -> T:
    last_error: Optional[Exception] = None

    for attempt in range(1, API_MAX_ATTEMPTS + 1):
        try:
            return call()

        except TRANSIENT_CCXT_ERRORS as exc:
            last_error = exc

            if attempt >= API_MAX_ATTEMPTS:
                break

            delay = (
                API_RETRY_DELAYS[
                    min(attempt - 1, len(API_RETRY_DELAYS) - 1)
                ]
                + random.uniform(0.0, 0.5)
            )

            LOG.warning(
                "%s mislukt | poging=%d/%d | opnieuw over %.1fs",
                description,
                attempt,
                API_MAX_ATTEMPTS,
                delay,
            )
            time.sleep(delay)

        except ccxt.ExchangeError:
            raise

    if last_error is not None:
        raise last_error

    raise RuntimeError(f"{description} mislukt")


def spread_pct(ticker: Dict[str, Any]) -> float:
    bid = to_float(ticker.get("bid"), 0.0)
    ask = to_float(ticker.get("ask"), 0.0)

    if bid <= 0 or ask <= 0:
        return 999.0

    middle = (bid + ask) / 2.0

    return (
        (ask - bid) / middle * 100.0
        if middle > 0
        else 999.0
    )


def quote_volume(ticker: Dict[str, Any]) -> float:
    direct = to_float(ticker.get("quoteVolume"), 0.0)

    if direct > 0:
        return direct

    base_volume = to_float(ticker.get("baseVolume"), 0.0)
    last = to_float(ticker.get("last"), 0.0)

    return base_volume * last if base_volume > 0 and last > 0 else 0.0


def leveraged_token(base: str) -> bool:
    suffixes = (
        "3L", "3S", "5L", "5S",
        "UP", "DOWN", "BULL", "BEAR",
    )
    return any(base.endswith(suffix) for suffix in suffixes)


def select_markets(
    exchange: ccxt.Exchange,
    tickers: Dict[str, Any],
    cfg: Dict[str, Any],
    rotation_index: int = 0,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    Hybride marktselectie.

    Eerst blijven alle bestaande liquiditeits- en spreadfilters gelden.
    Uit de geschikte markt worden daarna geselecteerd:
    - 50% sterkste 24h movers;
    - 25% hoogste quote-volume;
    - resterende plaatsen rouleren door de rest.

    Zo blijft het aantal zware OHLC-analyses begrensd terwijl snelle
    marktbewegingen niet meer uitsluitend door een volume-toplijst
    gemist kunnen worden.
    """
    eligible: List[Dict[str, Any]] = []

    counts = {
        "all_markets": 0,
        "eur_spot_active": 0,
        "excluded": 0,
        "volume_blocked": 0,
        "spread_blocked": 0,
        "invalid_ticker": 0,
        "eligible": 0,
        "selected_movers": 0,
        "selected_volume": 0,
        "selected_rotation": 0,
    }

    for symbol, market in exchange.markets.items():
        counts["all_markets"] += 1

        if not isinstance(market, dict):
            continue

        if str(market.get("quote") or "").upper() != cfg["quote"]:
            continue

        if market.get("spot") is False or market.get("active") is False:
            continue

        counts["eur_spot_active"] += 1

        base = str(market.get("base") or "").upper()

        if (
            not base
            or base in cfg["exclude_bases"]
            or leveraged_token(base)
        ):
            counts["excluded"] += 1
            continue

        ticker = tickers.get(symbol)

        if not isinstance(ticker, dict):
            counts["invalid_ticker"] += 1
            continue

        last = to_float(ticker.get("last"), 0.0)
        volume = quote_volume(ticker)
        spread = spread_pct(ticker)

        if last <= 0:
            counts["invalid_ticker"] += 1
            continue

        if volume < cfg["min_quote_volume"]:
            counts["volume_blocked"] += 1
            continue

        if spread > cfg["max_spread_pct"]:
            counts["spread_blocked"] += 1
            continue

        eligible.append({
            "symbol": symbol,
            "base": base,
            "last": last,
            "bid": to_float(ticker.get("bid"), 0.0),
            "ask": to_float(ticker.get("ask"), 0.0),
            "quote_volume": volume,
            "spread_pct": spread,
            "change_pct_24h": to_float(
                ticker.get("percentage"),
                0.0,
            ),
        })

    counts["eligible"] = len(eligible)

    if not eligible:
        return [], counts

    top_n = min(int(cfg["top_n"]), len(eligible))

    mover_quota = max(1, top_n // 2)
    volume_quota = max(1, top_n // 4)

    selected: List[Dict[str, Any]] = []
    selected_symbols = set()

    def add_unique(items, maximum, reason):
        added = 0

        for item in items:
            if added >= maximum:
                break

            symbol = item["symbol"]

            if symbol in selected_symbols:
                continue

            row = dict(item)
            row["selection_reason"] = reason
            selected.append(row)
            selected_symbols.add(symbol)
            added += 1

        return added

    # Zowel sterke stijging als sterke daling is marktbeweging.
    movers = sorted(
        eligible,
        key=lambda item: abs(item["change_pct_24h"]),
        reverse=True,
    )

    counts["selected_movers"] = add_unique(
        movers,
        mover_quota,
        "MOVER_24H",
    )

    volume_ranked = sorted(
        eligible,
        key=lambda item: item["quote_volume"],
        reverse=True,
    )

    counts["selected_volume"] = add_unique(
        volume_ranked,
        volume_quota,
        "HIGH_VOLUME",
    )

    remaining_slots = top_n - len(selected)

    if remaining_slots > 0:
        rotation_pool = sorted(
            (
                item
                for item in eligible
                if item["symbol"] not in selected_symbols
            ),
            key=lambda item: item["symbol"],
        )

        if rotation_pool:
            start = (
                max(0, int(rotation_index))
                * remaining_slots
            ) % len(rotation_pool)

            rotated = (
                rotation_pool[start:]
                + rotation_pool[:start]
            )

            counts["selected_rotation"] = add_unique(
                rotated,
                remaining_slots,
                "ROTATION",
            )

    # Veiligheidsfill als overlap/pool ooit onvoldoende is.
    if len(selected) < top_n:
        add_unique(
            volume_ranked,
            top_n - len(selected),
            "FILL",
        )

    return selected[:top_n], counts

def rsi(series: pd.Series, length: int) -> pd.Series:
    difference = series.diff()
    gains = difference.clip(lower=0)
    losses = -difference.clip(upper=0)

    avg_gain = gains.ewm(
        alpha=1 / length,
        adjust=False,
        min_periods=length,
    ).mean()

    avg_loss = losses.ewm(
        alpha=1 / length,
        adjust=False,
        min_periods=length,
    ).mean()

    strength = avg_gain / avg_loss.replace(0, float("nan"))
    result = 100 - (100 / (1 + strength))

    return result.fillna(50.0)


def atr(dataframe: pd.DataFrame, length: int) -> pd.Series:
    previous_close = dataframe["close"].shift(1)

    true_range = pd.concat(
        [
            dataframe["high"] - dataframe["low"],
            (dataframe["high"] - previous_close).abs(),
            (dataframe["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    return true_range.ewm(
        alpha=1 / length,
        adjust=False,
        min_periods=length,
    ).mean()


def closed_candles(
    exchange: ccxt.Exchange,
    symbol: str,
    timeframe: str,
    limit: int,
) -> pd.DataFrame:
    rows = api_call(
        f"{timeframe}-candles {symbol}",
        lambda: exchange.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            limit=limit,
        ),
    )

    dataframe = pd.DataFrame(
        rows,
        columns=[
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ],
    )

    for column in dataframe.columns:
        dataframe[column] = pd.to_numeric(
            dataframe[column],
            errors="coerce",
        )

    dataframe.dropna(inplace=True)
    dataframe.sort_values("timestamp", inplace=True)
    dataframe.drop_duplicates(
        subset=["timestamp"],
        keep="last",
        inplace=True,
    )
    dataframe.reset_index(drop=True, inplace=True)

    if dataframe.empty:
        raise RuntimeError("geen candledata")

    timeframe_ms = int(
        exchange.parse_timeframe(timeframe) * 1000
    )

    if (
        int(dataframe.iloc[-1]["timestamp"]) + timeframe_ms
        > int(exchange.milliseconds())
    ):
        dataframe = dataframe.iloc[:-1].copy()

    if dataframe.empty:
        raise RuntimeError("geen afgesloten candles")

    dataframe["timestamp_iso"] = pd.to_datetime(
        dataframe["timestamp"],
        unit="ms",
        utc=True,
    )

    return dataframe.reset_index(drop=True)


def snapshot(
    dataframe: pd.DataFrame,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    data = dataframe.copy()

    data["ema_fast"] = data["close"].ewm(
        span=cfg["ema_fast"],
        adjust=False,
        min_periods=cfg["ema_fast"],
    ).mean()

    data["ema_slow"] = data["close"].ewm(
        span=cfg["ema_slow"],
        adjust=False,
        min_periods=cfg["ema_slow"],
    ).mean()

    data["rsi"] = rsi(data["close"], cfg["rsi_len"])
    data["atr"] = atr(data, cfg["atr_len"])

    data["volume_average"] = data["volume"].rolling(20).mean()
    data["volume_ratio"] = (
        data["volume"]
        / data["volume_average"].replace(0, float("nan"))
    )

    data["bb_middle"] = data["close"].rolling(20).mean()
    data["bb_std"] = data["close"].rolling(20).std(ddof=0)
    data["bb_upper"] = data["bb_middle"] + 2 * data["bb_std"]
    data["bb_lower"] = data["bb_middle"] - 2 * data["bb_std"]

    required = max(
        cfg["ema_slow"] + 3,
        cfg["breakout_lookback"] + 3,
        cfg["rsi_len"] + 3,
        cfg["atr_len"] + 3,
    )

    if len(data) < required:
        raise RuntimeError(
            f"te weinig candles: {len(data)}, minimaal {required}"
        )

    latest = data.iloc[-1]
    previous = data.iloc[-2]

    prior = data.iloc[
        -(cfg["breakout_lookback"] + 1):-1
    ]

    close = to_float(latest["close"], 0.0)
    current_atr = to_float(latest["atr"], 0.0)
    current_ema = to_float(latest["ema_fast"], 0.0)

    earlier_close = to_float(data.iloc[-5]["close"], close)
    momentum = (
        (close / earlier_close - 1) * 100
        if earlier_close > 0
        else 0.0
    )

    return {
        "timestamp": str(latest["timestamp_iso"]),
        "timestamp_ms": int(latest["timestamp"]),
        "open": to_float(latest["open"]),
        "high": to_float(latest["high"]),
        "low": to_float(latest["low"]),
        "close": close,
        "previous_close": to_float(previous["close"]),
        "ema_fast": current_ema,
        "ema_slow": to_float(latest["ema_slow"]),
        "previous_ema_fast": to_float(previous["ema_fast"]),
        "rsi": to_float(latest["rsi"], 50.0),
        "atr": current_atr,
        "atr_pct": (
            current_atr / close * 100
            if close > 0
            else 0.0
        ),
        "volume_ratio": to_float(latest["volume_ratio"]),
        "bb_upper": to_float(latest["bb_upper"]),
        "bb_lower": to_float(latest["bb_lower"]),
        "recent_high": to_float(prior["high"].max()),
        "recent_low": to_float(prior["low"].min()),
        "breakout_up": (
            close > to_float(prior["high"].max())
        ),
        "breakout_down": (
            close < to_float(prior["low"].min())
        ),
        "ema_rising": (
            current_ema > to_float(previous["ema_fast"])
        ),
        "ema_falling": (
            current_ema < to_float(previous["ema_fast"])
        ),
        "momentum_4_pct": momentum,
        "extension_atr": (
            (close - current_ema) / current_atr
            if current_atr > 0
            else 0.0
        ),
        "previous_ema_relation": (
            to_float(previous["close"])
            - to_float(previous["ema_fast"])
        ),
    }


def regime(
    one_hour: Dict[str, Any],
    four_hour: Dict[str, Any],
) -> Tuple[str, int]:
    bullish_checks = [
        one_hour["close"] > one_hour["ema_fast"],
        one_hour["ema_fast"] > one_hour["ema_slow"],
        one_hour["ema_rising"],
        four_hour["close"] > four_hour["ema_fast"],
        four_hour["ema_fast"] > four_hour["ema_slow"],
        four_hour["ema_rising"],
    ]

    bearish_checks = [
        one_hour["close"] < one_hour["ema_fast"],
        one_hour["ema_fast"] < one_hour["ema_slow"],
        one_hour["ema_falling"],
        four_hour["close"] < four_hour["ema_fast"],
        four_hour["ema_fast"] < four_hour["ema_slow"],
        four_hour["ema_falling"],
    ]

    bull = sum(bullish_checks)
    bear = sum(bearish_checks)

    if bull == 6:
        return "BULLISH", 100

    if bear == 6:
        return "BEARISH", 100

    if bull >= 5:
        return "BULLISH_WEAK", round(bull / 6 * 100)

    if bear >= 5:
        return "BEARISH_WEAK", round(bear / 6 * 100)

    return "NEUTRAL", round(max(bull, bear) / 6 * 100)


def economics(
    entry: float,
    current_atr: float,
    spread: float,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    tp_pct = (
        current_atr * cfg["atr_tp_mult"] / entry * 100
        if entry > 0
        else 0.0
    )

    sl_pct = (
        current_atr * cfg["atr_sl_mult"] / entry * 100
        if entry > 0
        else 0.0
    )

    costs_pct = cfg["fee_pct_per_side"] * 2 + spread
    expected_net_pct = tp_pct - costs_pct
    risk_net_pct = sl_pct + costs_pct
    stake = cfg["stake_eur"]

    return {
        "tp_distance_pct": round(tp_pct, 4),
        "sl_distance_pct": round(sl_pct, 4),
        "roundtrip_cost_pct": round(costs_pct, 4),
        "expected_net_pct": round(expected_net_pct, 4),
        "risk_net_pct": round(risk_net_pct, 4),
        "reward_risk": round(
            expected_net_pct / risk_net_pct
            if risk_net_pct > 0
            else 0.0,
            3,
        ),
        "expected_profit_eur": round(
            stake * expected_net_pct / 100,
            4,
        ),
        "expected_loss_eur": round(
            stake * risk_net_pct / 100,
            4,
        ),
        "expected_eur": {
            str(test_stake): round(
                test_stake * expected_net_pct / 100,
                4,
            )
            for test_stake in (120, 125, 130, 135)
        },
    }



def signal_score(
    side: str,
    strategy: str,
    market_regime: str,
    regime_strength: int,
    snap: Dict[str, Any],
    market: Dict[str, Any],
    cfg: Dict[str, Any],
) -> float:
    score = 0.0

    volume_multiple = max(
        1.0,
        market["quote_volume"]
        / max(cfg["min_quote_volume"], 1),
    )

    score += min(
        15.0,
        8.0 + math.log10(volume_multiple) * 5.0,
    )

    score += max(
        0.0,
        10.0 * (
            1 - market["spread_pct"] / cfg["max_spread_pct"]
        ),
    )

    if (
        side == "LONG"
        and market_regime.startswith("BULLISH")
    ) or (
        side == "SHORT"
        and market_regime.startswith("BEARISH")
    ):
        score += 20.0 * regime_strength / 100

    if (
        side == "LONG"
        and snap["ema_rising"]
    ) or (
        side == "SHORT"
        and snap["ema_falling"]
    ):
        score += 10.0

    target_rsi = 62 if side == "LONG" else 38
    score += max(
        0.0,
        15.0 - abs(snap["rsi"] - target_rsi) * 0.75,
    )

    score += min(
        10.0,
        snap["atr_pct"]
        / max(cfg["min_atr_pct"], 0.01)
        * 7.0,
    )

    score += min(
        10.0,
        snap["volume_ratio"]
        / max(cfg["min_volume_ratio"], 0.01)
        * 7.0,
    )

    trigger_points = {
        "trend_breakout": 20,
        "range_breakout": 18,
        "pullback_retest": 18,
        "momentum": 16,
        "mean_reversion": 14,
    }

    score += trigger_points[strategy]

    return round(max(0.0, min(100.0, score)), 1)


def shadow_rejection_reasons(
    score: float,
    market: Dict[str, Any],
    calculation: Dict[str, Any],
    cfg: Dict[str, Any],
) -> List[str]:
    reasons: List[str] = []

    if score < cfg["min_signal_score"]:
        reasons.append(
            f"score {score:.1f} lager dan {cfg['min_signal_score']:.1f}"
        )

    if market["spread_pct"] > cfg["trade_max_spread_pct"]:
        reasons.append(
            f"spread {market['spread_pct']:.4f}% hoger dan "
            f"{cfg['trade_max_spread_pct']:.4f}%"
        )

    if calculation["expected_net_pct"] < cfg["min_expected_net_pct"]:
        reasons.append(
            f"nettoverwachting {calculation['expected_net_pct']:.4f}% lager "
            f"dan {cfg['min_expected_net_pct']:.4f}%"
        )

    if calculation["reward_risk"] < cfg["min_reward_risk"]:
        reasons.append(
            f"risico/winst {calculation['reward_risk']:.3f} lager dan "
            f"{cfg['min_reward_risk']:.3f}"
        )

    if calculation["expected_profit_eur"] < cfg["min_expected_profit_eur"]:
        reasons.append(
            f"verwachte winst €{calculation['expected_profit_eur']:.4f} lager "
            f"dan €{cfg['min_expected_profit_eur']:.2f}"
        )

    return reasons

def make_signal(
    market: Dict[str, Any],
    snap: Dict[str, Any],
    strategy: str,
    side: str,
    market_regime: str,
    regime_strength: int,
    reasons: List[str],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    score = signal_score(
        side,
        strategy,
        market_regime,
        regime_strength,
        snap,
        market,
        cfg,
    )

    calculation = economics(
        snap["close"],
        snap["atr"],
        market["spread_pct"],
        cfg,
    )

    if side == "LONG":
        take_profit = snap["close"] + snap["atr"] * cfg["atr_tp_mult"]
        stop_loss = snap["close"] - snap["atr"] * cfg["atr_sl_mult"]
    else:
        take_profit = snap["close"] - snap["atr"] * cfg["atr_tp_mult"]
        stop_loss = snap["close"] + snap["atr"] * cfg["atr_sl_mult"]

    rejection_reasons = shadow_rejection_reasons(
        score,
        market,
        calculation,
        cfg,
    )

    return {
        "signal_key": (
            f"{market['symbol']}|{strategy}|{side}|{snap['timestamp_ms']}"
        ),
        "detected_at": now_iso(),
        "candle_timestamp": snap["timestamp"],
        "candle_timestamp_ms": snap["timestamp_ms"],
        "symbol": market["symbol"],
        "strategy": strategy,
        "side": side,
        "market_regime": market_regime,
        "regime_strength": regime_strength,
        "score": score,
        "score_ok": score >= cfg["min_signal_score"],
        "entry_price": snap["close"],
        "take_profit": take_profit,
        "stop_loss": stop_loss,
        "atr": snap["atr"],
        "rsi": snap["rsi"],
        "atr_pct": snap["atr_pct"],
        "volume_ratio": snap["volume_ratio"],
        "momentum_4_pct": snap["momentum_4_pct"],
        "extension_atr": snap["extension_atr"],
        "spread_pct": market["spread_pct"],
        "quote_volume": market["quote_volume"],
        "change_pct_24h": market["change_pct_24h"],
        "selection_reason": market.get("selection_reason", "UNKNOWN"),
        "economics": calculation,
        "economically_positive": (
            calculation["expected_net_pct"] >= cfg["min_expected_net_pct"]
        ),
        "shadow_eligible": not rejection_reasons,
        "shadow_rejection_reasons": rejection_reasons,
        "reasons": reasons,
    }



def find_signals(
    market: Dict[str, Any],
    snapshots: Dict[str, Dict[str, Any]],
    cfg: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], str, int]:
    fifteen = snapshots["15m"]
    market_regime, strength = regime(
        snapshots["1h"],
        snapshots["4h"],
    )

    signals: List[Dict[str, Any]] = []

    atr_ok = fifteen["atr_pct"] >= cfg["min_atr_pct"]
    volume_ok = (
        fifteen["volume_ratio"] >= cfg["min_volume_ratio"]
    )

    not_extended_long = (
        fifteen["extension_atr"] <= cfg["max_extension_atr"]
    )

    not_extended_short = (
        fifteen["extension_atr"] >= -cfg["max_extension_atr"]
    )

    bullish = market_regime.startswith("BULLISH")
    bearish = market_regime.startswith("BEARISH")

    if (
        bullish
        and fifteen["breakout_up"]
        and fifteen["ema_rising"]
        and 52 <= fifteen["rsi"] <= 76
        and atr_ok
        and volume_ok
        and not_extended_long
    ):
        signals.append(
            make_signal(
                market,
                fifteen,
                "trend_breakout",
                "LONG",
                market_regime,
                strength,
                [
                    "1u en 4u bullish",
                    "15m boven recente weerstand",
                    "EMA stijgt",
                    "RSI, ATR en volume geschikt",
                ],
                cfg,
            )
        )

    if (
        bearish
        and fifteen["breakout_down"]
        and fifteen["ema_falling"]
        and 24 <= fifteen["rsi"] <= 48
        and atr_ok
        and volume_ok
        and not_extended_short
    ):
        signals.append(
            make_signal(
                market,
                fifteen,
                "trend_breakout",
                "SHORT",
                market_regime,
                strength,
                [
                    "1u en 4u bearish",
                    "15m onder recente steun",
                    "EMA daalt",
                    "RSI, ATR en volume geschikt",
                ],
                cfg,
            )
        )

    momentum_threshold = max(
        0.30,
        fifteen["atr_pct"] * 0.75,
    )

    if (
        bullish
        and fifteen["momentum_4_pct"] >= momentum_threshold
        and fifteen["ema_rising"]
        and 55 <= fifteen["rsi"] <= 78
        and atr_ok
        and fifteen["volume_ratio"] >= 1.35
        and not_extended_long
    ):
        signals.append(
            make_signal(
                market,
                fifteen,
                "momentum",
                "LONG",
                market_regime,
                strength,
                [
                    "bullish hoger timeframe",
                    "versnellend 15m-momentum",
                    "verhoogd volume",
                ],
                cfg,
            )
        )

    if (
        bearish
        and fifteen["momentum_4_pct"] <= -momentum_threshold
        and fifteen["ema_falling"]
        and 22 <= fifteen["rsi"] <= 45
        and atr_ok
        and fifteen["volume_ratio"] >= 1.35
        and not_extended_short
    ):
        signals.append(
            make_signal(
                market,
                fifteen,
                "momentum",
                "SHORT",
                market_regime,
                strength,
                [
                    "bearish hoger timeframe",
                    "versnellend neerwaarts momentum",
                    "verhoogd volume",
                ],
                cfg,
            )
        )

    long_retest = (
        fifteen["low"] <= fifteen["ema_fast"]
        and fifteen["close"] > fifteen["ema_fast"]
        and fifteen["previous_ema_relation"] <= 0
    )

    short_retest = (
        fifteen["high"] >= fifteen["ema_fast"]
        and fifteen["close"] < fifteen["ema_fast"]
        and fifteen["previous_ema_relation"] >= 0
    )

    if (
        bullish
        and long_retest
        and fifteen["ema_rising"]
        and 44 <= fifteen["rsi"] <= 66
        and atr_ok
        and fifteen["volume_ratio"] >= 0.80
    ):
        signals.append(
            make_signal(
                market,
                fifteen,
                "pullback_retest",
                "LONG",
                market_regime,
                strength,
                [
                    "bullish hoger timeframe",
                    "terugtest en herstel boven 15m EMA",
                    "RSI niet oververhit",
                ],
                cfg,
            )
        )

    if (
        bearish
        and short_retest
        and fifteen["ema_falling"]
        and 34 <= fifteen["rsi"] <= 56
        and atr_ok
        and fifteen["volume_ratio"] >= 0.80
    ):
        signals.append(
            make_signal(
                market,
                fifteen,
                "pullback_retest",
                "SHORT",
                market_regime,
                strength,
                [
                    "bearish hoger timeframe",
                    "terugtest en afwijzing onder 15m EMA",
                    "RSI niet extreem oversold",
                ],
                cfg,
            )
        )

    if (
        market_regime == "NEUTRAL"
        and fifteen["breakout_up"]
        and fifteen["ema_rising"]
        and 55 <= fifteen["rsi"] <= 78
        and atr_ok
        and fifteen["volume_ratio"] >= 1.70
        and not_extended_long
    ):
        signals.append(
            make_signal(
                market,
                fifteen,
                "range_breakout",
                "LONG",
                market_regime,
                strength,
                [
                    "uitbraak uit neutrale markt",
                    "sterk volume",
                    "RSI en EMA bevestigen omhoog",
                ],
                cfg,
            )
        )

    if (
        market_regime == "NEUTRAL"
        and fifteen["breakout_down"]
        and fifteen["ema_falling"]
        and 22 <= fifteen["rsi"] <= 45
        and atr_ok
        and fifteen["volume_ratio"] >= 1.70
        and not_extended_short
    ):
        signals.append(
            make_signal(
                market,
                fifteen,
                "range_breakout",
                "SHORT",
                market_regime,
                strength,
                [
                    "neerwaartse uitbraak uit neutrale markt",
                    "sterk volume",
                    "RSI en EMA bevestigen omlaag",
                ],
                cfg,
            )
        )

    if (
        market_regime == "NEUTRAL"
        and fifteen["bb_lower"] > 0
        and fifteen["close"] < fifteen["bb_lower"]
        and fifteen["rsi"] <= 30
        and atr_ok
    ):
        signals.append(
            make_signal(
                market,
                fifteen,
                "mean_reversion",
                "LONG",
                market_regime,
                strength,
                [
                    "neutrale markt",
                    "onder onderste Bollinger Band",
                    "RSI oversold",
                ],
                cfg,
            )
        )

    if (
        market_regime == "NEUTRAL"
        and fifteen["bb_upper"] > 0
        and fifteen["close"] > fifteen["bb_upper"]
        and fifteen["rsi"] >= 70
        and atr_ok
    ):
        signals.append(
            make_signal(
                market,
                fifteen,
                "mean_reversion",
                "SHORT",
                market_regime,
                strength,
                [
                    "neutrale markt",
                    "boven bovenste Bollinger Band",
                    "RSI overbought",
                ],
                cfg,
            )
        )

    signals.sort(
        key=lambda item: (
            item["score_ok"],
            item["economically_positive"],
            item["score"],
            item["economics"]["expected_net_pct"],
        ),
        reverse=True,
    )

    return signals, market_regime, strength


def analyse_symbol(
    exchange: ccxt.Exchange,
    market: Dict[str, Any],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    snapshots: Dict[str, Dict[str, Any]] = {}
    fifteen_dataframe: Optional[pd.DataFrame] = None

    for timeframe in ("15m", "1h", "4h"):
        dataframe = closed_candles(
            exchange,
            market["symbol"],
            timeframe,
            cfg["candles_limit"],
        )

        if timeframe == "15m":
            fifteen_dataframe = dataframe.copy()

        snapshots[timeframe] = snapshot(dataframe, cfg)

    signals, market_regime, strength = find_signals(
        market,
        snapshots,
        cfg,
    )

    if signals:
        source = "FRESH_TICKER"
        try:
            ticker = api_call(
                f"fresh ticker {market['symbol']}",
                lambda: exchange.fetch_ticker(market["symbol"]),
            )
        except Exception as exc:
            LOG.warning(
                "Fresh ticker mislukt | %s | fallback scan-ticker | %s",
                market["symbol"], exc,
            )
            source = "SCAN_TICKER_FALLBACK"
            ticker = market

        bid = to_float(ticker.get("bid"), to_float(market.get("bid"), 0.0))
        ask = to_float(ticker.get("ask"), to_float(market.get("ask"), 0.0))
        last = to_float(ticker.get("last"), to_float(market.get("last"), 0.0))
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else last
        current_spread = spread_pct({"bid": bid, "ask": ask})

        for signal in signals:
            candle_entry = to_float(signal.get("entry_price"), 0.0)
            executable = (ask if signal["side"] == "LONG" else bid) or last
            gap = (
                (executable - candle_entry) / candle_entry * 100.0
                if candle_entry > 0 and executable > 0 else 0.0
            )

            signal.update({
                "detection_quote_at": now_iso(),
                "detection_last": last,
                "detection_bid": bid,
                "detection_ask": ask,
                "detection_mid": mid,
                "detection_spread_pct": current_spread,
                "executable_entry_price": executable,
                "entry_gap_pct": gap,
                "adverse_entry_gap_pct":
                    gap if signal["side"] == "LONG" else -gap,
                "quote_source": source,
            })

    fifteen = snapshots["15m"]

    recent_candles: List[Dict[str, Any]] = []

    if fifteen_dataframe is not None:
        for _, row in fifteen_dataframe.tail(120).iterrows():
            recent_candles.append({
                "timestamp_ms": int(row["timestamp"]),
                "timestamp": str(row["timestamp_iso"]),
                "high": to_float(row["high"]),
                "low": to_float(row["low"]),
                "close": to_float(row["close"]),
            })

    return {
        "symbol": market["symbol"],
        "quote_volume": round(market["quote_volume"], 2),
        "spread_pct": round(market["spread_pct"], 6),
        "change_pct_24h": round(market["change_pct_24h"], 4),
        "market_regime": market_regime,
        "regime_strength": strength,
        "close_15m": fifteen["close"],
        "rsi_15m": round(fifteen["rsi"], 2),
        "atr_pct_15m": round(fifteen["atr_pct"], 4),
        "volume_ratio_15m": round(fifteen["volume_ratio"], 3),
        "momentum_4_candles_pct": round(
            fifteen["momentum_4_pct"],
            4,
        ),
        "signals": signals,
        "_recent_candles_15m": recent_candles,
    }




def append_signal_measurement(signal: Dict[str, Any]) -> None:
    """Schrijf execution-meetdata apart, zonder bestaande CSV te wijzigen."""
    row = {
        "signal_key": signal.get("signal_key"),
        "detected_at": signal.get("detected_at"),
        "candle_timestamp": signal.get("candle_timestamp"),
        "symbol": signal.get("symbol"),
        "strategy": signal.get("strategy"),
        "side": signal.get("side"),
        "selection_reason": signal.get("selection_reason", "UNKNOWN"),
        "market_regime": signal.get("market_regime"),
        "score": signal.get("score"),
        "candle_entry_price": signal.get("entry_price"),
        "detection_quote_at": signal.get("detection_quote_at"),
        "detection_last": signal.get("detection_last"),
        "detection_bid": signal.get("detection_bid"),
        "detection_ask": signal.get("detection_ask"),
        "detection_mid": signal.get("detection_mid"),
        "detection_spread_pct": signal.get("detection_spread_pct"),
        "executable_entry_price": signal.get("executable_entry_price"),
        "entry_gap_pct": signal.get("entry_gap_pct"),
        "adverse_entry_gap_pct": signal.get("adverse_entry_gap_pct"),
        "quote_source": signal.get("quote_source"),
        "shadow_eligible": signal.get("shadow_eligible"),
    }

    SIGNAL_MEASUREMENTS_FILE.parent.mkdir(parents=True, exist_ok=True)

    with SIGNAL_MEASUREMENTS_FILE.open("a", encoding="utf-8") as file:
        file.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_unique_signal(signal: Dict[str, Any]) -> None:
    SIGNALS_CSV_FILE.parent.mkdir(parents=True, exist_ok=True)
    needs_header = ensure_csv_schema(SIGNALS_CSV_FILE, CSV_HEADER)
    expected = signal["economics"]["expected_eur"]

    row = {
        "detected_at": signal["detected_at"],
        "candle_timestamp": signal["candle_timestamp"],
        "symbol": signal["symbol"],
        "strategy": signal["strategy"],
        "side": signal["side"],
        "market_regime": signal["market_regime"],
        "regime_strength": signal["regime_strength"],
        "score": signal["score"],
        "entry_price": signal["entry_price"],
        "take_profit": signal["take_profit"],
        "stop_loss": signal["stop_loss"],
        "rsi": signal["rsi"],
        "atr_pct": signal["atr_pct"],
        "volume_ratio": signal["volume_ratio"],
        "spread_pct": signal["spread_pct"],
        "quote_volume": signal["quote_volume"],
        "change_pct_24h": signal["change_pct_24h"],
        "expected_net_pct": signal["economics"]["expected_net_pct"],
        "risk_net_pct": signal["economics"]["risk_net_pct"],
        "reward_risk": signal["economics"]["reward_risk"],
        "expected_profit_eur": signal["economics"]["expected_profit_eur"],
        "expected_loss_eur": signal["economics"]["expected_loss_eur"],
        "expected_eur_120": expected["120"],
        "expected_eur_125": expected["125"],
        "expected_eur_130": expected["130"],
        "expected_eur_135": expected["135"],
        "shadow_eligible": signal["shadow_eligible"],
        "shadow_rejection_reasons": " | ".join(
            signal["shadow_rejection_reasons"]
        ),
        "reasons": " | ".join(signal["reasons"]),
    }

    with SIGNALS_CSV_FILE.open(
        "a",
        encoding="utf-8",
        newline="",
    ) as file:
        writer = csv.DictWriter(file, fieldnames=CSV_HEADER)

        if needs_header:
            writer.writeheader()

        writer.writerow(row)

    try:
        append_signal_measurement(signal)
    except Exception as exc:
        LOG.warning(
            "Signal measurement schrijven mislukt | %s | %s",
            signal.get("signal_key"),
            exc,
        )




def market_record_for_symbol(
    exchange: ccxt.Exchange,
    tickers: Dict[str, Any],
    symbol: str,
) -> Optional[Dict[str, Any]]:
    market = exchange.markets.get(symbol)
    ticker = tickers.get(symbol)

    if not isinstance(market, dict) or not isinstance(ticker, dict):
        return None

    last = to_float(ticker.get("last"), 0.0)

    if last <= 0:
        return None

    return {
        "symbol": symbol,
        "base": str(market.get("base") or "").upper(),
        "last": last,
        "quote_volume": quote_volume(ticker),
        "spread_pct": spread_pct(ticker),
        "change_pct_24h": to_float(ticker.get("percentage"), 0.0),
    }


def include_open_position_markets(
    exchange: ccxt.Exchange,
    tickers: Dict[str, Any],
    markets: List[Dict[str, Any]],
    state: Dict[str, Any],
) -> List[Dict[str, Any]]:
    result = list(markets)
    present = {item["symbol"] for item in result}

    for symbol in state["open_positions"]:
        if symbol in present:
            continue

        record = market_record_for_symbol(exchange, tickers, symbol)

        if record is not None:
            result.append(record)
            present.add(symbol)

    return result


def append_shadow_trade(row: Dict[str, Any]) -> None:
    SHADOW_TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
    needs_header = ensure_csv_schema(
        SHADOW_TRADES_FILE,
        SHADOW_TRADE_HEADER,
    )

    with SHADOW_TRADES_FILE.open(
        "a",
        encoding="utf-8",
        newline="",
    ) as file:
        writer = csv.DictWriter(file, fieldnames=SHADOW_TRADE_HEADER)

        if needs_header:
            writer.writeheader()

        writer.writerow({
            key: row.get(key, "")
            for key in SHADOW_TRADE_HEADER
        })



def open_shadow_position(
    signal: Dict[str, Any],
    state: Dict[str, Any],
    cfg: Dict[str, Any],
) -> bool:
    symbol = signal["symbol"]

    # Eén virtuele positie per munt voorkomt dubbele, sterk gecorreleerde
    # inzetten van meerdere strategieën op hetzelfde moment.
    if symbol in state["open_positions"]:
        return False

    if len(state["open_positions"]) >= cfg["max_shadow_positions"]:
        return False

    spread_half_fraction = signal["spread_pct"] / 200.0

    if signal["side"] == "LONG":
        entry_price = signal["entry_price"] * (1 + spread_half_fraction)
        take_profit = entry_price + signal["atr"] * cfg["atr_tp_mult"]
        stop_loss = entry_price - signal["atr"] * cfg["atr_sl_mult"]
    else:
        entry_price = signal["entry_price"] * (1 - spread_half_fraction)
        take_profit = entry_price - signal["atr"] * cfg["atr_tp_mult"]
        stop_loss = entry_price + signal["atr"] * cfg["atr_sl_mult"]

    stake = cfg["stake_eur"]
    amount = stake / entry_price if entry_price > 0 else 0.0
    entry_fee = stake * cfg["fee_pct_per_side"] / 100.0

    state["open_positions"][symbol] = {
        "opened_at": now_iso(),
        "symbol": symbol,
        "strategy": signal["strategy"],
        "side": signal["side"],
        "market_regime": signal["market_regime"],
        "signal_score": signal["score"],
        "entry_price": entry_price,
        "amount": amount,
        "stake_eur": stake,
        "entry_fee_eur": entry_fee,
        "entry_spread_pct": signal["spread_pct"],
        "atr_pct": signal["atr_pct"],
        "take_profit": take_profit,
        "stop_loss": stop_loss,
        "entry_candle_timestamp_ms": signal["candle_timestamp_ms"],
        "last_checked_candle_ms": signal["candle_timestamp_ms"],
    }

    totals = state["shadow_totals"]
    totals["opened"] = to_int(totals.get("opened"), 0) + 1

    LOG.info(
        "SCHADUW OPEN | %s | %s | %s | entry=%.10f | "
        "tp=%.10f | sl=%.10f | score=%.1f",
        symbol,
        signal["strategy"],
        signal["side"],
        entry_price,
        take_profit,
        stop_loss,
        signal["score"],
    )

    return True


def close_shadow_position(
    symbol: str,
    position: Dict[str, Any],
    raw_exit_price: float,
    exit_spread_pct: float,
    exit_reason: str,
    exit_candle_timestamp_ms: int,
    state: Dict[str, Any],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    spread_half_fraction = exit_spread_pct / 200.0

    if position["side"] == "LONG":
        exit_price = raw_exit_price * (1 - spread_half_fraction)
        gross_pnl = (
            exit_price - position["entry_price"]
        ) * position["amount"]
    else:
        exit_price = raw_exit_price * (1 + spread_half_fraction)
        gross_pnl = (
            position["entry_price"] - exit_price
        ) * position["amount"]

    exit_notional = position["amount"] * exit_price
    exit_fee = exit_notional * cfg["fee_pct_per_side"] / 100.0
    total_fees = position["entry_fee_eur"] + exit_fee
    net_pnl = gross_pnl - total_fees
    return_pct = (
        net_pnl / position["stake_eur"] * 100
        if position["stake_eur"] > 0
        else 0.0
    )
    duration_minutes = max(
        0.0,
        (
            exit_candle_timestamp_ms
            - position["entry_candle_timestamp_ms"]
        ) / 60_000,
    )

    row = {
        "opened_at": position["opened_at"],
        "closed_at": datetime.fromtimestamp(
            exit_candle_timestamp_ms / 1000,
            tz=timezone.utc,
        ).isoformat(),
        "symbol": symbol,
        "strategy": position["strategy"],
        "side": position["side"],
        "market_regime": position["market_regime"],
        "signal_score": position["signal_score"],
        "entry_price": round(position["entry_price"], 12),
        "exit_price": round(exit_price, 12),
        "stake_eur": round(position["stake_eur"], 4),
        "amount": round(position["amount"], 12),
        "entry_fee_eur": round(position["entry_fee_eur"], 6),
        "exit_fee_eur": round(exit_fee, 6),
        "total_fees_eur": round(total_fees, 6),
        "entry_spread_pct": round(position["entry_spread_pct"], 6),
        "exit_spread_pct": round(exit_spread_pct, 6),
        "atr_pct": round(position["atr_pct"], 6),
        "take_profit": round(position["take_profit"], 12),
        "stop_loss": round(position["stop_loss"], 12),
        "exit_reason": exit_reason,
        "gross_pnl_eur": round(gross_pnl, 6),
        "net_pnl_eur": round(net_pnl, 6),
        "return_pct": round(return_pct, 6),
        "duration_minutes": round(duration_minutes, 2),
        "entry_candle_timestamp_ms": position[
            "entry_candle_timestamp_ms"
        ],
        "exit_candle_timestamp_ms": exit_candle_timestamp_ms,
    }

    append_shadow_trade(row)
    del state["open_positions"][symbol]

    totals = state["shadow_totals"]
    totals["closed"] = to_int(totals.get("closed"), 0) + 1
    totals["net_pnl_eur"] = round(
        to_float(totals.get("net_pnl_eur"), 0.0) + net_pnl,
        6,
    )
    totals["total_fees_eur"] = round(
        to_float(totals.get("total_fees_eur"), 0.0) + total_fees,
        6,
    )

    if net_pnl > 0.000001:
        totals["wins"] = to_int(totals.get("wins"), 0) + 1
    elif net_pnl < -0.000001:
        totals["losses"] = to_int(totals.get("losses"), 0) + 1
    else:
        totals["neutral"] = to_int(totals.get("neutral"), 0) + 1

    LOG.info(
        "SCHADUW SLUIT | %s | %s | %s | reden=%s | "
        "netto=€%.4f | kosten=€%.4f",
        symbol,
        position["strategy"],
        position["side"],
        exit_reason,
        net_pnl,
        total_fees,
    )

    return row


def manage_shadow_positions(
    analyses_by_symbol: Dict[str, Dict[str, Any]],
    state: Dict[str, Any],
    cfg: Dict[str, Any],
) -> List[Dict[str, Any]]:
    closed_rows: List[Dict[str, Any]] = []

    for symbol, position in list(state["open_positions"].items()):
        analysis = analyses_by_symbol.get(symbol)

        if analysis is None:
            continue

        candles = analysis.get("_recent_candles_15m", [])
        exit_spread_pct = to_float(analysis.get("spread_pct"), 999.0)

        for candle in candles:
            candle_ms = to_int(candle.get("timestamp_ms"), 0)

            if candle_ms <= to_int(
                position.get("last_checked_candle_ms"),
                0,
            ):
                continue

            position["last_checked_candle_ms"] = candle_ms
            high = to_float(candle.get("high"), 0.0)
            low = to_float(candle.get("low"), 0.0)
            close = to_float(candle.get("close"), 0.0)
            stop_hit = False
            target_hit = False

            if position["side"] == "LONG":
                stop_hit = low <= position["stop_loss"]
                target_hit = high >= position["take_profit"]
            else:
                stop_hit = high >= position["stop_loss"]
                target_hit = low <= position["take_profit"]

            # Conservatieve simulatie: bij TP én SL in dezelfde candle
            # nemen we aan dat de stop-loss eerst geraakt werd.
            if stop_hit:
                closed_rows.append(
                    close_shadow_position(
                        symbol,
                        position,
                        position["stop_loss"],
                        exit_spread_pct,
                        "stop_loss",
                        candle_ms,
                        state,
                        cfg,
                    )
                )
                break

            if target_hit:
                closed_rows.append(
                    close_shadow_position(
                        symbol,
                        position,
                        position["take_profit"],
                        exit_spread_pct,
                        "take_profit",
                        candle_ms,
                        state,
                        cfg,
                    )
                )
                break

            held_minutes = (
                candle_ms - position["entry_candle_timestamp_ms"]
            ) / 60_000

            if held_minutes >= cfg["max_hold_minutes"] and close > 0:
                closed_rows.append(
                    close_shadow_position(
                        symbol,
                        position,
                        close,
                        exit_spread_pct,
                        "time_exit",
                        candle_ms,
                        state,
                        cfg,
                    )
                )
                break

    return closed_rows

def scan_once(
    exchange: ccxt.Exchange,
    cfg: Dict[str, Any],
    state: Dict[str, Any],
) -> Dict[str, Any]:
    started = time.monotonic()

    tickers = api_call(
        "alle tickers ophalen",
        lambda: exchange.fetch_tickers(),
    )

    if not isinstance(tickers, dict):
        raise RuntimeError("fetch_tickers gaf ongeldige data")

    markets, counts = select_markets(
        exchange,
        tickers,
        cfg,
        rotation_index=to_int(state.get("scan_count"), 0),
    )

    markets = include_open_position_markets(
        exchange,
        tickers,
        markets,
        state,
    )

    analyses: List[Dict[str, Any]] = []
    analyses_by_symbol: Dict[str, Dict[str, Any]] = {}
    errors: List[Dict[str, str]] = []
    signals: List[Dict[str, Any]] = []

    for index, market in enumerate(markets, start=1):
        LOG.info(
            "Analyse %d/%d | %s | volume=€%.0f | spread=%.4f%%",
            index,
            len(markets),
            market["symbol"],
            market["quote_volume"],
            market["spread_pct"],
        )

        try:
            result = analyse_symbol(exchange, market, cfg)
            analyses.append(result)
            analyses_by_symbol[result["symbol"]] = result
            signals.extend(result["signals"])

        except Exception as exc:
            LOG.warning(
                "Analyse mislukt | %s | %s: %s",
                market["symbol"],
                type(exc).__name__,
                exc,
            )
            errors.append({
                "symbol": market["symbol"],
                "error": f"{type(exc).__name__}: {exc}",
            })

    closed_this_scan = manage_shadow_positions(
        analyses_by_symbol,
        state,
        cfg,
    )
    closed_symbols = {
        row["symbol"]
        for row in closed_this_scan
    }

    signals.sort(
        key=lambda item: (
            item["shadow_eligible"],
            item["score"],
            item["economics"]["reward_risk"],
            item["economics"]["expected_profit_eur"],
        ),
        reverse=True,
    )

    seen_order = list(dict.fromkeys(
        str(item) for item in state["seen_signal_keys"]
    ))
    seen = set(seen_order)
    new_signals = 0
    opened_this_scan = 0

    for signal in signals:
        key = signal["signal_key"]

        if key in seen:
            continue

        seen.add(key)
        seen_order.append(key)
        append_unique_signal(signal)
        new_signals += 1

        if (
            signal["shadow_eligible"]
            and signal["symbol"] not in closed_symbols
            and open_shadow_position(signal, state, cfg)
        ):
            opened_this_scan += 1

    state["seen_signal_keys"] = seen_order[-5000:]
    state["last_scan_at"] = now_iso()
    state["scan_count"] = to_int(state.get("scan_count"), 0) + 1
    state["total_unique_signals"] = (
        to_int(state.get("total_unique_signals"), 0) + new_signals
    )

    public_analyses: List[Dict[str, Any]] = []

    for analysis in analyses:
        public_analyses.append({
            key: value
            for key, value in analysis.items()
            if not key.startswith("_")
        })

    report = {
        "version": VERSION,
        "generated_at": now_iso(),
        "mode": "VIRTUAL_SHADOW_TRADING",
        "safety": {
            "orders_possible": False,
            "diamond_state_modified": False,
            "diamond_transactions_modified": False,
            "scanner_state_file": str(STATE_FILE),
            "shadow_trades_file": str(SHADOW_TRADES_FILE),
        },
        "settings": {
            key: sorted(value) if isinstance(value, set) else value
            for key, value in cfg.items()
        },
        "market_counts": counts,
        "selected_markets": [
            {
                "symbol": market["symbol"],
                "quote_volume": round(market["quote_volume"], 2),
                "spread_pct": round(market["spread_pct"], 6),
                "change_pct_24h": round(
                    market["change_pct_24h"],
                    4,
                ),
                "selection_reason": market.get("selection_reason", "UNKNOWN"),
            }
            for market in markets
        ],
        "analysed_count": len(public_analyses),
        "analyses": public_analyses,
        "signals": signals,
        "new_signals_this_scan": new_signals,
        "total_unique_signals": state["total_unique_signals"],
        "shadow": {
            "opened_this_scan": opened_this_scan,
            "closed_this_scan": len(closed_this_scan),
            "closed_trades": closed_this_scan,
            "open_positions_count": len(state["open_positions"]),
            "open_positions": list(state["open_positions"].values()),
            "totals": state["shadow_totals"],
        },
        "errors": errors,
        "duration_seconds": round(time.monotonic() - started, 2),
    }

    save_json_atomic(STATE_FILE, state)
    save_json_atomic(REPORT_FILE, report)

    return report



def print_summary(report: Dict[str, Any]) -> None:
    counts = report["market_counts"]
    shadow = report["shadow"]
    totals = shadow["totals"]

    print()
    print("=" * 72)
    print(" DIAMOND MARKET SCANNER v1.1")
    print(f" {report['generated_at']}")
    print("=" * 72)
    print()
    print(f"Actieve EUR-spotmarkten : {counts['eur_spot_active']}")
    print(f"Na volume/spreadfilter : {counts['eligible']}")
    print(f"Diep geanalyseerd      : {report['analysed_count']}")
    print(f"Signalen deze ronde    : {len(report['signals'])}")
    print(f"Nieuwe unieke signalen : {report['new_signals_this_scan']}")
    print(f"Totaal unieke signalen : {report['total_unique_signals']}")
    print(f"Schaduwposities open   : {shadow['open_positions_count']}")
    print(f"Deze ronde geopend     : {shadow['opened_this_scan']}")
    print(f"Deze ronde gesloten    : {shadow['closed_this_scan']}")
    print(f"Schaduwtrades totaal   : {totals['closed']}")
    print(f"Schaduwresultaat       : €{totals['net_pnl_eur']:.4f}")
    print(f"Schaduwkosten          : €{totals['total_fees_eur']:.4f}")

    if report["signals"]:
        print()
        print("BESTE SIGNALEN")
        print("-" * 72)

        for signal in report["signals"][:10]:
            if signal["shadow_eligible"]:
                status = "SCHADUWTRADE"
            else:
                reason = signal["shadow_rejection_reasons"][0]
                status = f"AFGEWEZEN: {reason}"

            print(
                f"{signal['symbol']:12} "
                f"{signal['side']:5} "
                f"{signal['strategy']:18} "
                f"score={signal['score']:5.1f} "
                f"RR={signal['economics']['reward_risk']:5.2f} "
                f"winst=€{signal['economics']['expected_profit_eur']:6.3f} "
                f"{status}"
            )
    else:
        print()
        print("Geen technisch signaal in deze scan.")

    if report["errors"]:
        print()
        print(f"Markten met fout       : {len(report['errors'])}")

    print()
    print(f"JSON-rapport           : {REPORT_FILE}")
    print(f"Signalenhistorie       : {SIGNALS_CSV_FILE}")
    print(f"Scannerstate           : {STATE_FILE}")
    print(f"Schaduwtrades          : {SHADOW_TRADES_FILE}")
    print(f"Logbestand             : {LOG_FILE}")
    print("=" * 72)



def synthetic_dataframe(
    direction: str,
    rows: int = 260,
) -> pd.DataFrame:
    records: List[List[float]] = []
    price = 100.0
    start = int(
        (
            datetime.now(timezone.utc).timestamp()
            - rows * 900
        )
        * 1000
    )

    for index in range(rows):
        if direction == "up":
            drift = 0.12
        elif direction == "down":
            drift = -0.12
        else:
            drift = math.sin(index / 8) * 0.03

        open_price = price
        close = max(
            1.0,
            price + drift + math.sin(index / 5) * 0.02,
        )
        high = max(open_price, close) + 0.10
        low = min(open_price, close) - 0.10
        volume = 1000 + index * 2

        records.append([
            start + index * 900_000,
            open_price,
            high,
            low,
            close,
            volume,
        ])
        price = close

    dataframe = pd.DataFrame(
        records,
        columns=[
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ],
    )

    dataframe["timestamp_iso"] = pd.to_datetime(
        dataframe["timestamp"],
        unit="ms",
        utc=True,
    )

    return dataframe


def self_test() -> None:
    test_cfg = settings(
        {
            "quote": "EUR",
            "risk": {
                "fixed_stake_quote": 120,
                "max_spread_pct": 0.25,
            },
            "fees": {
                "taker_fee_pct": 0.25,
            },
        },
        None,
    )

    rising = snapshot(synthetic_dataframe("up"), test_cfg)
    falling = snapshot(synthetic_dataframe("down"), test_cfg)
    sideways = snapshot(synthetic_dataframe("sideways"), test_cfg)

    assert rising["close"] > rising["ema_fast"] > rising["ema_slow"]
    assert falling["close"] < falling["ema_fast"] < falling["ema_slow"]
    assert rising["atr"] > 0
    assert falling["atr"] > 0
    assert 0 <= sideways["rsi"] <= 100

    calculation = economics(100, 1, 0.05, test_cfg)

    assert calculation["roundtrip_cost_pct"] == 0.55
    assert "120" in calculation["expected_eur"]
    assert calculation["expected_profit_eur"] == round(
        120 * calculation["expected_net_pct"] / 100,
        4,
    )

    good_market = {
        "spread_pct": 0.02,
    }
    good_calculation = {
        "expected_net_pct": 1.0,
        "reward_risk": 1.5,
        "expected_profit_eur": 1.2,
    }
    bad_calculation = {
        "expected_net_pct": 0.44,
        "reward_risk": 0.45,
        "expected_profit_eur": 0.53,
    }

    assert not shadow_rejection_reasons(
        80,
        good_market,
        good_calculation,
        test_cfg,
    )
    assert shadow_rejection_reasons(
        94,
        good_market,
        bad_calculation,
        test_cfg,
    )

    state = normalize_state({})
    assert state["open_positions"] == {}
    assert state["shadow_totals"]["closed"] == 0

    print("DIAMOND MARKET SCANNER v1.1 ZELFTEST: GESLAAGD")
    print(
        "Geen netwerk gebruikt en geen /var/data-bestanden gewijzigd."
    )



def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Diamond Market Scanner - "
            "analyse en virtuele schaduwtrades"
        )
    )

    parser.add_argument(
        "--loop",
        action="store_true",
        help="Blijf iedere 15 minuten scannen",
    )

    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Test indicatoren zonder Bitvavo",
    )

    parser.add_argument(
        "--top",
        type=int,
        default=None,
        help="Tijdelijk aantal diep te analyseren markten",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Toon extra logregels",
    )

    return parser.parse_args()


def main() -> None:
    args = arguments()

    if args.self_test:
        self_test()
        return

    setup_logging(args.verbose)

    config = load_yaml(CFG_FILE)
    cfg = settings(config, args.top)
    state = normalize_state(
        load_json(STATE_FILE, default_state())
    )
    exchange = create_exchange()

    LOG.info(
        "Diamond Market Scanner v%s gestart | "
        "mode=VIRTUAL_SHADOW_TRADING | orders_possible=False",
        VERSION,
    )

    while True:
        try:
            config = load_yaml(CFG_FILE)
            cfg = settings(config, args.top)

            report = scan_once(
                exchange,
                cfg,
                state,
            )
            print_summary(report)

        except KeyboardInterrupt:
            LOG.info("Scanner gestopt")
            break

        except Exception as exc:
            LOG.exception("Scannerfout: %s", exc)

            if not args.loop:
                raise

        if not args.loop:
            break

        time.sleep(cfg["loop_sleep_seconds"])


if __name__ == "__main__":
    main()
