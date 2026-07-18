#!/usr/bin/env python3
"""
Diamond Supervisor v1

De supervisor:
- leest de verzamelde diagnosestatistieken;
- controleert of diagnose en botbestanden actueel zijn;
- maakt verbetervoorstellen;
- past nooit zelfstandig instellingen aan;
- plaatst nooit orders;
- schrijft alleen naar diamond_supervisor_state.json.
"""

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


LOG = logging.getLogger("diamond_supervisor")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

DIAG_STATS_FILE = os.getenv(
    "DIAG_STATS_FILE",
    "/var/data/diamond_diagnose_stats.json",
).strip()

BOT_STATE_FILE = os.getenv(
    "STATE_FILE",
    "/var/data/diamond_state.json",
).strip()

TRADES_FILE = os.getenv(
    "TRADES_FILE",
    "/var/data/diamond_transactions.csv",
).strip()

CONTROL_FILE = os.getenv(
    "CONTROL_FILE",
    "/var/data/diamond_control.json",
).strip()

SUPERVISOR_STATE_FILE = os.getenv(
    "SUPERVISOR_STATE_FILE",
    "/var/data/diamond_supervisor_state.json",
).strip()

CHECK_INTERVAL_SECONDS = 60 * 60
MAX_DIAGNOSE_AGE_MINUTES = 35
MIN_CHECKS_FOR_RECOMMENDATION = 48


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


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


def load_json(
    path_str: str,
    default: Dict[str, Any],
) -> Dict[str, Any]:
    path = Path(path_str)

    if not path.exists():
        return default.copy()

    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)

        if isinstance(data, dict):
            return data

    except Exception as exc:
        LOG.warning(
            "Bestand lezen mislukt %s: %s",
            path_str,
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


def parse_iso(
    value: Any,
) -> Optional[datetime]:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(
            str(value).replace(
                "Z",
                "+00:00",
            )
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(
                tzinfo=timezone.utc,
            )

        return parsed.astimezone(
            timezone.utc
        )

    except ValueError:
        return None


def age_minutes(
    timestamp: Any,
) -> Optional[float]:
    parsed = parse_iso(
        timestamp
    )

    if parsed is None:
        return None

    return (
        now_utc()
        - parsed
    ).total_seconds() / 60.0


def percentage(
    value: int,
    total: int,
) -> float:
    if total <= 0:
        return 0.0

    return (
        value
        / total
        * 100.0
    )


def make_symbol_recommendations(
    symbol: str,
    stats: Dict[str, Any],
) -> List[str]:
    recommendations: List[str] = []

    checks = int(
        stats.get(
            "checks",
            0,
        )
    )

    if checks < MIN_CHECKS_FOR_RECOMMENDATION:
        return [
            (
                f"{symbol}: nog onvoldoende gegevens "
                f"({checks}/{MIN_CHECKS_FOR_RECOMMENDATION} controles)"
            )
        ]

    trend_blocked = int(
        stats.get(
            "trend_blocked",
            0,
        )
    )

    rsi_blocked = int(
        stats.get(
            "rsi_blocked",
            0,
        )
    )

    atr_blocked = int(
        stats.get(
            "atr_blocked",
            0,
        )
    )

    spread_blocked = int(
        stats.get(
            "spread_blocked",
            0,
        )
    )

    near_signals = int(
        stats.get(
            "near_signals",
            0,
        )
    )

    technical_signals = int(
        stats.get(
            "technical_signals",
            0,
        )
    )

    atr_block_pct = percentage(
        atr_blocked,
        checks,
    )

    trend_block_pct = percentage(
        trend_blocked,
        checks,
    )

    rsi_block_pct = percentage(
        rsi_blocked,
        checks,
    )

    spread_block_pct = percentage(
        spread_blocked,
        checks,
    )

    near_signal_pct = percentage(
        near_signals,
        checks,
    )

    if (
        atr_block_pct >= 75.0
        and near_signal_pct >= 15.0
    ):
        recommendations.append(
            (
                f"{symbol}: ATR blokkeert {atr_block_pct:.1f}% "
                f"van de controles en {near_signal_pct:.1f}% is bijna-signaal. "
                "Voorstel: min_atr_pct later voorzichtig testen op 0.20."
            )
        )

    if rsi_block_pct >= 85.0:
        recommendations.append(
            (
                f"{symbol}: RSI blokkeert {rsi_block_pct:.1f}% "
                "van de controles. Eerst markttrend blijven volgen; "
                "nog geen automatische wijziging."
            )
        )

    if trend_block_pct >= 85.0:
        recommendations.append(
            (
                f"{symbol}: trendfilter blokkeert {trend_block_pct:.1f}%. "
                "Dit wijst waarschijnlijk op een zwakke markt; "
                "trendfilter niet versoepelen."
            )
        )

    if spread_block_pct >= 10.0:
        recommendations.append(
            (
                f"{symbol}: spread blokkeert {spread_block_pct:.1f}%. "
                "Controleer liquiditeit voordat deze munt live wordt gebruikt."
            )
        )

    if technical_signals == 0 and near_signals == 0:
        recommendations.append(
            (
                f"{symbol}: nog geen technisch of bijna-koopsignaal "
                f"na {checks} controles."
            )
        )

    if not recommendations:
        recommendations.append(
            (
                f"{symbol}: geen duidelijke afwijking gevonden. "
                "Instellingen voorlopig behouden."
            )
        )

    return recommendations


def build_supervisor_report() -> Dict[str, Any]:
    diagnose_stats = load_json(
        DIAG_STATS_FILE,
        {},
    )

    bot_state = load_json(
        BOT_STATE_FILE,
        {},
    )

    control = load_json(
        CONTROL_FILE,
        {},
    )

    last_round_at = diagnose_stats.get(
        "last_round_at"
    )

    diagnose_age = age_minutes(
        last_round_at
    )

    health: List[str] = []

    if diagnose_age is None:
        health.append(
            "Diagnosestatistieken ontbreken"
        )
    elif diagnose_age > MAX_DIAGNOSE_AGE_MINUTES:
        health.append(
            (
                "Diagnose mogelijk gestopt: laatste ronde "
                f"{diagnose_age:.1f} minuten geleden"
            )
        )
    else:
        health.append(
            (
                "Diagnose actief: laatste ronde "
                f"{diagnose_age:.1f} minuten geleden"
            )
        )

    if Path(BOT_STATE_FILE).exists():
        health.append(
            "Bot-statebestand aanwezig"
        )
    else:
        health.append(
            "WAARSCHUWING: bot-statebestand ontbreekt"
        )

    if Path(CONTROL_FILE).exists():
        health.append(
            "Controlebestand aanwezig"
        )
    else:
        health.append(
            "WAARSCHUWING: controlebestand ontbreekt"
        )

    if Path(TRADES_FILE).exists():
        health.append(
            "Transactiebestand aanwezig"
        )
    else:
        health.append(
            "Nog geen transactiebestand; er zijn waarschijnlijk nog geen trades"
        )

    symbols = diagnose_stats.get(
        "symbols",
        {},
    )

    recommendations: List[str] = []

    if isinstance(symbols, dict):
        for symbol, stats in sorted(
            symbols.items()
        ):
            if not isinstance(stats, dict):
                continue

            recommendations.extend(
                make_symbol_recommendations(
                    symbol,
                    stats,
                )
            )

    report = {
        "version": 1,
        "generated_at": now_iso(),
        "mode": "suggest",
        "total_diagnose_rounds": int(
            diagnose_stats.get(
                "total_rounds",
                0,
            )
        ),
        "open_positions": len(
            bot_state.get(
                "positions",
                {},
            )
            if isinstance(
                bot_state.get(
                    "positions",
                    {},
                ),
                dict,
            )
            else {}
        ),
        "paused": bool(
            control.get(
                "paused",
                False,
            )
        ),
        "pause_reason": control.get(
            "pause_reason",
            "",
        ),
        "health": health,
        "recommendations": recommendations,
    }

    return report


def log_report(
    report: Dict[str, Any],
) -> None:
    LOG.info(
        "SUPERVISOR | rondes=%s | open_posities=%s | paused=%s",
        report.get(
            "total_diagnose_rounds",
            0,
        ),
        report.get(
            "open_positions",
            0,
        ),
        report.get(
            "paused",
            False,
        ),
    )

    for item in report.get(
        "health",
        [],
    ):
        LOG.info(
            "SUPERVISOR GEZONDHEID | %s",
            item,
        )

    for item in report.get(
        "recommendations",
        [],
    ):
        LOG.info(
            "SUPERVISOR ADVIES | %s",
            item,
        )


def main() -> None:
    LOG.info(
        "Diamond Supervisor v1 gestart"
    )

    LOG.info(
        "Modus=suggest | supervisor past niets automatisch aan"
    )

    LOG.info(
        "Supervisorbestand: %s",
        SUPERVISOR_STATE_FILE,
    )

    while True:
        try:
            report = build_supervisor_report()

            save_json_atomic(
                SUPERVISOR_STATE_FILE,
                report,
            )

            log_report(
                report
            )

        except Exception as exc:
            LOG.exception(
                "Supervisor-fout: %s",
                exc,
            )

        time.sleep(
            CHECK_INTERVAL_SECONDS
        )


if __name__ == "__main__":
    main()
