#!/usr/bin/env python3
"""
Diamond Trader permanente zelftest.

Controleert zonder orders te plaatsen:
- bestanden en Python-syntax;
- configuratie en omgevingsvariabelen;
- actieve processen;
- permanente opslag;
- Bitvavo-leestoegang;
- afgesloten candles;
- gelijke candledata voor bot en diagnose;
- bescherming van handmatig bezit;
- trailing-stop, stop-loss en minimale nettowinst;
- statusbestand en transactiebestand.
"""

import ast
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import ccxt

import diamond_bot
from closed_candle_runner import (
    fetch_closed_bot_dataframe,
    fetch_closed_diagnose_dataframe,
)


FAILURES: list[str] = []


def run_test(
    name: str,
    test_function: Callable[[], None],
) -> None:
    try:
        test_function()
        print(f"GESLAAGD: {name}")

    except Exception as exc:
        message = (
            f"MISLUKT: {name} | "
            f"{type(exc).__name__}: {exc}"
        )

        FAILURES.append(message)
        print(message)


def load_config() -> dict:
    config_path = os.getenv(
        "CFG_FILE",
        "config.yaml",
    ).strip()

    return diamond_bot.load_yaml(
        config_path
    )


def test_files_and_syntax() -> None:
    required_files = [
        "diamond_bot.py",
        "diagnose.py",
        "agent.py",
        "supervisor_agent.py",
        "closed_candle_runner.py",
        "config.yaml",
        "start.sh",
        "requirements.txt",
    ]

    python_files = [
        "diamond_bot.py",
        "diagnose.py",
        "agent.py",
        "supervisor_agent.py",
        "closed_candle_runner.py",
        "selftest.py",
    ]

    for filename in required_files:
        assert Path(filename).is_file(), (
            f"{filename} ontbreekt"
        )

    for filename in python_files:
        source = Path(filename).read_text(
            encoding="utf-8"
        )

        ast.parse(
            source,
            filename=filename,
        )


def test_config() -> None:
    config = load_config()

    dry_run = diamond_bot.to_bool(
        diamond_bot.get_cfg(
            config,
            "dry_run",
            False,
        ),
        False,
    )

    min_atr_pct = diamond_bot.to_float(
        diamond_bot.get_cfg(
            config,
            "signals.min_atr_pct",
            -1,
        ),
        -1,
    )

    assert dry_run is True, (
        "dry_run staat niet op true"
    )

    assert abs(min_atr_pct - 0.30) < 0.000001, (
        f"min_atr_pct is {min_atr_pct}, verwacht 0.30"
    )

    symbols = config.get("symbols") or []

    assert isinstance(symbols, list), (
        "symbols is geen lijst"
    )

    assert len(symbols) > 0, (
        "geen symbolen ingesteld"
    )


def test_environment() -> None:
    required_variables = [
        "BITVAVO_API_KEY",
        "BITVAVO_API_SECRET",
        "BITVAVO_OPERATOR_ID",
        "CFG_FILE",
        "STATE_FILE",
        "TRADES_FILE",
        "CONTROL_FILE",
        "AGENT_STATE_FILE",
    ]

    missing = [
        name
        for name in required_variables
        if not os.getenv(name, "").strip()
    ]

    assert not missing, (
        "ontbrekende variabelen: "
        + ", ".join(missing)
    )


def test_start_script() -> None:
    content = Path("start.sh").read_text(
        encoding="utf-8"
    )

    required_text = [
        "python3 agent.py",
        "closed_candle_runner.py diagnose",
        "python3 supervisor_agent.py",
        "closed_candle_runner.py bot",
        "wait -n",
    ]

    for text in required_text:
        assert text in content, (
            f"start.sh mist: {text}"
        )

    result = subprocess.run(
        [
            "bash",
            "-n",
            "start.sh",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, (
        result.stderr.strip()
        or "start.sh heeft een syntaxfout"
    )


def test_processes() -> None:
    process_list = subprocess.check_output(
        [
            "ps",
            "-eo",
            "args=",
        ],
        text=True,
    )

    required_processes = [
        "python3 agent.py",
        "python3 closed_candle_runner.py diagnose",
        "python3 supervisor_agent.py",
        "python3 closed_candle_runner.py bot",
    ]

    for process in required_processes:
        assert process in process_list, (
            f"proces draait niet: {process}"
        )


def test_persistent_disk() -> None:
    disk_path = Path("/var/data")

    assert disk_path.is_dir(), (
        "/var/data ontbreekt"
    )

    disk_stats = os.statvfs(
        str(disk_path)
    )

    available_bytes = (
        disk_stats.f_bavail
        * disk_stats.f_frsize
    )

    assert available_bytes > 10 * 1024 * 1024, (
        "minder dan 10 MB schijfruimte beschikbaar"
    )

    print(
        "  Beschikbare schijfruimte:",
        round(
            available_bytes
            / 1024
            / 1024,
            1,
        ),
        "MB",
    )


def test_state_and_transactions() -> None:
    config = load_config()

    state_file = str(
        diamond_bot.get_cfg(
            config,
            "files.state_file",
            "/var/data/diamond_state.json",
        )
    )

    trades_file = str(
        diamond_bot.get_cfg(
            config,
            "files.trades_file",
            "/var/data/diamond_transactions.csv",
        )
    )

    assert Path(state_file).is_file(), (
        f"statusbestand ontbreekt: {state_file}"
    )

    assert Path(trades_file).is_file(), (
        f"transactiebestand ontbreekt: {trades_file}"
    )

    with open(
        state_file,
        "r",
        encoding="utf-8",
    ) as file:
        state = json.load(file)

    assert isinstance(
        state.get("positions"),
        dict,
    )

    assert isinstance(
        state.get("short_positions"),
        dict,
    )

    with open(
        trades_file,
        "r",
        encoding="utf-8",
        newline="",
    ) as file:
        rows = list(
            csv.DictReader(file)
        )

    required_columns = {
        "ts",
        "market",
        "side",
        "price",
        "base_amount",
        "quote_amount",
        "fees_quote",
        "net_pnl_quote",
        "reason",
        "dry_run",
    }

    if rows:
        assert required_columns.issubset(
            rows[0].keys()
        ), "transactiebestand mist kolommen"

    balances: dict[str, float] = {}

    for row in rows:
        side = str(
            row.get("side") or ""
        ).upper()

        if side not in {
            "BUY",
            "SELL",
        }:
            continue

        dry_run_value = str(
            row.get("dry_run") or ""
        ).strip().lower()

        assert dry_run_value in {
            "true",
            "1",
            "yes",
        }, (
            "live transactie gevonden tijdens dry-run-test"
        )

        market = str(
            row.get("market") or ""
        )

        amount = float(
            row.get("base_amount") or 0
        )

        balances.setdefault(
            market,
            0.0,
        )

        if side == "BUY":
            balances[market] += amount

        else:
            balances[market] -= amount

        assert balances[market] >= -0.000001, (
            f"meer verkocht dan gekocht voor {market}"
        )

    print(
        "  Transactieregels:",
        len(rows),
    )

    print(
        "  Open spotposities:",
        len(
            state.get("positions") or {}
        ),
    )


def create_public_exchange() -> ccxt.Exchange:
    exchange = ccxt.bitvavo({
        "enableRateLimit": True,
        "timeout": 30000,
        "options": {
            "fetchMarkets": {
                "types": ["spot"],
            },
        },
    })

    exchange.load_markets()

    return exchange


def test_closed_candles() -> None:
    config = load_config()
    exchange = create_public_exchange()

    symbols = [
        diamond_bot.normalize_symbol(
            symbol,
            str(
                config.get(
                    "quote",
                    "EUR",
                )
            ),
        )
        for symbol in (
            config.get("symbols") or []
        )
    ]

    timeframe = str(
        config.get(
            "timeframe",
            "15m",
        )
    )

    timeframe_ms = int(
        exchange.parse_timeframe(
            timeframe
        )
        * 1000
    )

    dummy_bot = SimpleNamespace(
        cfg=config,
        exchange=exchange,
    )

    for symbol in symbols:
        bot_dataframe = (
            fetch_closed_bot_dataframe(
                dummy_bot,
                symbol,
            )
        )

        diagnose_dataframe = (
            fetch_closed_diagnose_dataframe(
                exchange,
                symbol,
                timeframe,
                100,
            )
        )

        bot_timestamp = int(
            bot_dataframe.iloc[-1]["ts"]
            .timestamp()
            * 1000
        )

        diagnose_timestamp = int(
            diagnose_dataframe.iloc[-1][
                "timestamp"
            ]
        )

        assert bot_timestamp == diagnose_timestamp, (
            f"candleverschil voor {symbol}: "
            f"bot={bot_timestamp}, "
            f"diagnose={diagnose_timestamp}"
        )

        candle_close_ms = (
            bot_timestamp
            + timeframe_ms
        )

        now_ms = int(
            exchange.milliseconds()
        )

        assert candle_close_ms <= now_ms, (
            f"{symbol} gebruikt nog een open candle"
        )

        print(
            f"  {symbol}: dezelfde afgesloten candle"
        )


def test_bitvavo_read_access() -> None:
    api_key = os.getenv(
        "BITVAVO_API_KEY",
        "",
    ).strip()

    api_secret = os.getenv(
        "BITVAVO_API_SECRET",
        "",
    ).strip()

    exchange = ccxt.bitvavo({
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
        "timeout": 30000,
    })

    last_error: Exception | None = None

    for attempt in range(1, 4):
        try:
            balance = exchange.fetch_balance()
            break

        except Exception as exc:
            last_error = exc

            if attempt >= 3:
                raise

            time.sleep(
                attempt * 2
            )

    else:
        raise RuntimeError(
            f"saldo ophalen mislukt: {last_error}"
        )

    free = balance.get("free") or {}
    total = balance.get("total") or {}

    free_eur = float(
        free.get("EUR") or 0.0
    )

    assets_with_balance = sum(
        1
        for amount in total.values()
        if isinstance(
            amount,
            (int, float),
        )
        and amount > 0
    )

    print(
        f"  Vrij EUR-saldo: {free_eur:.2f}"
    )

    print(
        "  Assets met saldo:",
        assets_with_balance,
    )


def test_profit_and_stop_safety() -> None:
    config = load_config()

    bot = diamond_bot.Bot.__new__(
        diamond_bot.Bot
    )

    bot.cfg = config
    bot.quote = str(
        config.get(
            "quote",
            "EUR",
        )
    )

    bot.last_hold_log_ts = {}

    position = {
        "opened_by_bot": True,
        "amount": 687.66332,
        "quote_amount": 100.00,
        "fees_buy_quote": 0.25,
    }

    minimum_profit = diamond_bot.to_float(
        diamond_bot.get_cfg(
            config,
            "min_profit_eur",
            1.00,
        ),
        1.00,
    )

    minimum_price = (
        bot.minimum_profitable_exit_price(
            position,
            minimum_profit,
        )
    )

    minimum_pnl = (
        bot.estimated_exit_pnl_quote(
            "ADA/EUR",
            position,
            minimum_price,
        )
    )

    assert minimum_pnl >= (
        minimum_profit
        - 0.0001
    ), (
        "minimale nettowinst wordt niet gehaald"
    )

    bot.get_ticker = lambda symbol: {
        "bid": 0.14574
    }

    losing_trailing = (
        bot.sell_allowed_by_profit(
            "ADA/EUR",
            position,
            "trailing_stop",
        )
    )

    assert losing_trailing is False, (
        "verliesgevende trailing-stop toegestaan"
    )

    bot.get_ticker = lambda symbol: {
        "bid": 0.14000
    }

    normal_stop = (
        bot.sell_allowed_by_profit(
            "ADA/EUR",
            position,
            "stop_loss",
        )
    )

    hard_stop = (
        bot.sell_allowed_by_profit(
            "ADA/EUR",
            position,
            "hard_stop_loss",
        )
    )

    assert normal_stop is True, (
        "normale stop-loss geblokkeerd"
    )

    assert hard_stop is True, (
        "harde stop-loss geblokkeerd"
    )

    bot.get_ticker = lambda symbol: {
        "bid": 0.14800
    }

    profitable_trailing = (
        bot.sell_allowed_by_profit(
            "ADA/EUR",
            position,
            "trailing_stop",
        )
    )

    assert profitable_trailing is True, (
        "winstgevende trailing-stop geblokkeerd"
    )


def test_manual_coin_protection() -> None:
    bot = diamond_bot.Bot.__new__(
        diamond_bot.Bot
    )

    sell_called: list[bool] = []

    def forbidden_sell(
        *args,
        **kwargs,
    ) -> None:
        sell_called.append(
            True
        )

    bot.place_market_sell = (
        forbidden_sell
    )

    manual_position = {
        "opened_by_bot": False,
        "amount": 100.0,
        "entry_price": 1.0,
    }

    bot.try_sell_symbol(
        "ADA/EUR",
        manual_position,
        "stop_loss",
    )

    assert not sell_called, (
        "handmatig gekochte munt werd verkocht"
    )


def main() -> None:
    print(
        "=== DIAMOND TRADER ZELFTEST ==="
    )

    tests = [
        (
            "Bestanden en Python-syntax",
            test_files_and_syntax,
        ),
        (
            "Configuratie",
            test_config,
        ),
        (
            "Omgevingsvariabelen",
            test_environment,
        ),
        (
            "Startscript",
            test_start_script,
        ),
        (
            "Actieve processen",
            test_processes,
        ),
        (
            "Permanente schijf",
            test_persistent_disk,
        ),
        (
            "Status en transacties",
            test_state_and_transactions,
        ),
        (
            "Afgesloten candles",
            test_closed_candles,
        ),
        (
            "Bitvavo-leestoegang",
            test_bitvavo_read_access,
        ),
        (
            "Winst- en stopbeveiliging",
            test_profit_and_stop_safety,
        ),
        (
            "Bescherming handmatig bezit",
            test_manual_coin_protection,
        ),
    ]

    for name, test_function in tests:
        print()
        run_test(
            name,
            test_function,
        )

    print()

    if FAILURES:
        print(
            "=== ZELFTEST MISLUKT ==="
        )

        for failure in FAILURES:
            print(
                failure
            )

        sys.exit(1)

    print(
        "=== ALLE TESTS GESLAAGD ==="
    )

    print(
        "Er zijn geen orders geplaatst "
        "en actieve botbestanden zijn niet gewijzigd."
    )


if __name__ == "__main__":
    main()
