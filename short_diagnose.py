#!/usr/bin/env python3
"""
Diamond Trader - veilige paper-shortdiagnose.

Dit programma:
- gebruikt exact dezelfde shortsignaallogica als diamond_bot.py;
- gebruikt uitsluitend volledig afgesloten candles;
- controleert ook spread, pauze, cooldown en positielimieten;
- plaatst geen orders;
- schrijft geen state-, baseline- of transactiebestanden.

Handmatig uitvoeren:
    python3 short_diagnose.py

Compacte uitvoer:
    python3 short_diagnose.py --compact

Eén munt controleren:
    python3 short_diagnose.py --symbol BTC/EUR
"""

from __future__ import annotations

import argparse
import json
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import diamond_bot
import closed_candle_runner


def yes_no(value: bool) -> str:
    return "JA" if value else "NEE"


def as_float(value: Any, default: float = 0.0) -> float:
    return diamond_bot.to_float(value, default)


def as_bool(value: Any, default: bool = False) -> bool:
    return diamond_bot.to_bool(value, default)


def cfg_value(
    config: Dict[str, Any],
    path: str,
    default: Any = None,
) -> Any:
    return diamond_bot.get_cfg(config, path, default)


def read_json_file(
    path: str,
    default: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    fallback = dict(default or {})
    file_path = Path(path)

    if not file_path.exists():
        return fallback

    try:
        data = json.loads(
            file_path.read_text(encoding="utf-8")
        )
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    return fallback


def create_read_only_bot(
    config: Dict[str, Any],
) -> diamond_bot.Bot:
    """
    Bouwt een Bot-object zonder Bot.__init__ uit te voeren.

    Daardoor worden diamond_state.json en de shortbaseline niet
    aangeraakt. De exchange wordt alleen voor openbare marktdata gebruikt.
    """
    bot = object.__new__(diamond_bot.Bot)

    bot.cfg = config
    bot.quote = str(
        cfg_value(config, "quote", "EUR")
    ).upper()

    bot.dry_run = as_bool(
        cfg_value(config, "risk.dry_run", True),
        True,
    )

    bot.state_file = str(
        cfg_value(
            config,
            "files.state_file",
            "/var/data/diamond_state.json",
        )
    )

    bot.trades_file = str(
        cfg_value(
            config,
            "files.trades_file",
            "/var/data/diamond_transactions.csv",
        )
    )

    bot.control_file = str(
        cfg_value(
            config,
            "files.control_file",
            "/var/data/diamond_control.json",
        )
    )

    bot.short_test_baseline_file = str(
        cfg_value(
            config,
            "files.short_test_baseline_file",
            "/var/data/diamond_short_test_baseline.json",
        )
    )

    bot.state = diamond_bot.load_state(
        bot.state_file
    )

    bot.short_strategy_baseline_mismatch = False

    bot.exchange = diamond_bot.ccxt.bitvavo(
        {
            "enableRateLimit": True,
            "options": {
                "fetchMarkets": {
                    "types": ["spot"],
                }
            },
        }
    )

    bot.exchange.load_markets()

    # Exact dezelfde afgesloten-candlefunctie als de actieve bot.
    bot.fetch_ohlcv_df = types.MethodType(
        closed_candle_runner.fetch_closed_bot_dataframe,
        bot,
    )

    return bot


def inspect_baseline(
    bot: diamond_bot.Bot,
) -> Dict[str, Any]:
    baseline = read_json_file(
        bot.short_test_baseline_file
    )

    configured_version = str(
        cfg_value(
            bot.cfg,
            "short.strategy_version",
            "",
        )
        or ""
    ).strip()

    settings = baseline.get("settings")
    if not isinstance(settings, dict):
        settings = {}

    baseline_version = str(
        settings.get("strategy_version")
        or ""
    ).strip()

    start = int(
        baseline.get("start_short_trades", 0)
        or 0
    )

    target_total = int(
        baseline.get(
            "target_total_short_trades",
            0,
        )
        or 0
    )

    current = int(
        bot.state.get("short_trades", 0)
        or 0
    )

    open_shorts = len(
        bot.state.get("short_positions", {})
        or {}
    )

    test_started = (
        current > start
        or open_shorts > 0
    )

    mismatch = bool(
        configured_version
        and baseline_version
        and configured_version != baseline_version
    )

    mismatch_blocks = (
        mismatch
        and test_started
    )

    valid = (
        start >= 0
        and target_total > start
    )

    return {
        "file_exists": Path(
            bot.short_test_baseline_file
        ).exists(),
        "configured_version": configured_version,
        "baseline_version": baseline_version,
        "mismatch": mismatch,
        "mismatch_blocks": mismatch_blocks,
        "test_started": test_started,
        "start_short_trades": start,
        "current_short_trades": current,
        "target_total_short_trades": target_total,
        "new_short_trades": max(
            0,
            current - start,
        ),
        "remaining_short_trades": (
            max(0, target_total - current)
            if valid
            else None
        ),
        "target_reached": bool(
            valid
            and current >= target_total
        ),
    }


def inspect_global_status(
    bot: diamond_bot.Bot,
    baseline: Dict[str, Any],
) -> Dict[str, Any]:
    config = bot.cfg

    control = diamond_bot.load_control(
        bot.control_file
    )

    paused = as_bool(
        control.get("paused"),
        False,
    )

    pause_reason = str(
        control.get("pause_reason")
        or ""
    )

    long_test_pause = (
        paused
        and pause_reason.startswith("testdoel_")
        and pause_reason.endswith(
            "_trades_bereikt"
        )
    )

    safety_pause_blocks_shorts = (
        paused
        and not long_test_pause
    )

    short_signals_enabled = as_bool(
        cfg_value(
            config,
            "trading.enable_short_signals",
            False,
        ),
        False,
    )

    short_module_enabled = as_bool(
        cfg_value(
            config,
            "short.enabled",
            False,
        ),
        False,
    )

    paper_only = as_bool(
        cfg_value(
            config,
            "short.paper_only",
            True,
        ),
        True,
    )

    open_spots = len(
        bot.state.get("positions", {})
        or {}
    )

    open_shorts = len(
        bot.state.get("short_positions", {})
        or {}
    )

    max_open_shorts = max(
        0,
        int(
            as_float(
                cfg_value(
                    config,
                    "short.max_open_positions",
                    1,
                ),
                1,
            )
        ),
    )

    max_total_positions = max(
        1,
        int(
            as_float(
                cfg_value(
                    config,
                    "trading.max_total_positions",
                    5,
                ),
                5,
            )
        ),
    )

    total_open = (
        open_spots
        + open_shorts
    )

    blockers: List[str] = []

    if not short_signals_enabled:
        blockers.append(
            "trading.enable_short_signals staat uit"
        )

    if not short_module_enabled:
        blockers.append(
            "short.enabled staat uit"
        )

    if not paper_only:
        blockers.append(
            "short.paper_only staat niet op true"
        )

    if safety_pause_blocks_shorts:
        blockers.append(
            "veiligheidspauze blokkeert nieuwe shorts"
        )

    if baseline.get("mismatch_blocks"):
        blockers.append(
            "shortbaseline hoort bij een andere strategie"
        )

    if baseline.get("target_reached"):
        blockers.append(
            "paper-shorttestdoel is bereikt"
        )

    if open_shorts >= max_open_shorts:
        blockers.append(
            "maximum aantal open shorts is bereikt"
        )

    if total_open >= max_total_positions:
        blockers.append(
            "maximum totaal aantal posities is bereikt"
        )

    return {
        "strategy_version": str(
            cfg_value(
                config,
                "short.strategy_version",
                "",
            )
            or ""
        ),
        "dry_run": bot.dry_run,
        "short_signals_enabled": short_signals_enabled,
        "short_module_enabled": short_module_enabled,
        "paper_only": paper_only,
        "paused": paused,
        "pause_reason": pause_reason,
        "long_test_pause": long_test_pause,
        "safety_pause_blocks_shorts": (
            safety_pause_blocks_shorts
        ),
        "open_spots": open_spots,
        "open_shorts": open_shorts,
        "total_open": total_open,
        "max_open_shorts": max_open_shorts,
        "max_total_positions": max_total_positions,
        "blockers": blockers,
    }


def cooldown_remaining_minutes(
    bot: diamond_bot.Bot,
    symbol: str,
) -> float:
    raw_timestamp = (
        bot.state.get("short_cooldown", {})
        or {}
    ).get(symbol)

    if not raw_timestamp:
        return 0.0

    try:
        timestamp = float(raw_timestamp)
    except (TypeError, ValueError):
        return 0.0

    cooldown_minutes = as_float(
        cfg_value(
            bot.cfg,
            "short.cooldown_minutes",
            60,
        ),
        60.0,
    )

    elapsed = max(
        0.0,
        (time.time() - timestamp) / 60.0,
    )

    return max(
        0.0,
        cooldown_minutes - elapsed,
    )


def inspect_symbol(
    bot: diamond_bot.Bot,
    symbol: str,
    global_status: Dict[str, Any],
) -> Dict[str, Any]:
    technical = bot.short_entry_diagnostics(
        symbol
    )

    ticker = bot.exchange.fetch_ticker(
        symbol
    )

    bid = as_float(
        ticker.get("bid"),
        0.0,
    )

    ask = as_float(
        ticker.get("ask"),
        0.0,
    )

    spread_pct = bot.estimate_spread_pct(
        ticker
    )

    max_spread_pct = as_float(
        cfg_value(
            bot.cfg,
            "risk.max_spread_pct",
            0.25,
        ),
        0.25,
    )

    spread_ok = bool(
        bid > 0
        and ask > 0
        and spread_pct <= max_spread_pct
    )

    positions = (
        bot.state.get("positions", {})
        or {}
    )

    short_positions = (
        bot.state.get("short_positions", {})
        or {}
    )

    allow_both = as_bool(
        cfg_value(
            bot.cfg,
            "trading.allow_long_and_short_same_symbol",
            False,
        ),
        False,
    )

    cooldown_remaining = (
        cooldown_remaining_minutes(
            bot,
            symbol,
        )
    )

    symbol_blockers: List[str] = []

    if symbol in short_positions:
        symbol_blockers.append(
            "er staat al een paper short open"
        )

    if (
        not allow_both
        and symbol in positions
    ):
        symbol_blockers.append(
            "er staat al een longpositie op deze munt"
        )

    if cooldown_remaining > 0:
        symbol_blockers.append(
            f"shortcooldown nog {cooldown_remaining:.1f} minuten"
        )

    if bid <= 0 or ask <= 0:
        symbol_blockers.append(
            "geldige bied- of laatprijs ontbreekt"
        )
    elif not spread_ok:
        symbol_blockers.append(
            (
                f"spread {spread_pct:.4f}% hoger dan "
                f"{max_spread_pct:.4f}%"
            )
        )

    technical_signal = as_bool(
        technical.get("signal"),
        False,
    )

    trigger_ok = bool(
        technical.get("entry_trigger")
    )

    rsi_value = as_float(
        technical.get("rsi"),
        0.0,
    )

    rsi_min = as_float(
        technical.get("rsi_min"),
        25.0,
    )

    rsi_max = as_float(
        technical.get("rsi_max"),
        45.0,
    )

    rsi_ok = (
        rsi_min <= rsi_value <= rsi_max
    )

    atr_value = as_float(
        technical.get("atr_pct"),
        0.0,
    )

    min_atr = as_float(
        technical.get("min_atr_pct"),
        0.30,
    )

    atr_ok = (
        atr_value >= min_atr
        and as_float(
            technical.get("atr"),
            0.0,
        ) > 0
    )

    logical_missing: List[str] = []

    if not trigger_ok:
        logical_missing.append(
            "geen crossover of breakout"
        )

    if not rsi_ok:
        logical_missing.append(
            "RSI buiten toegestaan bereik"
        )

    if not atr_ok:
        logical_missing.append(
            "ATR te laag of ongeldig"
        )

    if not spread_ok:
        logical_missing.append(
            "spread niet akkoord"
        )

    logical_missing.extend(
        symbol_blockers
    )

    global_blockers = list(
        global_status.get("blockers", [])
    )

    ready = bool(
        not global_blockers
        and not symbol_blockers
        and technical_signal
        and spread_ok
    )

    if global_blockers:
        status = "GEBLOKKEERD"
    elif ready:
        status = "SHORTSIGNAAL"
    elif len(logical_missing) == 1:
        status = "BIJNA"
    else:
        status = "WACHT"

    return {
        "symbol": symbol,
        "status": status,
        "ready": ready,
        "technical_signal": technical_signal,
        "entry_trigger": str(
            technical.get("entry_trigger")
            or ""
        ),
        "last_candle": str(
            technical.get("last_candle")
            or ""
        ),
        "close": as_float(
            technical.get("close"),
            0.0,
        ),
        "trend_ok": as_bool(
            technical.get("trend_ok"),
            False,
        ),
        "cross_down": as_bool(
            technical.get("cross_down"),
            False,
        ),
        "breakout_down": as_bool(
            technical.get("breakout_down"),
            False,
        ),
        "fast_sma_falling": as_bool(
            technical.get(
                "fast_sma_falling"
            ),
            False,
        ),
        "breakout_level": as_float(
            technical.get("breakout_level"),
            0.0,
        ),
        "breakout_lookback_candles": int(
            as_float(
                technical.get(
                    "breakout_lookback_candles"
                ),
                0,
            )
        ),
        "rsi": rsi_value,
        "rsi_min": rsi_min,
        "rsi_max": rsi_max,
        "rsi_ok": rsi_ok,
        "atr_pct": atr_value,
        "min_atr_pct": min_atr,
        "atr_ok": atr_ok,
        "bid": bid,
        "ask": ask,
        "spread_pct": spread_pct,
        "max_spread_pct": max_spread_pct,
        "spread_ok": spread_ok,
        "cooldown_remaining_minutes": (
            cooldown_remaining
        ),
        "technical_blockers": list(
            technical.get("blockers", [])
            or []
        ),
        "symbol_blockers": symbol_blockers,
        "global_blockers": global_blockers,
        "logical_missing": logical_missing,
    }


def selected_symbols(
    bot: diamond_bot.Bot,
    requested: List[str],
) -> List[str]:
    if requested:
        result: List[str] = []

        for value in requested:
            symbol = diamond_bot.normalize_symbol(
                value,
                bot.quote,
            )

            if symbol not in result:
                result.append(symbol)

        return result

    return bot.scanned_symbols()


def format_trigger(trigger: str) -> str:
    labels = {
        "bearish_crossover": "bearish crossover",
        "bearish_breakout": "bearish breakout",
        "sma_filter_disabled": "SMA-filter uit",
    }
    return labels.get(
        trigger,
        trigger or "-",
    )


def print_compact(
    global_status: Dict[str, Any],
    baseline: Dict[str, Any],
    results: List[Dict[str, Any]],
) -> None:
    print(
        "PAPER-SHORTDIAGNOSE | "
        f"strategie={global_status['strategy_version']} | "
        f"shorts={baseline.get('new_short_trades', 0)}/"
        f"{baseline.get('target_total_short_trades', 0) - baseline.get('start_short_trades', 0)} | "
        f"open={global_status['open_shorts']}/"
        f"{global_status['max_open_shorts']} | "
        f"globaal={'OK' if not global_status['blockers'] else 'GEBLOKKEERD'}"
    )

    for item in results:
        blockers = (
            item["global_blockers"]
            + item["logical_missing"]
        )

        blocker_text = (
            "; ".join(dict.fromkeys(blockers))
            if blockers
            else "-"
        )

        print(
            f"{item['symbol']} | {item['status']} | "
            f"trend={yes_no(item['trend_ok'])} "
            f"cross={yes_no(item['cross_down'])} "
            f"breakout={yes_no(item['breakout_down'])} "
            f"sma_daalt={yes_no(item['fast_sma_falling'])} | "
            f"RSI={item['rsi']:.1f} "
            f"{'OK' if item['rsi_ok'] else 'NEE'} | "
            f"ATR={item['atr_pct']:.3f}% "
            f"{'OK' if item['atr_ok'] else 'NEE'} | "
            f"spread={item['spread_pct']:.4f}% "
            f"{'OK' if item['spread_ok'] else 'NEE'} | "
            f"blokkade={blocker_text}"
        )


def print_detailed(
    global_status: Dict[str, Any],
    baseline: Dict[str, Any],
    results: List[Dict[str, Any]],
) -> None:
    print()
    print("=" * 64)
    print(" DIAMOND TRADER PAPER-SHORTDIAGNOSE")
    print(
        " "
        + datetime.now(
            timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")
    )
    print("=" * 64)

    print()
    print("ALGEMEEN")
    print("-" * 64)
    print(
        f"Strategie          : "
        f"{global_status['strategy_version'] or '-'}"
    )
    print(
        f"Dry-run            : "
        f"{yes_no(global_status['dry_run'])}"
    )
    print(
        f"Shortsignalen aan  : "
        f"{yes_no(global_status['short_signals_enabled'])}"
    )
    print(
        f"Shortmodule aan    : "
        f"{yes_no(global_status['short_module_enabled'])}"
    )
    print(
        f"Paper only         : "
        f"{yes_no(global_status['paper_only'])}"
    )
    print(
        f"Bot gepauzeerd     : "
        f"{yes_no(global_status['paused'])}"
    )
    print(
        f"Open shorts        : "
        f"{global_status['open_shorts']}/"
        f"{global_status['max_open_shorts']}"
    )
    print(
        f"Open totaal        : "
        f"{global_status['total_open']}/"
        f"{global_status['max_total_positions']}"
    )

    target_count = (
        baseline.get(
            "target_total_short_trades",
            0,
        )
        - baseline.get(
            "start_short_trades",
            0,
        )
    )

    print(
        f"Shorttest          : "
        f"{baseline.get('new_short_trades', 0)}/"
        f"{max(0, target_count)}"
    )

    print(
        f"Baselineversie     : "
        f"{baseline.get('baseline_version') or '-'}"
    )

    if global_status["blockers"]:
        print("Globale blokkades  :")
        for blocker in global_status["blockers"]:
            print(f"  - {blocker}")
    else:
        print("Globale blokkades  : geen")

    for item in results:
        print()
        print(item["symbol"])
        print("-" * 64)
        print(
            f"Status             : {item['status']}"
        )
        print(
            f"Laatste candle     : "
            f"{item['last_candle'] or '-'}"
        )
        print(
            f"Slotkoers          : {item['close']:.8f}"
        )
        print(
            f"Bearish trend      : "
            f"{yes_no(item['trend_ok'])}"
        )
        print(
            f"Nieuwe crossover  : "
            f"{yes_no(item['cross_down'])}"
        )
        print(
            f"Nieuwe breakout   : "
            f"{yes_no(item['breakout_down'])}"
        )
        print(
            f"Snelle SMA daalt  : "
            f"{yes_no(item['fast_sma_falling'])}"
        )
        print(
            f"Breakoutniveau    : "
            f"{item['breakout_level']:.8f} "
            f"({item['breakout_lookback_candles']} candles)"
        )
        print(
            f"RSI                : "
            f"{item['rsi']:.2f} "
            f"[{item['rsi_min']:.2f}-"
            f"{item['rsi_max']:.2f}] "
            f"{'OK' if item['rsi_ok'] else 'NIET OK'}"
        )
        print(
            f"ATR                : "
            f"{item['atr_pct']:.3f}% "
            f"[min {item['min_atr_pct']:.3f}%] "
            f"{'OK' if item['atr_ok'] else 'NIET OK'}"
        )
        print(
            f"Spread             : "
            f"{item['spread_pct']:.4f}% "
            f"[max {item['max_spread_pct']:.4f}%] "
            f"{'OK' if item['spread_ok'] else 'NIET OK'}"
        )
        print(
            f"Instaptrigger      : "
            f"{format_trigger(item['entry_trigger'])}"
        )

        all_blockers = (
            item["global_blockers"]
            + item["technical_blockers"]
            + item["symbol_blockers"]
        )

        unique_blockers = list(
            dict.fromkeys(all_blockers)
        )

        if unique_blockers:
            print("Blokkades          :")
            for blocker in unique_blockers:
                print(f"  - {blocker}")
        else:
            print(
                "Blokkades          : geen; "
                "paper short kan worden geopend"
            )

    print()
    print("=" * 64)
    print(
        " LEESCONTROLE AFGEROND - "
        "GEEN ORDERS EN GEEN BESTANDEN GEWIJZIGD"
    )
    print("=" * 64)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Veilige, alleen-lezen diagnose van "
            "Diamond Traders paper-shortvoorwaarden."
        )
    )

    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Pad naar config.yaml (standaard: config.yaml)",
    )

    parser.add_argument(
        "--symbol",
        action="append",
        default=[],
        help=(
            "Controleer één munt. Mag meerdere keren "
            "worden gebruikt, bijvoorbeeld --symbol BTC/EUR."
        ),
    )

    parser.add_argument(
        "--compact",
        action="store_true",
        help="Toon één compacte regel per munt.",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="Toon het volledige rapport als JSON.",
    )

    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        config = diamond_bot.load_yaml(
            args.config
        )

        bot = create_read_only_bot(
            config
        )

        baseline = inspect_baseline(
            bot
        )

        bot.short_strategy_baseline_mismatch = as_bool(
            baseline.get("mismatch_blocks"),
            False,
        )

        global_status = inspect_global_status(
            bot,
            baseline,
        )

        symbols = selected_symbols(
            bot,
            args.symbol,
        )

        results: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []

        for symbol in symbols:
            try:
                results.append(
                    inspect_symbol(
                        bot,
                        symbol,
                        global_status,
                    )
                )
            except Exception as exc:
                errors.append(
                    {
                        "symbol": symbol,
                        "error": str(exc),
                    }
                )

        report = {
            "generated_at": datetime.now(
                timezone.utc
            ).isoformat(),
            "read_only": True,
            "orders_placed": False,
            "files_modified": False,
            "global": global_status,
            "baseline": baseline,
            "symbols": results,
            "errors": errors,
        }

        if args.json:
            print(
                json.dumps(
                    report,
                    indent=2,
                    ensure_ascii=False,
                )
            )
        elif args.compact:
            print_compact(
                global_status,
                baseline,
                results,
            )

            for error in errors:
                print(
                    f"{error['symbol']} | FOUT | "
                    f"{error['error']}"
                )
        else:
            print_detailed(
                global_status,
                baseline,
                results,
            )

            if errors:
                print()
                print("FOUTEN")
                print("-" * 64)
                for error in errors:
                    print(
                        f"{error['symbol']}: "
                        f"{error['error']}"
                    )

        if not results and errors:
            return 1

        return 0

    except Exception as exc:
        print(
            f"FOUT: shortdiagnose kon niet starten: {exc}"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
