#!/usr/bin/env python3
"""
Diamond Supervisor v2.1

De supervisor:
- controleert of diagnose, bot-state en veiligheidscontrole actief zijn;
- leest diagnosestatistieken per munt;
- geeft adviezen nadat voldoende metingen zijn verzameld;
- schrijft alleen naar diamond_supervisor_state.json;
- wijzigt config.yaml niet;
- plaatst geen orders;
- kan bestaande munten niet verkopen.
"""

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# ============================================================
# Logging
# ============================================================

LOG = logging.getLogger("diamond_supervisor")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


# ============================================================
# Bestanden en instellingen
# ============================================================

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

AGENT_STATE_FILE = os.getenv(
    "AGENT_STATE_FILE",
    "/var/data/diamond_agent_state.json",
).strip()

SUPERVISOR_STATE_FILE = os.getenv(
    "SUPERVISOR_STATE_FILE",
    "/var/data/diamond_supervisor_state.json",
).strip()

# Iedere 30 minuten controleren
CHECK_INTERVAL_SECONDS = 30 * 60

# Diagnose hoort iedere 15 minuten te draaien.
# Na 40 minuten zonder update volgt een waarschuwing.
MAX_DIAGNOSE_AGE_MINUTES = 40

# Minimaal 48 controles per munt voordat advies wordt gegeven.
MIN_CHECKS_FOR_RECOMMENDATION = 48


# ============================================================
# Algemene hulpfuncties
# ============================================================

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


def to_int(
    value: Any,
    default: int = 0,
) -> int:
    try:
        if value is None or value == "":
            return default

        return int(value)

    except (TypeError, ValueError):
        return default


def to_bool(
    value: Any,
    default: bool = False,
) -> bool:
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


def percentage(
    value: int,
    total: int,
) -> float:
    if total <= 0:
        return 0.0

    return value / total * 100.0


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
            "JSON lezen mislukt voor %s: %s",
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
        now_utc() - parsed
    ).total_seconds() / 60.0


def file_age_minutes(
    path_str: str,
) -> Optional[float]:
    path = Path(path_str)

    if not path.exists():
        return None

    try:
        modified = datetime.fromtimestamp(
            path.stat().st_mtime,
            tz=timezone.utc,
        )

        return (
            now_utc() - modified
        ).total_seconds() / 60.0

    except OSError:
        return None


# ============================================================
# Adviezen per munt
# ============================================================

def make_symbol_recommendations(
    symbol: str,
    stats: Dict[str, Any],
) -> List[str]:
    recommendations: List[str] = []

    checks = to_int(
        stats.get("checks"),
        0,
    )

    if checks < MIN_CHECKS_FOR_RECOMMENDATION:
        recommendations.append(
            (
                f"{symbol}: nog onvoldoende metingen "
                f"({checks}/{MIN_CHECKS_FOR_RECOMMENDATION})."
            )
        )

        return recommendations

    trend_blocked = to_int(
        stats.get("trend_blocked"),
        0,
    )

    rsi_blocked = to_int(
        stats.get("rsi_blocked"),
        0,
    )

    atr_blocked = to_int(
        stats.get("atr_blocked"),
        0,
    )

    spread_blocked = to_int(
        stats.get("spread_blocked"),
        0,
    )

    near_signals = to_int(
        stats.get("near_signals"),
        0,
    )

    technical_signals = to_int(
        stats.get("technical_signals"),
        0,
    )

    trend_block_pct = percentage(
        trend_blocked,
        checks,
    )

    rsi_block_pct = percentage(
        rsi_blocked,
        checks,
    )

    atr_block_pct = percentage(
        atr_blocked,
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

    technical_signal_pct = percentage(
        technical_signals,
        checks,
    )

    if trend_block_pct >= 80.0:
        recommendations.append(
            (
                f"{symbol}: trend blokkeerde "
                f"{trend_block_pct:.1f}% van de controles. "
                "Trendfilter niet versoepelen; markt is waarschijnlijk zwak."
            )
        )

    if rsi_block_pct >= 80.0:
        recommendations.append(
            (
                f"{symbol}: RSI blokkeerde "
                f"{rsi_block_pct:.1f}% van de controles. "
                "Voorlopig niet automatisch aanpassen."
            )
        )

    if (
        atr_block_pct >= 70.0
        and near_signal_pct >= 10.0
    ):
        recommendations.append(
            (
                f"{symbol}: ATR blokkeerde "
                f"{atr_block_pct:.1f}% en "
                f"{near_signal_pct:.1f}% was bijna-koopsignaal. "
                "Later in dry-run min_atr_pct voorzichtig lager testen."
            )
        )

    if spread_block_pct >= 10.0:
        recommendations.append(
            (
                f"{symbol}: spread blokkeerde "
                f"{spread_block_pct:.1f}% van de controles. "
                "Liquiditeit controleren voordat deze munt live wordt gebruikt."
            )
        )

    if technical_signal_pct >= 25.0:
        recommendations.append(
            (
                f"{symbol}: technisch koopsignaal bij "
                f"{technical_signal_pct:.1f}% van de controles. "
                "Controleer of de strategie niet te ruim staat."
            )
        )

    if (
        technical_signals == 0
        and near_signals == 0
    ):
        recommendations.append(
            (
                f"{symbol}: na {checks} controles nog geen "
                "technisch of bijna-koopsignaal."
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


# ============================================================
# Gezondheidscontrole
# ============================================================

def build_health_report(
    diagnose_stats: Dict[str, Any],
) -> List[str]:
    health: List[str] = []

    last_round_at = diagnose_stats.get(
        "last_round_at"
    )

    diagnose_age = age_minutes(
        last_round_at
    )

    if diagnose_age is None:
        health.append(
            "WAARSCHUWING: diagnosestatistieken ontbreken"
        )

    elif diagnose_age > MAX_DIAGNOSE_AGE_MINUTES:
        health.append(
            (
                "WAARSCHUWING: diagnose mogelijk gestopt; "
                f"laatste ronde {diagnose_age:.1f} minuten geleden"
            )
        )

    else:
        health.append(
            (
                "Diagnose actief; laatste ronde "
                f"{diagnose_age:.1f} minuten geleden"
            )
        )

    bot_age = file_age_minutes(
        BOT_STATE_FILE
    )

    if bot_age is None:
        health.append(
            "WAARSCHUWING: bot-statebestand ontbreekt"
        )

    else:
        health.append(
            (
                "Bot-state aanwezig; laatste wijziging "
                f"{bot_age:.1f} minuten geleden "
                "(wijzigt alleen bij een statewijziging)"
            )
        )

    if Path(CONTROL_FILE).exists():
        health.append(
            "Controlebestand aanwezig"
        )
    else:
        health.append(
            "WAARSCHUWING: controlebestand ontbreekt"
        )

    if Path(AGENT_STATE_FILE).exists():
        health.append(
            "Agent-statebestand aanwezig"
        )
    else:
        health.append(
            "WAARSCHUWING: agent-statebestand ontbreekt"
        )

    if Path(TRADES_FILE).exists():
        health.append(
            "Transactiebestand aanwezig"
        )
    else:
        health.append(
            "Nog geen transactiebestand; waarschijnlijk nog geen trades"
        )

    return health


# ============================================================
# Volledig supervisorrapport
# ============================================================

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

    symbols = diagnose_stats.get(
        "symbols",
        {},
    )

    recommendations: List[str] = []

    if isinstance(symbols, dict):
        for symbol, symbol_stats in sorted(
            symbols.items()
        ):
            if not isinstance(
                symbol_stats,
                dict,
            ):
                continue

            recommendations.extend(
                make_symbol_recommendations(
                    symbol,
                    symbol_stats,
                )
            )

    if not recommendations:
        recommendations.append(
            "Nog geen bruikbare diagnosestatistieken beschikbaar."
        )

    positions = bot_state.get(
        "positions",
        {},
    )

    if not isinstance(
        positions,
        dict,
    ):
        positions = {}

    short_positions = bot_state.get(
        "short_positions",
        {},
    )

    if not isinstance(
        short_positions,
        dict,
    ):
        short_positions = {}

    report = {
        "version": 2,
        "generated_at": now_iso(),
        "mode": "suggest",
        "automatic_changes_enabled": False,
        "total_diagnose_rounds": to_int(
            diagnose_stats.get(
                "total_rounds"
            ),
            0,
        ),
        "last_diagnose_round_at": diagnose_stats.get(
            "last_round_at"
        ),
        "open_spot_positions": len(
            positions
        ),
        "open_short_positions": len(
            short_positions
        ),
        "spot_trades": to_int(
            bot_state.get("trades"),
            0,
        ),
        "spot_wins": to_int(
            bot_state.get("wins"),
            0,
        ),
        "spot_pnl_eur": to_float(
            bot_state.get("pnl_quote"),
            0.0,
        ),
        "simulated_free_eur": to_float(
            bot_state.get(
                "simulated_free_quote"
            ),
            0.0,
        ),
        "paused": to_bool(
            control.get("paused"),
            False,
        ),
        "pause_reason": str(
            control.get(
                "pause_reason"
            )
            or ""
        ),
        "health": build_health_report(
            diagnose_stats
        ),
        "recommendations": recommendations,
    }

    return report


def log_report(
    report: Dict[str, Any],
) -> None:
    LOG.info(
        "SUPERVISOR | rondes=%s | spot_open=%s | "
        "trades=%s | pnl=%+.2f EUR | paused=%s",
        report.get(
            "total_diagnose_rounds",
            0,
        ),
        report.get(
            "open_spot_positions",
            0,
        ),
        report.get(
            "spot_trades",
            0,
        ),
        to_float(
            report.get("spot_pnl_eur"),
            0.0,
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


# ============================================================
# Hoofdprogramma
# ============================================================

def main() -> None:
    LOG.info(
        "Diamond Supervisor v2.1 gestart"
    )

    LOG.info(
        "Modus=suggest | automatische wijzigingen uitgeschakeld"
    )

    LOG.info(
        "Diagnosestatistieken: %s",
        DIAG_STATS_FILE,
    )

    LOG.info(
        "Supervisorrapport: %s",
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
                "Supervisor-hoofdloop fout: %s",
                exc,
            )

        time.sleep(
            CHECK_INTERVAL_SECONDS
        )


if __name__ == "__main__":
    main()
