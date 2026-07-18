#!/usr/bin/env python3
"""
Diamond Diagnose v2

Dit programma:
- leest dezelfde config.yaml als diamond_bot.py;
- controleert per munt trend, RSI, ATR en spread;
- toont een duidelijke koopscore;
- legt uit waarom niet wordt gekocht;
- plaatst nooit orders;
- wijzigt geen posities of botbestanden.
"""

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

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

LOOP_SLEEP_SECONDS = 15 * 60


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value

    if value is None:
        return default

    normalized = str(value).strip().lower()

    if normalized in {
        "1",
        "true",
        "yes",
        "ja",
        "aan",
        "on",
    }:
        return True

    if normalized in {
        "0",
        "false",
        "no",
        "nee",
        "uit",
        "off",
    }:
        return False

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


def load_config(path_str: str) -> Dict[str, Any]:
    path = Path(path_str)

    if not path.exists():
        raise FileNotFoundError(
            f"Configuratiebestand ontbreekt: {path_str}"
        )

    with path.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file) or {}

    if not isinstance(config, dict):
        raise ValueError(
            "config.yaml bevat geen geldige YAML-structuur"
        )

    return config


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


def result_label(
    passed_checks: int,
    total_checks: int,
) -> str:
    if total_checks <= 0:
        return "ONBEKEND"

    percentage = (
        passed_checks
        / total_checks
        * 100.0
    )

    if percentage >= 100:
        return "TECHNISCH KOOPSIGNAAL"

    if percentage >= 75:
        return "BIJNA KOOPSIGNAAL"

    if percentage >= 50:
        return "MATIG"

    return "GEEN KOOP"


def check_result(
    name: str,
    passed: bool,
    detail: str,
) -> Tuple[str, bool]:
    status = "OK" if passed else "NIET_OK"

    return (
        f"{name}={detail}:{status}",
        passed,
    )


def diagnose_symbol(
    exchange: ccxt.Exchange,
    config: Dict[str, Any],
    symbol: str,
) -> None:
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

    use_sma = to_bool(
        get_cfg(
            config,
            "signals.use_sma",
            True,
        ),
        True,
    )

    use_rsi = to_bool(
        get_cfg(
            config,
            "signals.use_rsi",
            True,
        ),
        True,
    )

    use_atr = to_bool(
        get_cfg(
            config,
            "signals.use_atr_filter",
            True,
        ),
        True,
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

    checks: List[str] = []
    reasons: List[str] = []

    passed_checks = 0
    total_checks = 0

    if use_sma:
        total_checks += 1

        trend_ok = (
            close_price > sma_fast
            and sma_fast > sma_slow
        )

        if trend_ok:
            passed_checks += 1
        else:
            reasons.append(
                "trend niet stijgend "
                f"(koers={close_price:.8f}, "
                f"SMA{sma_fast_length}={sma_fast:.8f}, "
                f"SMA{sma_slow_length}={sma_slow:.8f})"
            )

        check_text, _ = check_result(
            "trend",
            trend_ok,
            (
                "stijgend"
                if trend_ok
                else "dalend"
            ),
        )

        checks.append(check_text)

    if use_rsi:
        total_checks += 1

        rsi_ok = (
            rsi_min
            <= rsi_value
            <= rsi_max
        )

        if rsi_ok:
            passed_checks += 1
        elif rsi_value < rsi_min:
            reasons.append(
                f"RSI te laag "
                f"({rsi_value:.2f} < {rsi_min:.2f})"
            )
        else:
            reasons.append(
                f"RSI te hoog "
                f"({rsi_value:.2f} > {rsi_max:.2f})"
            )

        check_text, _ = check_result(
            "RSI",
            rsi_ok,
            f"{rsi_value:.2f}",
        )

        checks.append(check_text)

    if use_atr:
        total_checks += 1

        atr_ok = (
            atr_pct >= min_atr_pct
        )

        if atr_ok:
            passed_checks += 1
        else:
            reasons.append(
                "beweging te klein "
                f"({atr_pct:.3f}% < {min_atr_pct:.3f}%)"
            )

        check_text, _ = check_result(
            "ATR",
            atr_ok,
            f"{atr_pct:.3f}%",
        )

        checks.append(check_text)

    total_checks += 1

    spread_ok = (
        spread_pct <= max_spread_pct
    )

    if spread_ok:
        passed_checks += 1
    else:
        reasons.append(
            "spread te groot "
            f"({spread_pct:.3f}% > {max_spread_pct:.3f}%)"
        )

    check_text, _ = check_result(
        "spread",
        spread_ok,
        f"{spread_pct:.3f}%",
    )

    checks.append(check_text)

    score_pct = (
        passed_checks
        / total_checks
        * 100.0
        if total_checks > 0
        else 0.0
    )

    decision = result_label(
        passed_checks,
        total_checks,
    )

    LOG.info(
        "DIAGNOSE %s | score=%d/%d %.0f%% | %s | BESLISSING=%s",
        symbol,
        passed_checks,
        total_checks,
        score_pct,
        " | ".join(checks),
        decision,
    )

    if reasons:
        LOG.info(
            "DIAGNOSE %s | BLOKKADES: %s",
            symbol,
            " ; ".join(reasons),
        )

    if decision == "BIJNA KOOPSIGNAAL":
        LOG.info(
            "DIAGNOSE %s | LET OP: slechts één voorwaarde blokkeert de koop",
            symbol,
        )


def run_diagnosis(
    exchange: ccxt.Exchange,
    config: Dict[str, Any],
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
            diagnose_symbol(
                exchange,
                config,
                symbol,
            )
        except Exception as exc:
            LOG.warning(
                "DIAGNOSE %s mislukt: %s",
                symbol,
                exc,
            )

    LOG.info(
        "Diagnoseronde afgerond"
    )


def main() -> None:
    config = load_config(
        CFG_FILE
    )

    exchange = create_exchange()

    LOG.info(
        "Diamond Diagnose v2 gestart"
    )

    LOG.info(
        "Configuratiebestand: %s",
        CFG_FILE,
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
