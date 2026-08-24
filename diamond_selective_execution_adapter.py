import argparse
import csv
import json
from pathlib import Path
from typing import Any, Dict, List
from datetime import datetime, timezone

from diamond_market_crash_guard import poll_market_crash_guard
from diamond_selective_rules import (
    execution_signal,
    selective_accepts,
    selective_candidate_key,
)

DEFAULT_SIGNALS = Path("/var/data/diamond_market_signals.csv")

DEFAULT_CURSOR = Path(
    "/var/data/diamond_selective_execution_cursor.json"
)
MAX_EXECUTION_AGE_MINUTES = 20


def parse_time(value: str):
    try:
        dt = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def initialize_execution_baseline(
    signals_path: Path = DEFAULT_SIGNALS,
    cursor_path: Path = DEFAULT_CURSOR,
) -> Dict[str, Any]:
    contracts = load_candidates(signals_path)
    keys = [row["candidate_key"] for row in contracts]

    state = {
        "version": 2,
        "initialized_at": datetime.now(timezone.utc).isoformat(),
        "seen_keys": keys[-30000:],
        "baseline_count": len(keys),
    }

    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    cursor_path.write_text(
        json.dumps(state, indent=2),
        encoding="utf-8",
    )
    return state


def new_execution_contracts(
    signals_path: Path = DEFAULT_SIGNALS,
    cursor_path: Path = DEFAULT_CURSOR,
    max_age_minutes: int = MAX_EXECUTION_AGE_MINUTES,
) -> List[Dict[str, Any]]:
    """Geef verse uitvoerbare contracten terug zonder tijdelijke blockers weg te gooien.

    Belangrijk: een kandidaat wordt pas definitief als gezien gemarkeerd wanneer
    hij structureel ongeldig of verlopen is. Tijdelijke blokkades zoals crash
    guard, open positie, spread/liquiditeit of recovery worden elders opnieuw
    beoordeeld zolang het signaal jonger is dan max_age_minutes.
    """
    if not cursor_path.exists():
        initialize_execution_baseline(
            signals_path,
            cursor_path,
        )
        return []

    state = json.loads(cursor_path.read_text(encoding="utf-8"))
    state["version"] = max(2, int(state.get("version", 1) or 1))
    seen_order = list(dict.fromkeys(
        str(x) for x in state.get("seen_keys", [])
    ))
    seen = set(seen_order)

    def mark_seen(key: str) -> None:
        if key and key not in seen:
            seen.add(key)
            seen_order.append(key)

    now = datetime.now(timezone.utc)
    eligible = []

    # REALTIME_MARKET_CRASH_GUARD
    # Een crash-guard blokkade is tijdelijk. Daarom wordt een verse kandidaat
    # bij een blok NIET als gezien opgeslagen en mag hij later opnieuw proberen.
    crash_guard = poll_market_crash_guard()
    allow_long_by_market = bool(
        crash_guard.get("allow_long", False)
    )

    for row in load_candidates(signals_path):
        key = row["candidate_key"]
        if key in seen:
            continue

        detected = parse_time(row.get("detected_at", ""))
        if detected is None:
            mark_seen(key)
            continue

        age_min = (now - detected).total_seconds() / 60.0

        # Een klokverschil/future timestamp kan vanzelf herstellen.
        if age_min < 0:
            continue

        # Na expiry is opnieuw proberen niet meer toegestaan.
        if age_min > max_age_minutes:
            mark_seen(key)
            continue

        # Spot execution ondersteunt voorlopig uitsluitend LONG.
        if row.get("side") != "LONG":
            mark_seen(key)
            continue

        # Ontbrekend/bearish regime is structureel ongeschikt voor deze LONG.
        regime = str(
            row.get("market_regime") or ""
        ).strip().upper()

        if not regime or regime in {
            "BEARISH",
            "BEARISH_WEAK",
        }:
            mark_seen(key)
            continue

        # Tijdelijke marktblock: kandidaat blijft retryable tot expiry.
        if not allow_long_by_market:
            continue

        # NIET als gezien markeren: de daadwerkelijke LIVE-route kan hem nog
        # tijdelijk blokkeren door positie, cooldown, spread, liquidity, budget,
        # approval of recovery. Daardoor blijft hij opnieuw probeerbaar.
        eligible.append(row)

    state["seen_keys"] = seen_order[-30000:]
    state["last_poll_at"] = now.isoformat()
    state["retryable_count"] = len(eligible)
    state["crash_guard_status"] = str(
        crash_guard.get("status") or "UNKNOWN"
    )
    state["crash_guard_reason"] = str(
        crash_guard.get("reason") or ""
    )
    state["crash_guard_block_until"] = crash_guard.get(
        "block_until",
        0.0,
    )
    state["crash_guard_changes_pct"] = crash_guard.get(
        "changes_pct",
        {},
    )

    cursor_path.write_text(
        json.dumps(state, indent=2),
        encoding="utf-8",
    )

    return eligible



def load_candidates(
    path: Path = DEFAULT_SIGNALS,
) -> List[Dict[str, Any]]:
    if not path.exists():
        return []

    result = []
    seen = set()

    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not selective_accepts(row):
                continue

            key = selective_candidate_key(row)
            if not key or key in seen:
                continue

            seen.add(key)
            result.append(execution_signal(row))

    return result


def self_test() -> None:
    row = {
        "symbol": "ENA/EUR",
        "strategy": "trend_breakout",
        "side": "LONG",
        "market_regime": "BULLISH",
        "shadow_eligible": "true",
        "candle_timestamp": "2026-08-21T09:00:00+00:00",
        "entry_price": "0.5",
        "take_profit": "0.52",
        "stop_loss": "0.49",
    }

    contract = execution_signal(row)

    assert contract["symbol"] == "ENA/EUR"
    assert contract["side"] == "LONG"
    assert contract["strategy"] == "trend_breakout"
    assert contract["entry_price"] == 0.5


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    if args.self_test:
        self_test()
        print("DIAMOND_SELECTIVE_EXECUTION_ADAPTER_SELF_TEST_OK")
        return

    rows = load_candidates()

    print(f"SELECTIVE execution-contracts: {len(rows)}")

    for row in rows[-max(1, args.limit):]:
        print(json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    main()
