#!/usr/bin/env python3
"""
Diamond Diagnose v3

Functies:
- controleert trend, RSI, ATR en spread per munt;
- plaatst nooit orders;
- wijzigt geen botposities;
- bewaart statistieken in:
  /var/data/diamond_diagnose_stats.json
"""

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import ccxt
import pandas as pd
import yaml
from dotenv import load_dotenv


load_dotenv()

LOG = logging.getLogger("diamond_diagnose")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

CFG_FILE = os.getenv(
    "CFG_FILE",
    "/opt/render/project/src/config.yaml",
).strip()

DIAG_STATS_FILE = os.getenv(
    "DIAG_STATS_FILE",
    "/var/data/diamond_diagnose_stats.json",
).strip()

LOOP_SLEEP_SECONDS = 15 * 60


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_float(
    value: Any,
    default: float = 0.0,
) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def get_cfg(
    config: Dict[str, Any],
    path: str,
    default: Any = None,
) -> Any:
    current: Any = config

    for part in path.split("."):
        if not isinstance(current, dict):
            return default

        if part not in current:
            return default

        current = current[part]

    return current


def load_config(
    path_str: str,
) -> Dict[str, Any]:
    path = Path(path_str)

    if not path.exists():
        raise FileNotFoundError(
            f"Configuratiebestand ontbreekt: {path_str}"
        )

    with path.open(
        "r",
        encoding="utf-8",
    ) as file:
        config = yaml.safe_load(file) or {}

    if not isinstance(config, dict):
        raise ValueError(
            "config.yaml bevat geen geldige YAML-structuur"
        )

    return config


def load_json(
    path_str: str,
    default: Dict[str, Any],
) -> Dict[str, Any]:
    path = Path(path_str)

    if not path.exists():
        return default.copy()

    try:
        with path.open(
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)

        if isinstance(data, dict):
            return data

    except Exception as exc:
        LOG.warning(
            "Statistiekbestand lezen mislukt: %s",
            exc,
        )

    return default.copy()


def save_json_atomic(
    path_str: str,
    data: Dict[str, Any],
) -> None:
    target = Path(path_str)

    target.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(target.parent),
        delete=False,
    ) as temporary:
        json.dump(
            data,
            temporary,
            indent=2,
            ensure_ascii=False,
        )

        temporary_name = temporary.name

    os.replace(
        temporary_name,
        target,
    )


def default_stats() -> Dict[str, Any]:
    return {
        "version": 3,
        "started_at": now_iso(),
        "last_round_at": None,
        "total_rounds": 0,
        "symbols": {},
    }


def default_symbol_stats() -> Dict[str, Any]:
    return {
        "checks": 0,
        "technical_signals": 0,
        "near_signals": 0,
        "trend_ok": 0,
        "rsi_ok": 0,
        "atr_ok": 0,
        "spread_ok": 0,
        "trend_blocked": 0,
        "rsi_blocked": 0,
        "atr_blocked": 0,
        "spread_blocked": 0,
        "last_score_pct": 0.0,
        "last_rsi": 0.0,
        "last_atr_pct": 0.0,
        "last_spread_pct": 0.0,
        "last_decision": "",
        "last_checked_at": None,
    }


def create_exchange() -> ccxt.Exchange:
    exchange = ccxt.bitvavo({
        "apiKey": os.getenv(
            "BITVAVO_API_KEY",
            "",
        ).strip(),
        "secret": os.getenv(
            "BITVAVO_API_SECRET",
            "",
        ).strip(),
        "enableRateLimit": True,
        "options": {
            "fetchMarkets": {
                "types": ["spot"],
            },
        },
    })

    exchange.load_markets()
    return exchange


def calculate_rsi(
    series: pd.Series,
    length: int,
) -> pd.Series:
    difference = series.diff()

    gains = difference.clip(lower=0)
    losses = -difference.clip(upper=0)

    average_gain = gains.ewm(
        alpha=1 / length,
        adjust=False,
        min_periods=length,
    ).mean()

    average_loss = losses.ewm(
        alpha=1 / length,
        adjust=False,
        min_periods=length,
    ).mean()

    relative_strength = (
        average_gain
        / average_loss.replace(0, pd.NA)
    )

    return 100 - (
        100 / (1 + relative_strength)
    )


def calculate_atr(
    dataframe: pd.DataFrame,
    length: int,
) -> pd.Series:
    previous_close = dataframe["close"].shift(1)

    true_range = pd.concat(
        [
            dataframe["high"] - dataframe["low"],
            (
                dataframe["high"]
                - previous_close
            ).abs(),
            (
                dataframe["low"]
                - previous_close
            ).abs(),
        ],
        axis=1,
    ).max(axis=1)

    return true_range.ewm(
        alpha=1 / length,
        adjust=False,
        min_periods=length,
    ).mean()


def fetch_dataframe(
    exchange: ccxt.Exchange,
    symbol: str,
    timeframe: str,
    limit: int,
) -> pd.DataFrame:
    candles = exchange.fetch_ohlcv(
        symbol,
        timeframe=timeframe,
        limit=limit,
    )

    if not candles:
        raise RuntimeError(
            "geen candles ontvangen"
        )

    dataframe = pd.DataFrame(
        candles,
        columns=[
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ],
    )

    for column in (
        "open",
        "high",
        "low",
        "close",
        "volume",
    ):
        dataframe[column] = pd.to_numeric(
            dataframe[column],
            errors="coerce",
        )

    dataframe.dropna(inplace=True)

    if dataframe.empty:
        raise RuntimeError(
            "geen bruikbare candledata ontvangen"
        )

    return dataframe


def calculate_spread_pct(
    ticker: Dict[str, Any],
) -> float:
    bid = to_float(
        ticker.get("bid"),
        0.0,
    )

    ask = to_float(
        ticker.get("ask"),
        0.0,
    )

    if bid <= 0 or ask <= 0:
        return 999.0

    middle = (bid + ask) / 2.0

    if middle <= 0:
        return 999.0

    return (
        (ask - bid)
        / middle
        * 100.0
    )


def get_symbols(
    config: Dict[str, Any],
) -> List[str]:
    symbols = config.get("symbols") or []

    if not isinstance(symbols, list):
        return []

    return [
        str(symbol).strip().upper()
        for symbol in symbols
        if str(symbol).strip()
    ]


def diagnose_symbol(
    exchange: ccxt.Exchange,
    config: Dict[str, Any],
    symbol: str,
) -> Dict[str, Any]:
    timeframe = str(
        config.get(
            "timeframe",
            "15m",
        )
    )

    candles_limit = int(
        to_float(
            get_cfg(
                config,
                "logging.candles_limit",
                400,
            ),
            400,
        )
    )

    sma_fast_length = int(
        to_float(
            get_cfg(
                config,
                "signals.sma_fast",
                20,
            ),
            20,
        )
    )

    sma_slow_length = int(
        to_float(
            get_cfg(
                config,
                "signals.sma_slow",
                60,
            ),
            60,
        )
    )

    rsi_length = int(
        to_float(
            get_cfg(
                config,
                "signals.rsi_len",
                14,
            ),
            14,
        )
    )

    rsi_min = to_float(
        get_cfg(
            config,
            "signals.rsi_buy_min",
            55,
        ),
        55,
    )

    rsi_max = to_float(
        get_cfg(
            config,
            "signals.rsi_buy_max",
            70,
        ),
        70,
    )

    atr_length = int(
        to_float(
            get_cfg(
                config,
                "signals.atr_len",
                14,
            ),
            14,
        )
    )

    min_atr_pct = to_float(
        get_cfg(
            config,
            "signals.min_atr_pct",
            0.30,
        ),
        0.30,
    )

    max_spread_pct = to_float(
        get_cfg(
            config,
            "risk.max_spread_pct",
            0.25,
        ),
        0.25,
    )

    dataframe = fetch_dataframe(
        exchange,
        symbol,
        timeframe,
        candles_limit,
    )

    required_candles = max(
        sma_slow_length + 2,
        rsi_length + 2,
        atr_length + 2,
    )

    if len(dataframe) < required_candles:
        raise RuntimeError(
            f"te weinig candles: {len(dataframe)}"
        )

    dataframe["sma_fast"] = (
        dataframe["close"]
        .rolling(sma_fast_length)
        .mean()
    )

    dataframe["sma_slow"] = (
        dataframe["close"]
        .rolling(sma_slow_length)
        .mean()
    )

    dataframe["rsi"] = calculate_rsi(
        dataframe["close"],
        rsi_length,
    )

    dataframe["atr"] = calculate_atr(
        dataframe,
        atr_length,
    )

    latest = dataframe.iloc[-1]

    ticker = exchange.fetch_ticker(
        symbol
    )

    close_price = to_float(
        latest["close"],
        0.0,
    )

    sma_fast = to_float(
        latest["sma_fast"],
        0.0,
    )

    sma_slow = to_float(
        latest["sma_slow"],
        0.0,
    )

    rsi_value = to_float(
        latest["rsi"],
        0.0,
    )

    atr_value = to_float(
        latest["atr"],
        0.0,
    )

    atr_pct = (
        atr_value
        / close_price
        * 100.0
        if close_price > 0
        else 0.0
    )

    spread_pct = calculate_spread_pct(
        ticker
    )

    trend_ok = (
        close_price > sma_fast
        and sma_fast > sma_slow
    )

    rsi_ok = (
        rsi_min
        <= rsi_value
        <= rsi_max
    )

    atr_ok = (
        atr_pct >= min_atr_pct
    )

    spread_ok = (
        spread_pct <= max_spread_pct
    )

    passed_checks = sum([
        trend_ok,
        rsi_ok,
        atr_ok,
        spread_ok,
    ])

    score_pct = (
        passed_checks
        / 4
        * 100.0
    )

    if passed_checks == 4:
        decision = "TECHNISCH KOOPSIGNAAL"

    elif passed_checks == 3:
        decision = "BIJNA KOOPSIGNAAL"

    elif passed_checks == 2:
        decision = "MATIG"

    else:
        decision = "GEEN KOOP"

    reasons: List[str] = []

    if not trend_ok:
        reasons.append(
            "trend niet stijgend"
        )

    if not rsi_ok:
        if rsi_value < rsi_min:
            reasons.append(
                f"RSI te laag ({rsi_value:.2f})"
            )
        else:
            reasons.append(
                f"RSI te hoog ({rsi_value:.2f})"
            )

    if not atr_ok:
        reasons.append(
            f"ATR te laag ({atr_pct:.3f}%)"
        )

    if not spread_ok:
        reasons.append(
            f"spread te hoog ({spread_pct:.3f}%)"
        )

    LOG.info(
        "DIAGNOSE %s | score=%d/4 %.0f%% | "
        "trend=%s | RSI=%.2f:%s | ATR=%.3f%%:%s | "
        "spread=%.3f%%:%s | BESLISSING=%s",
        symbol,
        passed_checks,
        score_pct,
        "OK" if trend_ok else "NIET_OK",
        rsi_value,
        "OK" if rsi_ok else "NIET_OK",
        atr_pct,
        "OK" if atr_ok else "NIET_OK",
        spread_pct,
        "OK" if spread_ok else "NIET_OK",
        decision,
    )

    if reasons:
        LOG.info(
            "DIAGNOSE %s | BLOKKADES: %s",
            symbol,
            " ; ".join(reasons),
        )

    return {
        "symbol": symbol,
        "trend_ok": trend_ok,
        "rsi_ok": rsi_ok,
        "atr_ok": atr_ok,
        "spread_ok": spread_ok,
        "score_pct": score_pct,
        "rsi": rsi_value,
        "atr_pct": atr_pct,
        "spread_pct": spread_pct,
        "decision": decision,
    }


def update_statistics(
    stats: Dict[str, Any],
    result: Dict[str, Any],
) -> None:
    symbol = result["symbol"]

    symbols = stats.setdefault(
        "symbols",
        {},
    )

    current = symbols.get(
        symbol,
        default_symbol_stats(),
    )

    current["checks"] = int(
        current.get("checks", 0)
    ) + 1

    if result["decision"] == "TECHNISCH KOOPSIGNAAL":
        current["technical_signals"] = int(
            current.get("technical_signals", 0)
        ) + 1

    if result["decision"] == "BIJNA KOOPSIGNAAL":
        current["near_signals"] = int(
            current.get("near_signals", 0)
        ) + 1

    for filter_name in (
        "trend",
        "rsi",
        "atr",
        "spread",
    ):
        result_key = f"{filter_name}_ok"

        if result[result_key]:
            current[result_key] = int(
                current.get(result_key, 0)
            ) + 1
        else:
            blocked_key = f"{filter_name}_blocked"

            current[blocked_key] = int(
                current.get(blocked_key, 0)
            ) + 1

    current["last_score_pct"] = result["score_pct"]
    current["last_rsi"] = result["rsi"]
    current["last_atr_pct"] = result["atr_pct"]
    current["last_spread_pct"] = result["spread_pct"]
    current["last_decision"] = result["decision"]
    current["last_checked_at"] = now_iso()

    symbols[symbol] = current


def run_diagnosis(
    exchange: ccxt.Exchange,
    config: Dict[str, Any],
    stats: Dict[str, Any],
) -> None:
    symbols = get_symbols(
        config
    )

    if not symbols:
        LOG.warning(
            "Geen symbolen gevonden in config.yaml"
        )
        return

    LOG.info(
        "Diagnoseronde gestart | timeframe=%s | symbolen=%s",
        config.get(
            "timeframe",
            "15m",
        ),
        len(symbols),
    )

    for symbol in symbols:
        try:
            result = diagnose_symbol(
                exchange,
                config,
                symbol,
            )

            update_statistics(
                stats,
                result,
            )

        except Exception as exc:
            LOG.warning(
                "DIAGNOSE %s mislukt: %s",
                symbol,
                exc,
            )

    stats["total_rounds"] = int(
        stats.get(
            "total_rounds",
            0,
        )
    ) + 1

    stats["last_round_at"] = now_iso()

    save_json_atomic(
        DIAG_STATS_FILE,
        stats,
    )

    LOG.info(
        "Diagnoseronde afgerond | statistieken=%s",
        DIAG_STATS_FILE,
    )


def main() -> None:
    config = load_config(
        CFG_FILE
    )

    stats = load_json(
        DIAG_STATS_FILE,
        default_stats(),
    )

    exchange = create_exchange()

    LOG.info(
        "Diamond Diagnose v3 gestart"
    )

    LOG.info(
        "Configuratiebestand: %s",
        CFG_FILE,
    )

    LOG.info(
        "Statistiekbestand: %s",
        DIAG_STATS_FILE,
    )

    LOG.info(
        "Diagnose plaatst geen orders en wijzigt geen posities"
    )

    while True:
        try:
            config = load_config(
                CFG_FILE
            )

            run_diagnosis(
                exchange,
                config,
                stats,
            )

        except Exception as exc:
            LOG.exception(
                "Diagnose-hoofdloop fout: %s",
                exc,
            )

        time.sleep(
            LOOP_SLEEP_SECONDS
        )


if __name__ == "__main__":
    main()
