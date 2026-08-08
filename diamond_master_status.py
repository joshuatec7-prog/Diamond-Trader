#!/usr/bin/env python3

import json
import subprocess
from pathlib import Path

VERSION = "1.0"

PERIODIC = Path("/var/data/diamond_periodic_analysis_state.json")
BTC = Path(
    "/var/data/diamond_market_lead_btc/"
    "btc_market_lead_state_v1_1.json"
)
EARLY = Path(
    "/var/data/diamond_early_entry/"
    "early_entry_state_v1_3_1.json"
)


def load_json(path):
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def run_status(command):
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout
    except Exception as exc:
        return f"FOUT: {type(exc).__name__}: {exc}"


def lines_with(text, needles):
    found = []

    for line in text.splitlines():
        if any(
            line.strip().startswith(n)
            for n in needles
        ):
            found.append(line.strip())

    return found


def memory_mib(path):
    try:
        return int(Path(path).read_text()) / 1024 / 1024
    except Exception:
        return None


def print_section(title, text):
    print()
    print("=" * 68)
    print(title)
    print("=" * 68)
    print(text.strip() or "GEEN DATA")


def compact_status(script, needles):
    text = run_status(
        ["python3", script, "--status"]
    )

    rows = lines_with(text, needles)

    return "\n".join(rows) if rows else text


def show_memory():
    current = memory_mib(
        "/sys/fs/cgroup/memory.current"
    )
    peak = memory_mib(
        "/sys/fs/cgroup/memory.peak"
    )

    print()
    print("=" * 68)
    print("GEHEUGEN")
    print("=" * 68)

    if current is not None:
        print(f"Current : {current:.1f} MiB")

    if peak is not None:
        print(f"Peak    : {peak:.1f} MiB")

    try:
        events = Path(
            "/sys/fs/cgroup/memory.events"
        ).read_text().strip()

        print(events)
    except Exception:
        pass


def show_btc():
    d = load_json(BTC)

    print()
    print("=" * 68)
    print("BTC MARKET LEAD")
    print("=" * 68)

    if not d:
        print("GEEN STATE")
        return

    print("Status  :", d.get("status"))
    print("Cycles  :", d.get("cycles"))
    print("Samples :", d.get("samples_written"))
    print("CB err  :", d.get("coinbase_errors"))
    print("BV err  :", d.get("bitvavo_errors"))

    mem = d.get("memory") or {}
    print(
        "Max mem :",
        mem.get("max_cgroup_mib"),
        "MiB"
    )


def show_early():
    d = load_json(EARLY)

    print()
    print("=" * 68)
    print("EARLY ENTRY")
    print("=" * 68)

    if not d:
        print("GEEN STATE")
        return

    print("Samples :", d.get("samples_written"))
    print("Cycles  :", d.get("cycles"))
    print("Errors  :", d.get("errors_total"))
    print("Update  :", d.get("last_update"))


def show_periodic():
    d = load_json(PERIODIC)

    print()
    print("=" * 68)
    print("PERIODIC RUNNER")
    print("=" * 68)

    print("Versie :", d.get("version"))
    print("Cycle  :", d.get("cycle_count"))

    for name, row in (d.get("tasks") or {}).items():
        print(
            f"{name:26s} "
            f"{row.get('last_status','-'):4s} "
            f"runs={row.get('run_count',0)}"
        )


def main():
    print("=" * 68)
    print("DIAMOND TRADER MASTER STATUS v1.1")
    print("=" * 68)

    show_periodic()
    show_btc()
    show_early()
    show_memory()

    print_section(
        "SELECTIVE / STRONG",
        compact_status(
            "scanner_selective_shadow_lab.py",
            ("CURRENT", "SELECTIVE", "STRONG"),
        ),
    )

    print_section(
        "SESSION SHADOW",
        compact_status(
            "scanner_session_shadow_lab.py",
            ("CURRENT", "NO_12_17", "ONLY_00_05"),
        ),
    )

    print_section(
        "LONG ENTRY",
        compact_status(
            "long_entry_shadow_lab.py",
            ("CURRENT", "WAIT_15M", "WAIT_30M"),
        ),
    )

    print_section(
        "LONG COMBO v2",
        compact_status(
            "long_combo_shadow_lab_v2.py",
            ("CURRENT", "WAIT15_100", "WAIT15_050"),
        ),
    )

    print_section(
        "LONG MIN PROFIT",
        compact_status(
            "long_min_profit_shadow_lab.py",
            ("CURRENT_100", "TEST_050", "TEST_025"),
        ),
    )

    print_section(
        "SELECTIVE SESSION SHADOW",
        run_status([
            "python3",
            "selective_session_shadow_v1_0.py",
        ]),
    )

    print_section(
        "SECOND CHANCE SHADOW",
        run_status([
            "python3",
            "second_chance_shadow_v1_0.py",
        ]),
    )

    print_section(
        "SHORT TRIGGER SHADOW",
        run_status([
            "python3",
            "short_trigger_shadow_v1_0.py",
            "--update",
        ]),
    )

    print_section(
        "CAPITAL ALLOCATION",
        "\n".join(
            lines_with(
                run_status([
                    "python3",
                    "capital_allocation_shadow_v1_0.py",
                    "--update",
                ]),
                ("BASE", "RR160", "QUALITY", "STRONG_QUALITY"),
            )
        ),
    )

    readiness = run_status(
        ["python3", "readiness_gate.py"]
    )

    print_section(
        "READINESS",
        "\n".join(
            lines_with(
                readiness,
                (
                    "Status",
                    "Fase",
                    "Totale testvoortgang",
                    "Volgende stap",
                    "Longtest",
                    "Paper-shorttest",
                ),
            )
        ),
    )


if __name__ == "__main__":
    main()
