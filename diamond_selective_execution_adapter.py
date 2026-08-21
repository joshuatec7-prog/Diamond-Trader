import argparse
import csv
import json
from pathlib import Path
from typing import Any, Dict, List

from diamond_selective_rules import (
    execution_signal,
    selective_accepts,
    selective_candidate_key,
)

DEFAULT_SIGNALS = Path("/var/data/diamond_market_signals.csv")


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
