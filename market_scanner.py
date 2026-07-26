#!/usr/bin/env python3
"""
Diamond Market Scanner v1.0

Veilige TA-schaduwscanner voor Diamond Trader.

Doet wel:
- haalt actieve Bitvavo EUR-spotmarkten op;
- filtert op volume en spread;
- analyseert afgesloten candles op 15m, 1u en 4u;
- bepaalt bullish, bearish of neutraal marktregime;
- zoekt trend-breakout, momentum, pullback/retest,
  range-breakout en mean-reversion;
- berekent verwachte opbrengst na kosten voor €120, €125, €130 en €135;
- schrijft signalen naar eigen bestanden in /var/data.

Doet nooit:
- orders plaatsen;
- diamond_state.json wijzigen;
- diamond_transactions.csv wijzigen;
- bestaande long- of shorttests beïnvloeden.

Gebruik:
    python3 market_scanner.py --self-test
    python3 market_scanner.py
    python3 market_scanner.py --loop
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


VERSION = "1.0"

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
    "expected_eur_120",
    "expected_eur_125",
    "expected_eur_130",
    "expected_eur_135",
    "economically_positive",
    "reasons",
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


def default_state() -> Dict[str, Any]:
    return {
        "version": VERSION,
        "started_at": now_iso(),
        "last_scan_at": None,
        "scan_count": 0,
        "seen_signal_keys": [],
        "total_unique_signals": 0,
    }


def normalize_state(state: Dict[str, Any]) -> Dict[str, Any]:
    defaults = default_state()

    for key, value in defaults.items():
        state.setdefault(key, value)

    if not isinstance(state.get("seen_signal_keys"), list):
        state["seen_signal_keys"] = []

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
        get_cfg(config, "market_scanner.top_n_markets", 20),
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
                    get_cfg(
                        config,
                        "scanner.min_quote_volume",
                        250_000,
                    ),
                ),
                250_000,
            ),
        ),
        "max_spread_pct": max(
            0.001,
            to_float(
                get_cfg(
                    config,
                    "market_scanner.max_spread_pct",
                    get_cfg(
                        config,
                        "risk.max_spread_pct",
                        0.25,
                    ),
                ),
                0.25,
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
                    get_cfg(
                        config,
                        "market_scanner.candles_limit",
                        240,
                    ),
                    240,
                ),
            ),
        ),
        "loop_sleep_seconds": max(
            300,
            to_int(
                get_cfg(
                    config,
                    "market_scanner.loop_sleep_seconds",
                    900,
                ),
                900,
            ),
        ),
        "ema_fast": max(
            2,
            to_int(
                get_cfg(config, "market_scanner.ema_fast", 20),
                20,
            ),
        ),
        "ema_slow": max(
            5,
            to_int(
                get_cfg(config, "market_scanner.ema_slow", 50),
                50,
            ),
        ),
        "rsi_len": max(
            2,
            to_int(
                get_cfg(config, "market_scanner.rsi_len", 14),
                14,
            ),
        ),
        "atr_len": max(
            2,
            to_int(
                get_cfg(config, "market_scanner.atr_len", 14),
                14,
            ),
        ),
        "breakout_lookback": max(
            5,
            to_int(
                get_cfg(
                    config,
                    "market_scanner.breakout_lookback",
                    20,
                ),
                20,
            ),
        ),
        "min_atr_pct": max(
            0.0,
            to_float(
                get_cfg(
                    config,
                    "market_scanner.min_atr_pct",
                    0.20,
                ),
                0.20,
            ),
        ),
        "min_signal_score": max(
            0.0,
            min(
                100.0,
                to_float(
                    get_cfg(
                        config,
                        "market_scanner.min_signal_score",
                        70,
                    ),
                    70,
                ),
            ),
        ),
        "min_volume_ratio": max(
            0.0,
            to_float(
                get_cfg(
                    config,
                    "market_scanner.min_volume_ratio",
                    1.10,
                ),
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
                get_cfg(
                    config,
                    "market_scanner.atr_tp_mult",
                    2.6,
                ),
                2.6,
            ),
        ),
        "atr_sl_mult": max(
            0.1,
            to_float(
                get_cfg(
                    config,
                    "market_scanner.atr_sl_mult",
                    1.2,
                ),
                1.2,
            ),
        ),
        "min_expected_net_pct": to_float(
            get_cfg(
                config,
                "market_scanner.min_expected_net_pct",
                0.10,
            ),
            0.10,
        ),
        "max_extension_atr": max(
            0.5,
            to_float(
                get_cfg(
                    config,
                    "market_scanner.max_extension_atr",
                    3.0,
                ),
                3.0,
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
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    eligible: List[Dict[str, Any]] = []

    counts = {
        "all_markets": 0,
        "eur_spot_active": 0,
        "excluded": 0,
        "volume_blocked": 0,
        "spread_blocked": 0,
        "invalid_ticker": 0,
        "eligible": 0,
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
            "quote_volume": volume,
            "spread_pct": spread,
            "change_pct_24h": to_float(
                ticker.get("percentage"),
                0.0,
            ),
        })

    eligible.sort(
        key=lambda item: item["quote_volume"],
        reverse=True,
    )

    counts["eligible"] = len(eligible)

    return eligible[:cfg["top_n"]], counts


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
        "expected_eur": {
            str(stake): round(
                stake * expected_net_pct / 100,
                4,
            )
            for stake in (120, 125, 130, 135)
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
        take_profit = (
            snap["close"] + snap["atr"] * cfg["atr_tp_mult"]
        )
        stop_loss = (
            snap["close"] - snap["atr"] * cfg["atr_sl_mult"]
        )
    else:
        take_profit = (
            snap["close"] - snap["atr"] * cfg["atr_tp_mult"]
        )
        stop_loss = (
            snap["close"] + snap["atr"] * cfg["atr_sl_mult"]
        )

    return {
        "signal_key": (
            f"{market['symbol']}|{strategy}|{side}|"
            f"{snap['timestamp_ms']}"
        ),
        "detected_at": now_iso(),
        "candle_timestamp": snap["timestamp"],
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
        "rsi": snap["rsi"],
        "atr_pct": snap["atr_pct"],
        "volume_ratio": snap["volume_ratio"],
        "momentum_4_pct": snap["momentum_4_pct"],
        "extension_atr": snap["extension_atr"],
        "spread_pct": market["spread_pct"],
        "quote_volume": market["quote_volume"],
        "change_pct_24h": market["change_pct_24h"],
        "economics": calculation,
        "economically_positive": (
            calculation["expected_net_pct"]
            >= cfg["min_expected_net_pct"]
        ),
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

    for timeframe in ("15m", "1h", "4h"):
        dataframe = closed_candles(
            exchange,
            market["symbol"],
            timeframe,
            cfg["candles_limit"],
        )
        snapshots[timeframe] = snapshot(dataframe, cfg)

    signals, market_regime, strength = find_signals(
        market,
        snapshots,
        cfg,
    )

    fifteen = snapshots["15m"]

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
        "volume_ratio_15m": round(
            fifteen["volume_ratio"],
            3,
        ),
        "momentum_4_candles_pct": round(
            fifteen["momentum_4_pct"],
            4,
        ),
        "signals": signals,
    }


def append_unique_signal(signal: Dict[str, Any]) -> None:
    SIGNALS_CSV_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    needs_header = (
        not SIGNALS_CSV_FILE.exists()
        or SIGNALS_CSV_FILE.stat().st_size == 0
    )

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
        "expected_net_pct": signal["economics"][
            "expected_net_pct"
        ],
        "expected_eur_120": expected["120"],
        "expected_eur_125": expected["125"],
        "expected_eur_130": expected["130"],
        "expected_eur_135": expected["135"],
        "economically_positive": signal[
            "economically_positive"
        ],
        "reasons": " | ".join(signal["reasons"]),
    }

    with SIGNALS_CSV_FILE.open(
        "a",
        encoding="utf-8",
        newline="",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=CSV_HEADER,
        )

        if needs_header:
            writer.writeheader()

        writer.writerow(row)


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
    )

    analyses: List[Dict[str, Any]] = []
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
            result = analyse_symbol(
                exchange,
                market,
                cfg,
            )
            analyses.append(result)
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

    signals.sort(
        key=lambda item: (
            item["score_ok"],
            item["economically_positive"],
            item["score"],
            item["economics"]["expected_net_pct"],
        ),
        reverse=True,
    )

    seen = {
        str(item)
        for item in state["seen_signal_keys"]
    }

    new_signals = 0

    for signal in signals:
        key = signal["signal_key"]

        if key in seen:
            continue

        seen.add(key)
        append_unique_signal(signal)
        new_signals += 1

    state["seen_signal_keys"] = list(seen)[-5000:]
    state["last_scan_at"] = now_iso()
    state["scan_count"] = to_int(
        state.get("scan_count"),
        0,
    ) + 1
    state["total_unique_signals"] = to_int(
        state.get("total_unique_signals"),
        0,
    ) + new_signals

    report = {
        "version": VERSION,
        "generated_at": now_iso(),
        "mode": "SHADOW_SIGNALS_ONLY",
        "safety": {
            "orders_possible": False,
            "diamond_state_modified": False,
            "diamond_transactions_modified": False,
        },
        "settings": {
            key: (
                sorted(value)
                if isinstance(value, set)
                else value
            )
            for key, value in cfg.items()
        },
        "market_counts": counts,
        "selected_markets": [
            {
                "symbol": market["symbol"],
                "quote_volume": round(
                    market["quote_volume"],
                    2,
                ),
                "spread_pct": round(
                    market["spread_pct"],
                    6,
                ),
                "change_pct_24h": round(
                    market["change_pct_24h"],
                    4,
                ),
            }
            for market in markets
        ],
        "analysed_count": len(analyses),
        "analyses": analyses,
        "signals": signals,
        "new_signals_this_scan": new_signals,
        "total_unique_signals": state[
            "total_unique_signals"
        ],
        "errors": errors,
        "duration_seconds": round(
            time.monotonic() - started,
            2,
        ),
    }

    save_json_atomic(STATE_FILE, state)
    save_json_atomic(REPORT_FILE, report)

    return report


def print_summary(report: Dict[str, Any]) -> None:
    counts = report["market_counts"]

    print()
    print("=" * 64)
    print(" DIAMOND MARKET SCANNER v1.0")
    print(f" {report['generated_at']}")
    print("=" * 64)
    print()
    print(
        f"Actieve EUR-spotmarkten : {counts['eur_spot_active']}"
    )
    print(
        f"Na volume/spreadfilter : {counts['eligible']}"
    )
    print(
        f"Diep geanalyseerd      : {report['analysed_count']}"
    )
    print(
        f"Signalen deze ronde    : {len(report['signals'])}"
    )
    print(
        f"Nieuwe unieke signalen : "
        f"{report['new_signals_this_scan']}"
    )
    print(
        f"Totaal unieke signalen : "
        f"{report['total_unique_signals']}"
    )

    if report["signals"]:
        print()
        print("BESTE SIGNALEN")
        print("-" * 64)

        for signal in report["signals"][:10]:
            status = (
                "BRUIKBAAR"
                if (
                    signal["score_ok"]
                    and signal["economically_positive"]
                )
                else "REGISTREREN"
            )

            print(
                f"{signal['symbol']:12} "
                f"{signal['side']:5} "
                f"{signal['strategy']:18} "
                f"score={signal['score']:5.1f} "
                f"netto={signal['economics']['expected_net_pct']:6.3f}% "
                f"{status}"
            )
    else:
        print()
        print("Geen technisch signaal in deze scan.")

    if report["errors"]:
        print()
        print(
            f"Markten met fout       : {len(report['errors'])}"
        )

    print()
    print(f"JSON-rapport           : {REPORT_FILE}")
    print(f"Signalenhistorie       : {SIGNALS_CSV_FILE}")
    print(f"Scannerstate           : {STATE_FILE}")
    print(f"Logbestand             : {LOG_FILE}")
    print("=" * 64)


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

    rising = snapshot(
        synthetic_dataframe("up"),
        test_cfg,
    )
    falling = snapshot(
        synthetic_dataframe("down"),
        test_cfg,
    )
    sideways = snapshot(
        synthetic_dataframe("sideways"),
        test_cfg,
    )

    assert (
        rising["close"]
        > rising["ema_fast"]
        > rising["ema_slow"]
    )
    assert (
        falling["close"]
        < falling["ema_fast"]
        < falling["ema_slow"]
    )
    assert rising["atr"] > 0
    assert falling["atr"] > 0
    assert 0 <= sideways["rsi"] <= 100

    calculation = economics(
        100,
        1,
        0.05,
        test_cfg,
    )

    assert calculation["roundtrip_cost_pct"] == 0.55
    assert "120" in calculation["expected_eur"]

    print("DIAMOND MARKET SCANNER ZELFTEST: GESLAAGD")
    print(
        "Geen netwerk gebruikt en geen /var/data-bestanden gewijzigd."
    )


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Diamond Market Scanner - "
            "alleen analyse en schaduwsignalen"
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
        "mode=SHADOW_SIGNALS_ONLY | orders_possible=False",
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
