#!/usr/bin/env python3
"""
Diamond Strategy Lab v1.0

Doel:
- Analyseert de historische signalen van Diamond Market Scanner.
- Analyseert gesloten virtuele schaduwtrades.
- Vergelijkt resultaten per strategie, munt, richting en marktregime.
- Berekent resultaten voor inzetten van 120, 125, 130 en 135 euro.
- Schrijft een JSON-, tekst- en CSV-rapport naar /var/data.
- Wijzigt geen bot-, scanner-, positie- of transactiebestanden.
- Plaatst nooit orders en maakt geen verbinding met Bitvavo.

Standaarduitvoer:
- /var/data/diamond_strategy_lab.json
- /var/data/diamond_strategy_lab.txt
- /var/data/diamond_strategy_lab_groups.csv

Gebruik:
    python3 strategy_lab.py
    python3 strategy_lab.py --self-test
    python3 strategy_lab.py --loop --interval-minutes 360
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


VERSION = "1.0"
REPORT_VERSION = 1

LOG = logging.getLogger("diamond_strategy_lab")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

DEFAULT_DATA_DIR = Path(
    os.getenv(
        "DIAMOND_DATA_DIR",
        "/var/data",
    ).strip()
)

SIGNALS_FILENAME = "diamond_market_signals.csv"
SHADOW_TRADES_FILENAME = "diamond_shadow_trades.csv"
SCANNER_STATE_FILENAME = "diamond_market_scanner_state.json"
SCANNER_REPORT_FILENAME = "diamond_market_signals.json"

LAB_JSON_FILENAME = "diamond_strategy_lab.json"
LAB_TEXT_FILENAME = "diamond_strategy_lab.txt"
LAB_GROUPS_CSV_FILENAME = "diamond_strategy_lab_groups.csv"

STAKE_SCENARIOS = (
    120.0,
    125.0,
    130.0,
    135.0,
)

SIGNAL_REQUIRED_COLUMNS = {
    "detected_at",
    "symbol",
    "strategy",
    "side",
    "market_regime",
    "score",
    "spread_pct",
    "reward_risk",
    "expected_profit_eur",
    "shadow_eligible",
    "shadow_rejection_reasons",
}

SHADOW_REQUIRED_COLUMNS = {
    "opened_at",
    "closed_at",
    "symbol",
    "strategy",
    "side",
    "market_regime",
    "stake_eur",
    "total_fees_eur",
    "exit_reason",
    "net_pnl_eur",
    "return_pct",
    "duration_minutes",
}

GROUPS_CSV_HEADER = [
    "group_type",
    "group_name",
    "data_status",
    "trades",
    "wins",
    "losses",
    "neutral",
    "winrate_pct",
    "net_pnl_eur",
    "gross_profit_eur",
    "gross_loss_eur",
    "profit_factor",
    "average_pnl_eur",
    "average_win_eur",
    "average_loss_eur",
    "total_fees_eur",
    "average_return_pct",
    "average_duration_minutes",
    "maximum_loss_streak",
    "pnl_at_120_eur",
    "pnl_at_125_eur",
    "pnl_at_130_eur",
    "pnl_at_135_eur",
]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def to_float(
    value: Any,
    default: float = 0.0,
) -> float:
    try:
        if value in (
            None,
            "",
        ):
            return default

        number = float(value)

        if not math.isfinite(number):
            return default

        return number

    except (
        TypeError,
        ValueError,
    ):
        return default


def to_int(
    value: Any,
    default: int = 0,
) -> int:
    try:
        return int(
            to_float(
                value,
                float(default),
            )
        )

    except (
        TypeError,
        ValueError,
        OverflowError,
    ):
        return default


def to_bool(
    value: Any,
    default: bool = False,
) -> bool:
    if isinstance(
        value,
        bool,
    ):
        return value

    if value is None:
        return default

    normalized = str(
        value
    ).strip().lower()

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


def parse_datetime(
    value: Any,
) -> Optional[datetime]:
    raw = str(
        value
        or ""
    ).strip()

    if not raw:
        return None

    try:
        parsed = datetime.fromisoformat(
            raw.replace(
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


def safe_round(
    value: Any,
    digits: int = 6,
) -> float:
    return round(
        to_float(
            value,
            0.0,
        ),
        digits,
    )


def optional_round(
    value: Optional[float],
    digits: int = 4,
) -> Optional[float]:
    if value is None:
        return None

    return round(
        value,
        digits,
    )


def atomic_write_text(
    path: Path,
    text: str,
) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(
            path.parent
        ),
        delete=False,
        newline="\n",
    ) as temporary:
        temporary.write(
            text
        )
        temporary_name = (
            temporary.name
        )

    os.replace(
        temporary_name,
        path,
    )


def atomic_write_json(
    path: Path,
    data: Dict[str, Any],
) -> None:
    atomic_write_text(
        path,
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
    )


def atomic_write_csv(
    path: Path,
    rows: Iterable[
        Dict[str, Any]
    ],
) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(
            path.parent
        ),
        delete=False,
        newline="",
    ) as temporary:
        writer = csv.DictWriter(
            temporary,
            fieldnames=(
                GROUPS_CSV_HEADER
            ),
            extrasaction="ignore",
        )

        writer.writeheader()

        for row in rows:
            writer.writerow({
                key: row.get(
                    key,
                    "",
                )
                for key in (
                    GROUPS_CSV_HEADER
                )
            })

        temporary_name = (
            temporary.name
        )

    os.replace(
        temporary_name,
        path,
    )


def load_json_object(
    path: Path,
) -> Dict[str, Any]:
    if not path.is_file():
        return {}

    last_error: Optional[
        Exception
    ] = None

    for attempt in range(
        1,
        4,
    ):
        try:
            with path.open(
                "r",
                encoding="utf-8",
            ) as file:
                data = json.load(
                    file
                )

            if not isinstance(
                data,
                dict,
            ):
                raise ValueError(
                    f"{path.name} bevat geen JSON-object"
                )

            return data

        except Exception as exc:
            last_error = exc

            if attempt < 3:
                time.sleep(
                    0.15 * attempt
                )

    LOG.warning(
        "JSON-bestand kon niet worden gelezen | %s | %s",
        path,
        last_error,
    )

    return {}


def load_csv_rows(
    path: Path,
    required_columns: set[str],
    optional: bool,
) -> Tuple[
    List[Dict[str, str]],
    List[str],
]:
    if not path.is_file():
        if optional:
            return [], []

        return [], [
            f"bestand ontbreekt: {path}"
        ]

    last_error: Optional[
        Exception
    ] = None

    for attempt in range(
        1,
        4,
    ):
        try:
            with path.open(
                "r",
                encoding="utf-8",
                newline="",
            ) as file:
                reader = csv.DictReader(
                    file
                )

                header = set(
                    reader.fieldnames
                    or []
                )

                missing = sorted(
                    required_columns
                    - header
                )

                if missing:
                    return [], [
                        (
                            f"ongeldig CSV-schema in "
                            f"{path.name}; ontbreekt: "
                            + ", ".join(
                                missing
                            )
                        )
                    ]

                rows = [
                    {
                        str(key): (
                            ""
                            if value is None
                            else str(value)
                        )
                        for key, value
                        in row.items()
                    }
                    for row in reader
                ]

            return rows, []

        except Exception as exc:
            last_error = exc

            if attempt < 3:
                time.sleep(
                    0.15 * attempt
                )

    return [], [
        (
            f"CSV lezen mislukt voor "
            f"{path}: {last_error}"
        )
    ]


def split_rejection_reasons(
    raw: Any,
) -> List[str]:
    text = str(
        raw
        or ""
    ).strip()

    if not text:
        return []

    return [
        item.strip()
        for item in text.split(
            "|"
        )
        if item.strip()
    ]


def normalize_regime(
    value: Any,
) -> str:
    raw = str(
        value
        or "ONBEKEND"
    ).strip().upper()

    if raw.startswith(
        "BULLISH"
    ):
        return "BULLISH"

    if raw.startswith(
        "BEARISH"
    ):
        return "BEARISH"

    if raw.startswith(
        "NEUTRAL"
    ):
        return "NEUTRAL"

    return raw or "ONBEKEND"


def data_status(
    trade_count: int,
) -> str:
    if trade_count <= 0:
        return "GEEN GESLOTEN TRADES"

    if trade_count < 5:
        return "NOG TE WEINIG DATA"

    if trade_count < 10:
        return "EERSTE INDICATIE"

    if trade_count < 20:
        return "VOORLOPIGE BEOORDELING"

    return "VOLDOENDE VOOR BEOORDELING"


def maximum_loss_streak(
    rows: List[
        Dict[str, Any]
    ],
) -> int:
    current = 0
    maximum = 0

    ordered = sorted(
        rows,
        key=lambda item: (
            item.get(
                "_closed_datetime"
            )
            or datetime.min.replace(
                tzinfo=timezone.utc
            )
        ),
    )

    for row in ordered:
        pnl = to_float(
            row.get(
                "net_pnl_eur"
            ),
            0.0,
        )

        if pnl < -0.000001:
            current += 1
            maximum = max(
                maximum,
                current,
            )
        else:
            current = 0

    return maximum


def summarize_trade_group(
    rows: List[
        Dict[str, Any]
    ],
) -> Dict[str, Any]:
    pnls = [
        to_float(
            row.get(
                "net_pnl_eur"
            ),
            0.0,
        )
        for row in rows
    ]

    returns = [
        to_float(
            row.get(
                "return_pct"
            ),
            0.0,
        )
        for row in rows
    ]

    durations = [
        max(
            0.0,
            to_float(
                row.get(
                    "duration_minutes"
                ),
                0.0,
            ),
        )
        for row in rows
    ]

    fees = [
        max(
            0.0,
            to_float(
                row.get(
                    "total_fees_eur"
                ),
                0.0,
            ),
        )
        for row in rows
    ]

    wins_values = [
        pnl
        for pnl in pnls
        if pnl > 0.000001
    ]

    loss_values = [
        pnl
        for pnl in pnls
        if pnl < -0.000001
    ]

    neutral = (
        len(pnls)
        - len(wins_values)
        - len(loss_values)
    )

    gross_profit = sum(
        wins_values
    )

    gross_loss = sum(
        loss_values
    )

    net_pnl = sum(
        pnls
    )

    trade_count = len(
        rows
    )

    profit_factor: Optional[
        float
    ]

    if gross_loss < -0.000001:
        profit_factor = (
            gross_profit
            / abs(
                gross_loss
            )
        )
    elif gross_profit > 0.000001:
        profit_factor = None
    else:
        profit_factor = 0.0

    scenarios = {
        str(
            int(stake)
        ): round(
            sum(
                stake
                * return_pct
                / 100.0
                for return_pct
                in returns
            ),
            6,
        )
        for stake in (
            STAKE_SCENARIOS
        )
    }

    best_trade = max(
        rows,
        key=lambda item: to_float(
            item.get(
                "net_pnl_eur"
            ),
            0.0,
        ),
        default=None,
    )

    worst_trade = min(
        rows,
        key=lambda item: to_float(
            item.get(
                "net_pnl_eur"
            ),
            0.0,
        ),
        default=None,
    )

    return {
        "data_status": data_status(
            trade_count
        ),
        "trades": trade_count,
        "wins": len(
            wins_values
        ),
        "losses": len(
            loss_values
        ),
        "neutral": neutral,
        "winrate_pct": round(
            (
                100.0
                * len(
                    wins_values
                )
                / trade_count
            )
            if trade_count
            else 0.0,
            2,
        ),
        "net_pnl_eur": round(
            net_pnl,
            6,
        ),
        "gross_profit_eur": round(
            gross_profit,
            6,
        ),
        "gross_loss_eur": round(
            gross_loss,
            6,
        ),
        "profit_factor": optional_round(
            profit_factor,
            4,
        ),
        "average_pnl_eur": round(
            (
                net_pnl
                / trade_count
            )
            if trade_count
            else 0.0,
            6,
        ),
        "average_win_eur": round(
            (
                gross_profit
                / len(
                    wins_values
                )
            )
            if wins_values
            else 0.0,
            6,
        ),
        "average_loss_eur": round(
            (
                gross_loss
                / len(
                    loss_values
                )
            )
            if loss_values
            else 0.0,
            6,
        ),
        "total_fees_eur": round(
            sum(
                fees
            ),
            6,
        ),
        "average_return_pct": round(
            (
                sum(
                    returns
                )
                / trade_count
            )
            if trade_count
            else 0.0,
            6,
        ),
        "average_duration_minutes": round(
            (
                sum(
                    durations
                )
                / trade_count
            )
            if trade_count
            else 0.0,
            2,
        ),
        "maximum_loss_streak": maximum_loss_streak(
            rows
        ),
        "stake_scenarios": scenarios,
        "best_trade": public_trade(
            best_trade
        ),
        "worst_trade": public_trade(
            worst_trade
        ),
    }


def public_trade(
    row: Optional[
        Dict[str, Any]
    ],
) -> Optional[
    Dict[str, Any]
]:
    if not row:
        return None

    return {
        "opened_at": row.get(
            "opened_at"
        ),
        "closed_at": row.get(
            "closed_at"
        ),
        "symbol": row.get(
            "symbol"
        ),
        "strategy": row.get(
            "strategy"
        ),
        "side": row.get(
            "side"
        ),
        "market_regime": row.get(
            "market_regime"
        ),
        "signal_score": safe_round(
            row.get(
                "signal_score"
            ),
            2,
        ),
        "stake_eur": safe_round(
            row.get(
                "stake_eur"
            ),
            2,
        ),
        "exit_reason": row.get(
            "exit_reason"
        ),
        "net_pnl_eur": safe_round(
            row.get(
                "net_pnl_eur"
            ),
            6,
        ),
        "return_pct": safe_round(
            row.get(
                "return_pct"
            ),
            6,
        ),
        "duration_minutes": safe_round(
            row.get(
                "duration_minutes"
            ),
            2,
        ),
    }


def prepare_shadow_rows(
    rows: List[
        Dict[str, str]
    ],
) -> List[
    Dict[str, Any]
]:
    result: List[
        Dict[str, Any]
    ] = []

    for row in rows:
        closed = parse_datetime(
            row.get(
                "closed_at"
            )
        )

        prepared: Dict[
            str,
            Any
        ] = dict(
            row
        )

        prepared[
            "market_regime"
        ] = normalize_regime(
            row.get(
                "market_regime"
            )
        )

        prepared[
            "_closed_datetime"
        ] = closed

        result.append(
            prepared
        )

    return result


def build_group_summaries(
    rows: List[
        Dict[str, Any]
    ],
) -> Dict[
    str,
    Dict[
        str,
        Dict[str, Any]
    ]
]:
    definitions = {
        "strategy": lambda row: str(
            row.get(
                "strategy"
            )
            or "ONBEKEND"
        ),
        "symbol": lambda row: str(
            row.get(
                "symbol"
            )
            or "ONBEKEND"
        ),
        "side": lambda row: str(
            row.get(
                "side"
            )
            or "ONBEKEND"
        ).upper(),
        "market_regime": lambda row: normalize_regime(
            row.get(
                "market_regime"
            )
        ),
        "exit_reason": lambda row: str(
            row.get(
                "exit_reason"
            )
            or "ONBEKEND"
        ),
    }

    result: Dict[
        str,
        Dict[
            str,
            Dict[str, Any]
        ]
    ] = {}

    for group_type, key_function in definitions.items():
        grouped: Dict[
            str,
            List[
                Dict[str, Any]
            ]
        ] = defaultdict(
            list
        )

        for row in rows:
            grouped[
                key_function(
                    row
                )
            ].append(
                row
            )

        summaries = {
            key: summarize_trade_group(
                items
            )
            for key, items
            in grouped.items()
        }

        result[
            group_type
        ] = dict(
            sorted(
                summaries.items(),
                key=lambda item: (
                    -to_int(
                        item[1].get(
                            "trades"
                        ),
                        0,
                    ),
                    -to_float(
                        item[1].get(
                            "net_pnl_eur"
                        ),
                        0.0,
                    ),
                    item[0],
                ),
            )
        )

    return result


def summarize_signal_group(
    rows: List[
        Dict[str, str]
    ],
) -> Dict[str, Any]:
    count = len(
        rows
    )

    eligible = sum(
        1
        for row in rows
        if to_bool(
            row.get(
                "shadow_eligible"
            ),
            False,
        )
    )

    scores = [
        to_float(
            row.get(
                "score"
            ),
            0.0,
        )
        for row in rows
    ]

    spreads = [
        to_float(
            row.get(
                "spread_pct"
            ),
            0.0,
        )
        for row in rows
    ]

    reward_risks = [
        to_float(
            row.get(
                "reward_risk"
            ),
            0.0,
        )
        for row in rows
    ]

    expected_profits = [
        to_float(
            row.get(
                "expected_profit_eur"
            ),
            0.0,
        )
        for row in rows
    ]

    return {
        "signals": count,
        "shadow_eligible": eligible,
        "rejected": (
            count
            - eligible
        ),
        "eligible_pct": round(
            (
                100.0
                * eligible
                / count
            )
            if count
            else 0.0,
            2,
        ),
        "average_score": round(
            (
                sum(
                    scores
                )
                / count
            )
            if count
            else 0.0,
            2,
        ),
        "average_spread_pct": round(
            (
                sum(
                    spreads
                )
                / count
            )
            if count
            else 0.0,
            6,
        ),
        "average_reward_risk": round(
            (
                sum(
                    reward_risks
                )
                / count
            )
            if count
            else 0.0,
            4,
        ),
        "average_expected_profit_eur": round(
            (
                sum(
                    expected_profits
                )
                / count
            )
            if count
            else 0.0,
            4,
        ),
    }


def build_signal_analysis(
    rows: List[
        Dict[str, str]
    ],
) -> Dict[str, Any]:
    ordered = sorted(
        rows,
        key=lambda row: (
            parse_datetime(
                row.get(
                    "detected_at"
                )
            )
            or datetime.min.replace(
                tzinfo=timezone.utc
            )
        ),
    )

    rejection_counter: Counter[
        str
    ] = Counter()

    for row in rows:
        for reason in split_rejection_reasons(
            row.get(
                "shadow_rejection_reasons"
            )
        ):
            rejection_counter[
                reason
            ] += 1

    groups: Dict[
        str,
        Dict[
            str,
            Dict[str, Any]
        ]
    ] = {}

    group_definitions = {
        "strategy": lambda row: str(
            row.get(
                "strategy"
            )
            or "ONBEKEND"
        ),
        "symbol": lambda row: str(
            row.get(
                "symbol"
            )
            or "ONBEKEND"
        ),
        "side": lambda row: str(
            row.get(
                "side"
            )
            or "ONBEKEND"
        ).upper(),
        "market_regime": lambda row: normalize_regime(
            row.get(
                "market_regime"
            )
        ),
    }

    for group_type, key_function in group_definitions.items():
        grouped: Dict[
            str,
            List[
                Dict[str, str]
            ]
        ] = defaultdict(
            list
        )

        for row in rows:
            grouped[
                key_function(
                    row
                )
            ].append(
                row
            )

        summaries = {
            key: summarize_signal_group(
                items
            )
            for key, items
            in grouped.items()
        }

        groups[
            group_type
        ] = dict(
            sorted(
                summaries.items(),
                key=lambda item: (
                    -to_int(
                        item[1].get(
                            "signals"
                        ),
                        0,
                    ),
                    item[0],
                ),
            )
        )

    total_summary = summarize_signal_group(
        rows
    )

    return {
        "first_signal_at": (
            ordered[0].get(
                "detected_at"
            )
            if ordered
            else None
        ),
        "last_signal_at": (
            ordered[-1].get(
                "detected_at"
            )
            if ordered
            else None
        ),
        **total_summary,
        "top_rejection_reasons": [
            {
                "reason": reason,
                "count": count,
            }
            for reason, count
            in rejection_counter.most_common(
                15
            )
        ],
        "groups": groups,
    }


def build_recommendations(
    signals: Dict[str, Any],
    shadow: Dict[str, Any],
    groups: Dict[
        str,
        Dict[
            str,
            Dict[str, Any]
        ]
    ],
) -> List[str]:
    recommendations: List[
        str
    ] = []

    trades = to_int(
        shadow.get(
            "trades"
        ),
        0,
    )

    eligible = to_int(
        signals.get(
            "shadow_eligible"
        ),
        0,
    )

    signal_count = to_int(
        signals.get(
            "signals"
        ),
        0,
    )

    if signal_count <= 0:
        recommendations.append(
            "Nog geen scannersignalen beschikbaar; eerst gegevens verzamelen."
        )

    elif eligible <= 0:
        recommendations.append(
            "Er zijn nog geen signalen door alle financiële filters gekomen; instellingen niet aanpassen op basis van alleen afwijzingen."
        )

    if trades <= 0:
        recommendations.append(
            "Nog geen gesloten schaduwtrades; er kan nog geen strategie als beter of slechter worden beoordeeld."
        )

    elif trades < 5:
        recommendations.append(
            "Minder dan vijf gesloten schaduwtrades; resultaten zijn nog toevalgevoelig en niet geschikt voor aanpassingen."
        )

    elif trades < 10:
        recommendations.append(
            "De eerste indicatie is beschikbaar, maar minimaal tien gesloten schaduwtrades zijn nodig voor een voorlopige vergelijking."
        )

    elif trades < 20:
        recommendations.append(
            "Een voorlopige vergelijking is mogelijk; wacht tot minimaal twintig gesloten schaduwtrades voordat een strategie wordt geselecteerd."
        )

    else:
        recommendations.append(
            "Er zijn minimaal twintig gesloten schaduwtrades; beoordeel nu ook spreiding per strategie, munt en marktregime voordat instellingen worden aangepast."
        )

    strategy_groups = (
        groups.get(
            "strategy"
        )
        or {}
    )

    qualified = [
        (
            name,
            summary,
        )
        for name, summary
        in strategy_groups.items()
        if to_int(
            summary.get(
                "trades"
            ),
            0,
        ) >= 5
    ]

    if qualified:
        best_name, best_summary = max(
            qualified,
            key=lambda item: (
                to_float(
                    item[1].get(
                        "net_pnl_eur"
                    ),
                    0.0,
                ),
                to_float(
                    item[1].get(
                        "profit_factor"
                    ),
                    0.0,
                ),
                to_float(
                    item[1].get(
                        "winrate_pct"
                    ),
                    0.0,
                ),
            ),
        )

        recommendations.append(
            (
                f"Beste huidige strategie-indicatie: {best_name} "
                f"met {best_summary.get('trades', 0)} trades en "
                f"€{to_float(best_summary.get('net_pnl_eur'), 0.0):+.4f}; "
                "dit is een observatie, geen automatische wijziging."
            )
        )

    recommendations.append(
        "Strategy Lab wijzigt nooit zelfstandig de bot, scanner, inzet, stop-loss of take-profit."
    )

    return recommendations


def flatten_group_rows(
    groups: Dict[
        str,
        Dict[
            str,
            Dict[str, Any]
        ]
    ],
) -> List[
    Dict[str, Any]
]:
    rows: List[
        Dict[str, Any]
    ] = []

    for group_type, items in groups.items():
        for group_name, summary in items.items():
            scenarios = (
                summary.get(
                    "stake_scenarios"
                )
                or {}
            )

            rows.append({
                "group_type": group_type,
                "group_name": group_name,
                "data_status": summary.get(
                    "data_status"
                ),
                "trades": summary.get(
                    "trades"
                ),
                "wins": summary.get(
                    "wins"
                ),
                "losses": summary.get(
                    "losses"
                ),
                "neutral": summary.get(
                    "neutral"
                ),
                "winrate_pct": summary.get(
                    "winrate_pct"
                ),
                "net_pnl_eur": summary.get(
                    "net_pnl_eur"
                ),
                "gross_profit_eur": summary.get(
                    "gross_profit_eur"
                ),
                "gross_loss_eur": summary.get(
                    "gross_loss_eur"
                ),
                "profit_factor": (
                    ""
                    if summary.get(
                        "profit_factor"
                    ) is None
                    else summary.get(
                        "profit_factor"
                    )
                ),
                "average_pnl_eur": summary.get(
                    "average_pnl_eur"
                ),
                "average_win_eur": summary.get(
                    "average_win_eur"
                ),
                "average_loss_eur": summary.get(
                    "average_loss_eur"
                ),
                "total_fees_eur": summary.get(
                    "total_fees_eur"
                ),
                "average_return_pct": summary.get(
                    "average_return_pct"
                ),
                "average_duration_minutes": summary.get(
                    "average_duration_minutes"
                ),
                "maximum_loss_streak": summary.get(
                    "maximum_loss_streak"
                ),
                "pnl_at_120_eur": scenarios.get(
                    "120",
                    0.0,
                ),
                "pnl_at_125_eur": scenarios.get(
                    "125",
                    0.0,
                ),
                "pnl_at_130_eur": scenarios.get(
                    "130",
                    0.0,
                ),
                "pnl_at_135_eur": scenarios.get(
                    "135",
                    0.0,
                ),
            })

    return rows


def format_profit_factor(
    value: Any,
) -> str:
    if value is None:
        return "n.v.t."

    return f"{to_float(value, 0.0):.2f}"


def format_group_table(
    title: str,
    groups: Dict[
        str,
        Dict[str, Any]
    ],
    limit: int = 15,
) -> List[str]:
    lines = [
        "",
        title,
        "-" * 78,
    ]

    if not groups:
        lines.append(
            "Nog geen gesloten schaduwtrades beschikbaar."
        )

        return lines

    lines.append(
        (
            f"{'Groep':<24} "
            f"{'Trades':>6} "
            f"{'W/V':>7} "
            f"{'Winrate':>8} "
            f"{'Netto':>11} "
            f"{'PF':>7} "
            f"{'Status':<26}"
        )
    )

    for name, summary in list(
        groups.items()
    )[:limit]:
        lines.append(
            (
                f"{name[:24]:<24} "
                f"{to_int(summary.get('trades'), 0):>6} "
                f"{to_int(summary.get('wins'), 0):>3}/"
                f"{to_int(summary.get('losses'), 0):<3} "
                f"{to_float(summary.get('winrate_pct'), 0.0):>7.1f}% "
                f"€{to_float(summary.get('net_pnl_eur'), 0.0):>+9.4f} "
                f"{format_profit_factor(summary.get('profit_factor')):>7} "
                f"{str(summary.get('data_status') or '-'):<26}"
            )
        )

    return lines


def format_text_report(
    report: Dict[str, Any],
) -> str:
    signals = (
        report.get(
            "signals"
        )
        or {}
    )

    shadow = (
        report.get(
            "shadow_trades"
        )
        or {}
    )

    groups = (
        report.get(
            "groups"
        )
        or {}
    )

    state = (
        report.get(
            "scanner_state"
        )
        or {}
    )

    open_positions = (
        state.get(
            "open_positions"
        )
        or []
    )

    lines = [
        "=" * 78,
        "DIAMOND STRATEGY LAB",
        f"Versie                  : {report.get('version')}",
        f"Gegenereerd             : {report.get('generated_at')}",
        "=" * 78,
        "",
        "VEILIGHEID",
        "Orders mogelijk         : NEE",
        "Bot-state gewijzigd     : NEE",
        "Scanner-state gewijzigd : NEE",
        "Automatische aanpassing : NEE",
        "",
        "SCANNERGEGEVENS",
        f"Scanner-versie          : {state.get('version') or '-'}",
        f"Scanner gestart         : {state.get('started_at') or '-'}",
        f"Laatste scan            : {state.get('last_scan_at') or '-'}",
        f"Scans totaal            : {to_int(state.get('scan_count'), 0)}",
        f"Unieke signalen state   : {to_int(state.get('total_unique_signals'), 0)}",
        f"Open schaduwposities    : {len(open_positions)}",
        "",
        "SIGNAALTRECHTER",
        f"Signalen in CSV         : {to_int(signals.get('signals'), 0)}",
        f"Door filters gekomen    : {to_int(signals.get('shadow_eligible'), 0)}",
        f"Afgewezen               : {to_int(signals.get('rejected'), 0)}",
        f"Doorgangspercentage     : {to_float(signals.get('eligible_pct'), 0.0):.2f}%",
        f"Gemiddelde score        : {to_float(signals.get('average_score'), 0.0):.2f}",
        f"Gemiddelde spread       : {to_float(signals.get('average_spread_pct'), 0.0):.4f}%",
        f"Gemiddelde RR           : {to_float(signals.get('average_reward_risk'), 0.0):.2f}",
        f"Gem. verwachte winst    : €{to_float(signals.get('average_expected_profit_eur'), 0.0):+.4f}",
        "",
        "SCHADUWRESULTATEN TOTAAL",
        f"Datastatus              : {shadow.get('data_status') or '-'}",
        f"Gesloten trades         : {to_int(shadow.get('trades'), 0)}",
        f"Winst/verlies/neutraal  : "
        f"{to_int(shadow.get('wins'), 0)}/"
        f"{to_int(shadow.get('losses'), 0)}/"
        f"{to_int(shadow.get('neutral'), 0)}",
        f"Winrate                 : {to_float(shadow.get('winrate_pct'), 0.0):.2f}%",
        f"Nettoresultaat          : €{to_float(shadow.get('net_pnl_eur'), 0.0):+.4f}",
        f"Totale kosten           : €{to_float(shadow.get('total_fees_eur'), 0.0):.4f}",
        f"Profit factor           : {format_profit_factor(shadow.get('profit_factor'))}",
        f"Gemiddelde per trade    : €{to_float(shadow.get('average_pnl_eur'), 0.0):+.4f}",
        f"Gemiddelde return       : {to_float(shadow.get('average_return_pct'), 0.0):+.4f}%",
        f"Gemiddelde looptijd     : {to_float(shadow.get('average_duration_minutes'), 0.0):.1f} minuten",
        f"Max. verliesreeks       : {to_int(shadow.get('maximum_loss_streak'), 0)}",
        "",
        "INZETSCENARIO'S OP BASIS VAN DEZELFDE RETURNS",
    ]

    scenarios = (
        shadow.get(
            "stake_scenarios"
        )
        or {}
    )

    for stake in STAKE_SCENARIOS:
        key = str(
            int(stake)
        )

        lines.append(
            f"€{stake:>6.0f} per trade       : "
            f"€{to_float(scenarios.get(key), 0.0):+.4f}"
        )

    rejection_reasons = (
        signals.get(
            "top_rejection_reasons"
        )
        or []
    )

    lines.extend([
        "",
        "MEEST VOORKOMENDE AFWIJZINGEN",
        "-" * 78,
    ])

    if rejection_reasons:
        for item in rejection_reasons[:10]:
            lines.append(
                f"{to_int(item.get('count'), 0):>5}x  "
                f"{item.get('reason') or '-'}"
            )
    else:
        lines.append(
            "Geen afwijzingsredenen geregistreerd."
        )

    lines.extend(
        format_group_table(
            "RESULTAAT PER STRATEGIE",
            groups.get(
                "strategy"
            )
            or {},
        )
    )

    lines.extend(
        format_group_table(
            "RESULTAAT PER MUNT",
            groups.get(
                "symbol"
            )
            or {},
            limit=25,
        )
    )

    lines.extend(
        format_group_table(
            "RESULTAAT PER RICHTING",
            groups.get(
                "side"
            )
            or {},
        )
    )

    lines.extend(
        format_group_table(
            "RESULTAAT PER MARKTREGIME",
            groups.get(
                "market_regime"
            )
            or {},
        )
    )

    lines.extend(
        format_group_table(
            "RESULTAAT PER SLUITREDEN",
            groups.get(
                "exit_reason"
            )
            or {},
        )
    )

    lines.extend([
        "",
        "BEOORDELING",
        "-" * 78,
    ])

    for item in (
        report.get(
            "recommendations"
        )
        or []
    ):
        lines.append(
            f"- {item}"
        )

    errors = (
        report.get(
            "errors"
        )
        or []
    )

    if errors:
        lines.extend([
            "",
            "WAARSCHUWINGEN",
            "-" * 78,
        ])

        for error in errors:
            lines.append(
                f"- {error}"
            )

    lines.extend([
        "",
        "RAPPORTBESTANDEN",
        f"JSON                     : {report.get('output_files', {}).get('json')}",
        f"Tekst                    : {report.get('output_files', {}).get('text')}",
        f"Groepen-CSV              : {report.get('output_files', {}).get('groups_csv')}",
        "=" * 78,
    ])

    return "\n".join(
        lines
    ) + "\n"


def build_report(
    data_dir: Path,
) -> Dict[str, Any]:
    signals_path = (
        data_dir
        / SIGNALS_FILENAME
    )

    shadow_path = (
        data_dir
        / SHADOW_TRADES_FILENAME
    )

    state_path = (
        data_dir
        / SCANNER_STATE_FILENAME
    )

    scanner_report_path = (
        data_dir
        / SCANNER_REPORT_FILENAME
    )

    json_output = (
        data_dir
        / LAB_JSON_FILENAME
    )

    text_output = (
        data_dir
        / LAB_TEXT_FILENAME
    )

    groups_output = (
        data_dir
        / LAB_GROUPS_CSV_FILENAME
    )

    signal_rows, signal_errors = load_csv_rows(
        signals_path,
        SIGNAL_REQUIRED_COLUMNS,
        optional=False,
    )

    shadow_rows_raw, shadow_errors = load_csv_rows(
        shadow_path,
        SHADOW_REQUIRED_COLUMNS,
        optional=True,
    )

    shadow_rows = prepare_shadow_rows(
        shadow_rows_raw
    )

    scanner_state = load_json_object(
        state_path
    )

    scanner_report = load_json_object(
        scanner_report_path
    )

    signal_analysis = build_signal_analysis(
        signal_rows
    )

    shadow_summary = summarize_trade_group(
        shadow_rows
    )

    groups = build_group_summaries(
        shadow_rows
    )

    open_positions_raw = (
        scanner_state.get(
            "open_positions"
        )
        or {}
    )

    if isinstance(
        open_positions_raw,
        dict,
    ):
        open_positions = list(
            open_positions_raw.values()
        )
    elif isinstance(
        open_positions_raw,
        list,
    ):
        open_positions = (
            open_positions_raw
        )
    else:
        open_positions = []

    public_open_positions = [
        {
            "opened_at": position.get(
                "opened_at"
            ),
            "symbol": position.get(
                "symbol"
            ),
            "strategy": position.get(
                "strategy"
            ),
            "side": position.get(
                "side"
            ),
            "market_regime": position.get(
                "market_regime"
            ),
            "signal_score": safe_round(
                position.get(
                    "signal_score"
                ),
                2,
            ),
            "stake_eur": safe_round(
                position.get(
                    "stake_eur"
                ),
                2,
            ),
            "entry_price": safe_round(
                position.get(
                    "entry_price"
                ),
                12,
            ),
            "take_profit": safe_round(
                position.get(
                    "take_profit"
                ),
                12,
            ),
            "stop_loss": safe_round(
                position.get(
                    "stop_loss"
                ),
                12,
            ),
        }
        for position in open_positions
        if isinstance(
            position,
            dict,
        )
    ]

    errors = (
        signal_errors
        + shadow_errors
    )

    scanner_mode = str(
        scanner_report.get(
            "mode"
        )
        or "-"
    )

    orders_possible = to_bool(
        (
            scanner_report.get(
                "safety"
            )
            or {}
        ).get(
            "orders_possible"
        ),
        False,
    )

    if (
        scanner_report
        and scanner_mode
        != "VIRTUAL_SHADOW_TRADING"
    ):
        errors.append(
            (
                "onverwachte scannermodus: "
                f"{scanner_mode}"
            )
        )

    if orders_possible:
        errors.append(
            "scanner meldt dat echte orders mogelijk zijn"
        )

    report: Dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "version": VERSION,
        "generated_at": now_iso(),
        "mode": "READ_ONLY_STRATEGY_ANALYSIS",
        "safety": {
            "orders_possible": False,
            "exchange_connection_used": False,
            "bot_state_modified": False,
            "scanner_state_modified": False,
            "settings_modified": False,
            "automatic_strategy_changes": False,
        },
        "input_files": {
            "signals_csv": str(
                signals_path
            ),
            "shadow_trades_csv": str(
                shadow_path
            ),
            "scanner_state_json": str(
                state_path
            ),
            "scanner_report_json": str(
                scanner_report_path
            ),
        },
        "output_files": {
            "json": str(
                json_output
            ),
            "text": str(
                text_output
            ),
            "groups_csv": str(
                groups_output
            ),
        },
        "scanner_state": {
            "version": scanner_state.get(
                "version"
            ),
            "started_at": scanner_state.get(
                "started_at"
            ),
            "last_scan_at": scanner_state.get(
                "last_scan_at"
            ),
            "scan_count": to_int(
                scanner_state.get(
                    "scan_count"
                ),
                0,
            ),
            "total_unique_signals": to_int(
                scanner_state.get(
                    "total_unique_signals"
                ),
                0,
            ),
            "open_positions": public_open_positions,
            "shadow_totals": (
                scanner_state.get(
                    "shadow_totals"
                )
                or {}
            ),
        },
        "signals": signal_analysis,
        "shadow_trades": shadow_summary,
        "groups": groups,
        "recommendations": [],
        "errors": errors,
    }

    report[
        "recommendations"
    ] = build_recommendations(
        signal_analysis,
        shadow_summary,
        groups,
    )

    return report


def write_report(
    report: Dict[str, Any],
) -> None:
    output_files = (
        report.get(
            "output_files"
        )
        or {}
    )

    json_path = Path(
        str(
            output_files[
                "json"
            ]
        )
    )

    text_path = Path(
        str(
            output_files[
                "text"
            ]
        )
    )

    groups_path = Path(
        str(
            output_files[
                "groups_csv"
            ]
        )
    )

    atomic_write_json(
        json_path,
        report,
    )

    atomic_write_text(
        text_path,
        format_text_report(
            report
        ),
    )

    atomic_write_csv(
        groups_path,
        flatten_group_rows(
            report.get(
                "groups"
            )
            or {}
        ),
    )


def run_once(
    data_dir: Path,
    print_report: bool,
) -> Dict[str, Any]:
    report = build_report(
        data_dir
    )

    write_report(
        report
    )

    text = format_text_report(
        report
    )

    if print_report:
        print(
            text,
            end="",
        )

    shadow = (
        report.get(
            "shadow_trades"
        )
        or {}
    )

    signals = (
        report.get(
            "signals"
        )
        or {}
    )

    LOG.info(
        "Strategy Lab gereed | signalen=%d | geschikt=%d | "
        "gesloten_schaduw=%d | netto=€%+.4f | fouten=%d",
        to_int(
            signals.get(
                "signals"
            ),
            0,
        ),
        to_int(
            signals.get(
                "shadow_eligible"
            ),
            0,
        ),
        to_int(
            shadow.get(
                "trades"
            ),
            0,
        ),
        to_float(
            shadow.get(
                "net_pnl_eur"
            ),
            0.0,
        ),
        len(
            report.get(
                "errors"
            )
            or []
        ),
    )

    return report


def self_test() -> None:
    with tempfile.TemporaryDirectory() as temporary_name:
        data_dir = Path(
            temporary_name
        )

        now = now_iso()

        signal_rows = [
            {
                "detected_at": now,
                "candle_timestamp": now,
                "symbol": "SOL/EUR",
                "strategy": "momentum",
                "side": "LONG",
                "market_regime": "BULLISH_STRONG",
                "regime_strength": "80",
                "score": "88",
                "entry_price": "100",
                "take_profit": "103",
                "stop_loss": "98",
                "rsi": "62",
                "atr_pct": "0.7",
                "volume_ratio": "1.8",
                "spread_pct": "0.05",
                "quote_volume": "1000000",
                "change_pct_24h": "4",
                "expected_net_pct": "2",
                "risk_net_pct": "1.2",
                "reward_risk": "1.67",
                "expected_profit_eur": "2.4",
                "expected_loss_eur": "1.44",
                "expected_eur_120": "2.4",
                "expected_eur_125": "2.5",
                "expected_eur_130": "2.6",
                "expected_eur_135": "2.7",
                "shadow_eligible": "True",
                "shadow_rejection_reasons": "",
                "reasons": "test",
            },
            {
                "detected_at": now,
                "candle_timestamp": now,
                "symbol": "ALLO/EUR",
                "strategy": "trend_breakout",
                "side": "SHORT",
                "market_regime": "BEARISH",
                "regime_strength": "70",
                "score": "94",
                "entry_price": "0.2",
                "take_profit": "0.19",
                "stop_loss": "0.205",
                "rsi": "35",
                "atr_pct": "1",
                "volume_ratio": "2",
                "spread_pct": "0.18",
                "quote_volume": "800000",
                "change_pct_24h": "-5",
                "expected_net_pct": "4",
                "risk_net_pct": "2.5",
                "reward_risk": "1.6",
                "expected_profit_eur": "4.8",
                "expected_loss_eur": "3",
                "expected_eur_120": "4.8",
                "expected_eur_125": "5",
                "expected_eur_130": "5.2",
                "expected_eur_135": "5.4",
                "shadow_eligible": "False",
                "shadow_rejection_reasons": (
                    "spread 0.1800% hoger dan 0.1000%"
                ),
                "reasons": "test",
            },
        ]

        with (
            data_dir
            / SIGNALS_FILENAME
        ).open(
            "w",
            encoding="utf-8",
            newline="",
        ) as file:
            writer = csv.DictWriter(
                file,
                fieldnames=[
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
                ],
            )
            writer.writeheader()
            writer.writerows(
                signal_rows
            )

        shadow_rows = [
            {
                "opened_at": now,
                "closed_at": now,
                "symbol": "SOL/EUR",
                "strategy": "momentum",
                "side": "LONG",
                "market_regime": "BULLISH_STRONG",
                "signal_score": "88",
                "entry_price": "100",
                "exit_price": "102",
                "stake_eur": "120",
                "amount": "1.2",
                "entry_fee_eur": "0.3",
                "exit_fee_eur": "0.306",
                "total_fees_eur": "0.606",
                "entry_spread_pct": "0.05",
                "exit_spread_pct": "0.05",
                "atr_pct": "0.7",
                "take_profit": "103",
                "stop_loss": "98",
                "exit_reason": "take_profit",
                "gross_pnl_eur": "2.4",
                "net_pnl_eur": "1.794",
                "return_pct": "1.495",
                "duration_minutes": "90",
                "entry_candle_timestamp_ms": "1",
                "exit_candle_timestamp_ms": "2",
            },
            {
                "opened_at": now,
                "closed_at": now,
                "symbol": "BTC/EUR",
                "strategy": "momentum",
                "side": "SHORT",
                "market_regime": "BEARISH",
                "signal_score": "82",
                "entry_price": "50000",
                "exit_price": "50500",
                "stake_eur": "120",
                "amount": "0.0024",
                "entry_fee_eur": "0.3",
                "exit_fee_eur": "0.303",
                "total_fees_eur": "0.603",
                "entry_spread_pct": "0.01",
                "exit_spread_pct": "0.01",
                "atr_pct": "0.5",
                "take_profit": "49000",
                "stop_loss": "50500",
                "exit_reason": "stop_loss",
                "gross_pnl_eur": "-1.2",
                "net_pnl_eur": "-1.803",
                "return_pct": "-1.5025",
                "duration_minutes": "45",
                "entry_candle_timestamp_ms": "1",
                "exit_candle_timestamp_ms": "2",
            },
        ]

        with (
            data_dir
            / SHADOW_TRADES_FILENAME
        ).open(
            "w",
            encoding="utf-8",
            newline="",
        ) as file:
            writer = csv.DictWriter(
                file,
                fieldnames=[
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
                ],
            )
            writer.writeheader()
            writer.writerows(
                shadow_rows
            )

        atomic_write_json(
            data_dir
            / SCANNER_STATE_FILENAME,
            {
                "version": "1.1",
                "started_at": now,
                "last_scan_at": now,
                "scan_count": 12,
                "total_unique_signals": 2,
                "open_positions": {},
                "shadow_totals": {
                    "opened": 2,
                    "closed": 2,
                    "wins": 1,
                    "losses": 1,
                    "neutral": 0,
                    "net_pnl_eur": -0.009,
                    "total_fees_eur": 1.209,
                },
            },
        )

        atomic_write_json(
            data_dir
            / SCANNER_REPORT_FILENAME,
            {
                "version": "1.1",
                "generated_at": now,
                "mode": "VIRTUAL_SHADOW_TRADING",
                "safety": {
                    "orders_possible": False,
                },
            },
        )

        report = run_once(
            data_dir,
            print_report=False,
        )

        assert (
            report["signals"][
                "signals"
            ]
            == 2
        )

        assert (
            report["signals"][
                "shadow_eligible"
            ]
            == 1
        )

        assert (
            report["shadow_trades"][
                "trades"
            ]
            == 2
        )

        assert (
            report["shadow_trades"][
                "wins"
            ]
            == 1
        )

        assert (
            report["shadow_trades"][
                "losses"
            ]
            == 1
        )

        assert (
            "momentum"
            in report["groups"][
                "strategy"
            ]
        )

        for filename in (
            LAB_JSON_FILENAME,
            LAB_TEXT_FILENAME,
            LAB_GROUPS_CSV_FILENAME,
        ):
            assert (
                data_dir
                / filename
            ).is_file()

        print(
            "DIAMOND STRATEGY LAB v1.0 ZELFTEST: GESLAAGD"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Analyseert Diamond Market Scanner-signalen "
            "en virtuele schaduwtrades."
        )
    )

    parser.add_argument(
        "--data-dir",
        default=str(
            DEFAULT_DATA_DIR
        ),
        help=(
            "Map met Diamond Trader-data "
            "(standaard: /var/data)"
        ),
    )

    parser.add_argument(
        "--self-test",
        action="store_true",
        help=(
            "Voert een lokale test uit zonder "
            "bestaande bestanden te gebruiken."
        ),
    )

    parser.add_argument(
        "--no-print",
        action="store_true",
        help=(
            "Schrijft rapportbestanden maar toont "
            "het tekstrapport niet op het scherm."
        ),
    )

    parser.add_argument(
        "--loop",
        action="store_true",
        help=(
            "Maakt het rapport herhaaldelijk."
        ),
    )

    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=360,
        help=(
            "Interval in minuten bij --loop "
            "(standaard: 360)."
        ),
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.self_test:
        self_test()
        return

    data_dir = Path(
        args.data_dir
    )

    interval_seconds = max(
        15,
        int(
            args.interval_minutes
        ),
    ) * 60

    while True:
        try:
            run_once(
                data_dir,
                print_report=(
                    not args.no_print
                ),
            )

        except Exception as exc:
            LOG.exception(
                "Strategy Lab mislukt: %s",
                exc,
            )

            if not args.loop:
                raise

        if not args.loop:
            break

        time.sleep(
            interval_seconds
        )


if __name__ == "__main__":
    main()
