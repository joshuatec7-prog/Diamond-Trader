#!/usr/bin/env python3
"""
Diamond Trader quarter-hour aligned periodic runner.

Doel:
- laat de bestaande periodic_analysis_runner.py pas starten vlak na een echte
  15m candle-close (:00/:15/:30/:45 UTC);
- de bestaande periodic runner houdt daarna zelf exact 900 seconden tussen
  cyclusstarts, zodat de alignment behouden blijft;
- geen strategie-, stake-, config- of LIVE-wijzigingen.

De target is +20 seconden na de kwartiergrens. Diagnose draait eerst en duurt
normaal circa 1-2 seconden, waardoor de scanner rond +22 seconden start.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

OFFSET_SECONDS = 20
QUARTER_MINUTES = 15
PROJECT_DIR = Path("/opt/render/project/src")
RUNNER = PROJECT_DIR / "periodic_analysis_runner.py"


def next_target(now: datetime) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    minute = (now.minute // QUARTER_MINUTES) * QUARTER_MINUTES
    target = now.replace(
        minute=minute,
        second=OFFSET_SECONDS,
        microsecond=0,
    )

    if target <= now:
        target += timedelta(minutes=QUARTER_MINUTES)

    return target


def self_test() -> int:
    cases = [
        (datetime(2026, 8, 23, 4, 6, 16, tzinfo=timezone.utc), "2026-08-23T04:15:20+00:00"),
        (datetime(2026, 8, 23, 4, 15, 5, tzinfo=timezone.utc), "2026-08-23T04:15:20+00:00"),
        (datetime(2026, 8, 23, 4, 15, 20, tzinfo=timezone.utc), "2026-08-23T04:30:20+00:00"),
        (datetime(2026, 8, 23, 4, 59, 59, tzinfo=timezone.utc), "2026-08-23T05:00:20+00:00"),
    ]

    for value, expected in cases:
        actual = next_target(value).isoformat()
        assert actual == expected, (actual, expected)

    print("DIAMOND_QUARTER_ALIGNED_PERIODIC_RUNNER_SELF_TEST_OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    if not RUNNER.is_file():
        raise SystemExit(f"FOUT: periodic runner ontbreekt: {RUNNER}")

    now = datetime.now(timezone.utc)
    target = next_target(now)
    wait_seconds = max(0.0, (target - now).total_seconds())

    print(
        "Diamond quarter-aligned periodic runner gestart | "
        f"nu={now.isoformat()} | eerste_cycle={target.isoformat()} | "
        f"wacht={wait_seconds:.1f}s",
        flush=True,
    )

    time.sleep(wait_seconds)

    os.chdir(PROJECT_DIR)
    os.execv(
        sys.executable,
        [sys.executable, str(RUNNER)],
    )


if __name__ == "__main__":
    raise SystemExit(main())
