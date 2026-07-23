#!/usr/bin/env python3
"""
Diamond Trader closed-candle runner.

Start de bestaande bot en diagnose met één gedeelde correctie:
indicatoren en koopsignalen gebruiken uitsluitend afgesloten candles.
"""

import os
import sys
import time
from typing import Any

import pandas as pd

import diamond_bot
import diagnose


def fetch_closed_bot_dataframe(
    self: diamond_bot.Bot,
    symbol: str,
) -> pd.DataFrame:
    """
    Haalt candles voor Diamond Bot op.

    De eventueel nog lopende candle wordt verwijderd, zodat indicatoren
    uitsluitend met volledig afgesloten candles worden berekend.
    """
    timeframe = str(
        diamond_bot.get_cfg(
            self.cfg,
            "timeframe",
            "15m",
        )
    )

    limit = int(
        diamond_bot.to_float(
            diamond_bot.get_cfg(
                self.cfg,
                "logging.candles_limit",
                400,
            ),
            400,
        )
    )

    last_error: Exception | None = None

    for attempt in range(1, 5):
        try:
            rows = self.exchange.fetch_ohlcv(
                symbol,
                timeframe=timeframe,
                limit=limit,
            )

            dataframe = pd.DataFrame(
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

            if dataframe.empty:
                raise ValueError(
                    f"Geen candles voor {symbol}"
                )

            for column in [
                "ts",
                "open",
                "high",
                "low",
                "close",
                "volume",
            ]:
                dataframe[column] = pd.to_numeric(
                    dataframe[column],
                    errors="coerce",
                )

            dataframe.dropna(
                inplace=True,
            )

            dataframe.sort_values(
                "ts",
                inplace=True,
            )

            dataframe.drop_duplicates(
                subset=["ts"],
                keep="last",
                inplace=True,
            )

            dataframe.reset_index(
                drop=True,
                inplace=True,
            )

            if dataframe.empty:
                raise ValueError(
                    f"Geen bruikbare candles voor {symbol}"
                )

            timeframe_ms = int(
                self.exchange.parse_timeframe(
                    timeframe
                )
                * 1000
            )

            now_ms = int(
                self.exchange.milliseconds()
            )

            last_start_ms = int(
                dataframe.iloc[-1]["ts"]
            )

            last_close_ms = (
                last_start_ms
                + timeframe_ms
            )

            if last_close_ms > now_ms:
                dataframe = dataframe.iloc[:-1].copy()

            if dataframe.empty:
                raise ValueError(
                    f"Geen afgesloten candles voor {symbol}"
                )

            dataframe["ts"] = pd.to_datetime(
                dataframe["ts"],
                unit="ms",
                utc=True,
            )

            dataframe.reset_index(
                drop=True,
                inplace=True,
            )

            return dataframe

        except Exception as exc:
            last_error = exc

            diamond_bot.LOG.warning(
                "Afgesloten candles ophalen poging %s/4 "
                "mislukt voor %s: %s",
                attempt,
                symbol,
                exc,
            )

            time.sleep(
                2 * attempt
            )

    raise RuntimeError(
        f"Kon afgesloten candles niet ophalen voor "
        f"{symbol}: {last_error}"
    )


def fetch_closed_diagnose_dataframe(
    exchange: Any,
    symbol: str,
    timeframe: str,
    limit: int,
) -> pd.DataFrame:
    """
    Haalt candles voor Diamond Diagnose op.

    Diagnose gebruikt exact dezelfde afgesloten candle als Diamond Bot.
    """
    candles = diagnose.exchange_call_with_retry(
        f"Candles ophalen voor {symbol}",
        lambda: exchange.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            limit=limit,
        ),
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

    for column in [
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]:
        dataframe[column] = pd.to_numeric(
            dataframe[column],
            errors="coerce",
        )

    dataframe.dropna(
        inplace=True,
    )

    dataframe.sort_values(
        "timestamp",
        inplace=True,
    )

    dataframe.drop_duplicates(
        subset=["timestamp"],
        keep="last",
        inplace=True,
    )

    dataframe.reset_index(
        drop=True,
        inplace=True,
    )

    if dataframe.empty:
        raise RuntimeError(
            "geen bruikbare candledata ontvangen"
        )

    timeframe_ms = int(
        exchange.parse_timeframe(
            timeframe
        )
        * 1000
    )

    now_ms = int(
        exchange.milliseconds()
    )

    last_start_ms = int(
        dataframe.iloc[-1]["timestamp"]
    )

    last_close_ms = (
        last_start_ms
        + timeframe_ms
    )

    if last_close_ms > now_ms:
        dataframe = dataframe.iloc[:-1].copy()

    if dataframe.empty:
        raise RuntimeError(
            "geen afgesloten candles beschikbaar"
        )

    dataframe.reset_index(
        drop=True,
        inplace=True,
    )

    return dataframe


def run_bot() -> None:
    """
    Start Diamond Bot met de afgesloten-candlecorrectie.
    """
    diamond_bot.Bot.fetch_ohlcv_df = (
        fetch_closed_bot_dataframe
    )

    cfg_path = os.getenv(
        "CFG_FILE",
        "config.yaml",
    )

    config = diamond_bot.load_yaml(
        cfg_path
    )

    diamond_bot.setup_logging(
        str(
            diamond_bot.get_cfg(
                config,
                "logging.level",
                "INFO",
            )
        )
    )

    bot = diamond_bot.Bot(
        config
    )

    diamond_bot.LOG.info(
        "Diamond Bot v6.4 gestart | "
        "closed_candles=True | "
        "dry_run=%s | state=%s | "
        "trades=%s | control=%s",
        bot.dry_run,
        bot.state_file,
        bot.trades_file,
        bot.control_file,
    )

    bot.run_forever()


def run_diagnose() -> None:
    """
    Start Diamond Diagnose met dezelfde afgesloten-candlecorrectie.
    """
    diagnose.fetch_dataframe = (
        fetch_closed_diagnose_dataframe
    )

    config = diagnose.load_config(
        diagnose.CFG_FILE
    )

    stats = diagnose.load_json(
        diagnose.DIAG_STATS_FILE,
        diagnose.default_stats(),
    )

    stats.setdefault(
        "symbols",
        {},
    )

    stats.setdefault(
        "started_at",
        diagnose.now_iso(),
    )

    stats.setdefault(
        "total_rounds",
        0,
    )

    stats["version"] = 4

    exchange = diagnose.create_exchange()

    diagnose.LOG.info(
        "Diamond Diagnose v4.2 gestart | "
        "closed_candles=True"
    )

    diagnose.LOG.info(
        "Configuratiebestand: %s",
        diagnose.CFG_FILE,
    )

    diagnose.LOG.info(
        "Statistiekbestand: %s",
        diagnose.DIAG_STATS_FILE,
    )

    diagnose.LOG.info(
        "API-retry actief | pogingen=%d | "
        "wachttijden=%s",
        diagnose.API_MAX_ATTEMPTS,
        diagnose.API_RETRY_DELAYS_SECONDS,
    )

    diagnose.LOG.info(
        "Diagnose plaatst geen orders "
        "en wijzigt geen posities"
    )

    while True:
        try:
            config = diagnose.load_config(
                diagnose.CFG_FILE
            )

            diagnose.run_diagnosis(
                exchange,
                config,
                stats,
            )

        except Exception as exc:
            diagnose.LOG.exception(
                "Diagnose-hoofdloop fout: %s",
                exc,
            )

        time.sleep(
            diagnose.LOOP_SLEEP_SECONDS
        )


def main() -> None:
    if (
        len(sys.argv) != 2
        or sys.argv[1] not in {
            "bot",
            "diagnose",
        }
    ):
        raise SystemExit(
            "Gebruik: python3 "
            "closed_candle_runner.py "
            "bot|diagnose"
        )

    if sys.argv[1] == "bot":
        run_bot()

    else:
        run_diagnose()


if __name__ == "__main__":
    main()
