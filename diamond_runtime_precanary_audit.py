#!/usr/bin/env python3
# Diamond Trader Disk / Memory / Runtime Pre-Canary Audit v1.0
#
# Read-only infrastructuurcontrole.
# Geen orders, geen private API en geen live/config wijziging.

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


VERSION = "1.0"

DEFAULT_ROOT = Path(".")
DEFAULT_DATA_DIR = Path("/var/data")
DEFAULT_PROC_ROOT = Path("/proc")

REQUIRED_PROCESSES = (
    "agent.py",
    "supervisor_agent.py",
    "closed_candle_runner.py",
    "periodic_analysis_runner.py",
)

MIB = 1024 * 1024


def add(
    checks: List[Dict[str, Any]],
    name: str,
    status: str,
    detail: str,
    hard: bool = True,
) -> None:
    checks.append(
        {
            "name": name,
            "status": status,
            "detail": detail,
            "hard": hard,
        }
    )


def pct(part: float, total: float) -> float:
    if total <= 0:
        return 0.0
    return (part / total) * 100.0


def disk_status(path: Path) -> Tuple[str, str]:
    try:
        usage = shutil.disk_usage(path)
    except Exception as exc:
        return "FAIL", f"disk usage niet leesbaar: {type(exc).__name__}"

    free_mib = usage.free / MIB
    used_pct = pct(usage.used, usage.total)

    if free_mib < 100 or used_pct >= 95:
        status = "FAIL"
    elif free_mib < 250 or used_pct >= 85:
        status = "WARNING"
    else:
        status = "PASS"

    detail = (
        f"totaal={usage.total / MIB:.1f} MiB | "
        f"gebruikt={usage.used / MIB:.1f} MiB ({used_pct:.1f}%) | "
        f"vrij={free_mib:.1f} MiB"
    )
    return status, detail


def parse_meminfo(proc_root: Path) -> Dict[str, int]:
    path = proc_root / "meminfo"
    values: Dict[str, int] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return values

    for line in text.splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        match = re.search(r"(\d+)", raw)
        if match:
            values[key.strip()] = int(match.group(1)) * 1024
    return values


def memory_status(proc_root: Path) -> Tuple[str, str]:
    info = parse_meminfo(proc_root)
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", 0)

    if total <= 0:
        return "FAIL", "MemTotal/MemAvailable niet leesbaar"

    used = max(0, total - available)
    available_pct = pct(available, total)
    available_mib = available / MIB

    # Hard fail alleen bij echt kleine vrije marge.
    if available_mib < 128 or available_pct < 10:
        status = "FAIL"
    elif available_mib < 256 or available_pct < 20:
        status = "WARNING"
    else:
        status = "PASS"

    detail = (
        f"totaal={total / MIB:.1f} MiB | "
        f"gebruikt≈{used / MIB:.1f} MiB | "
        f"beschikbaar={available_mib:.1f} MiB ({available_pct:.1f}%)"
    )
    return status, detail


def read_cmdline(path: Path) -> str:
    try:
        data = path.read_bytes()
        return data.replace(b"\x00", b" ").decode("utf-8", errors="ignore").strip()
    except Exception:
        return ""


def read_status_rss(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return 0

    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            match = re.search(r"(\d+)", line)
            if match:
                return int(match.group(1)) * 1024
    return 0


def process_snapshot(proc_root: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not proc_root.exists():
        return rows

    for child in proc_root.iterdir():
        if not child.name.isdigit():
            continue
        cmdline = read_cmdline(child / "cmdline")
        if not cmdline:
            continue

        rows.append(
            {
                "pid": int(child.name),
                "cmdline": cmdline,
                "rss": read_status_rss(child / "status"),
            }
        )

    return rows


def process_checks(
    checks: List[Dict[str, Any]],
    proc_root: Path,
) -> int:
    snapshot = process_snapshot(proc_root)
    total_rss = 0

    for pattern in REQUIRED_PROCESSES:
        matched = [
            row
            for row in snapshot
            if pattern in row["cmdline"]
        ]
        rss = sum(row["rss"] for row in matched)
        total_rss += rss

        if not matched:
            add(
                checks,
                f"Proces {pattern}",
                "FAIL",
                "niet actief",
            )
        else:
            add(
                checks,
                f"Proces {pattern}",
                "PASS",
                f"aantal={len(matched)} | RSS={rss / MIB:.1f} MiB",
            )

    return total_rss


def start_script_check(root: Path) -> Tuple[str, str]:
    path = root / "start.sh"
    if not path.exists():
        return "FAIL", "start.sh ontbreekt"

    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return "FAIL", f"start.sh niet leesbaar: {type(exc).__name__}"

    missing = [
        pattern
        for pattern in REQUIRED_PROCESSES
        if pattern not in text
    ]

    try:
        syntax = subprocess.run(
            ["bash", "-n", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        syntax_ok = syntax.returncode == 0
    except Exception:
        syntax_ok = False

    if missing or not syntax_ok:
        bits = []
        if missing:
            bits.append("ontbreekt in start.sh: " + ", ".join(missing))
        if not syntax_ok:
            bits.append("bash -n FAIL")
        return "FAIL", " | ".join(bits)

    return "PASS", "bash -n OK | alle hoofdprocessen opgenomen"


def data_dir_check(path: Path) -> Tuple[str, str]:
    if not path.exists() or not path.is_dir():
        return "FAIL", "directory ontbreekt"

    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        return "FAIL", "niet volledig lees/schrijf/toegankelijk"

    return "PASS", f"{path} | lees/schrijf/toegang OK"


def uptime_check(proc_root: Path) -> Tuple[str, str]:
    path = proc_root / "uptime"
    try:
        first = path.read_text(encoding="utf-8").split()[0]
        seconds = float(first)
    except Exception:
        return "WARNING", "uptime niet leesbaar"

    hours = seconds / 3600.0
    # Uptime is informatief; korte uptime is niet per se fout na een deploy.
    return "PASS", f"{hours:.2f} uur"


def audit(
    root: Path,
    data_dir: Path,
    proc_root: Path,
    *,
    skip_processes: bool = False,
) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []

    status, detail = data_dir_check(data_dir)
    add(checks, "Persistent data directory", status, detail)

    status, detail = disk_status(data_dir)
    add(checks, "Persistent disk", status, detail)

    status, detail = memory_status(proc_root)
    add(checks, "Geheugen headroom", status, detail)

    status, detail = uptime_check(proc_root)
    add(checks, "Runtime uptime", status, detail, hard=False)

    if not skip_processes:
        total_rss = process_checks(checks, proc_root)
        add(
            checks,
            "Hoofdprocessen RSS totaal",
            "PASS",
            f"{total_rss / MIB:.1f} MiB",
            hard=False,
        )

    status, detail = start_script_check(root)
    add(checks, "Restart start.sh", status, detail)

    state = data_dir / "diamond_state.json"
    if state.exists():
        try:
            size = state.stat().st_size
            add(
                checks,
                "State bestand",
                "PASS",
                f"aanwezig | {size / 1024:.1f} KiB",
            )
        except OSError:
            add(checks, "State bestand", "FAIL", "stat mislukt")
    else:
        add(checks, "State bestand", "FAIL", "ontbreekt")

    # Alleen informatief: grootste bestanden in /var/data.
    largest: List[Tuple[int, Path]] = []
    try:
        for path in data_dir.iterdir():
            if not path.is_file():
                continue
            try:
                largest.append((path.stat().st_size, path))
            except OSError:
                continue
    except OSError:
        pass

    largest.sort(reverse=True)
    top = largest[:3]
    detail = ", ".join(
        f"{path.name}={size / MIB:.1f} MiB"
        for size, path in top
    ) or "geen bestanden"
    add(
        checks,
        "Grootste /var/data bestanden",
        "PASS",
        detail,
        hard=False,
    )

    hard_failures = [
        item
        for item in checks
        if item["hard"] and item["status"] == "FAIL"
    ]
    warnings = [
        item
        for item in checks
        if item["status"] == "WARNING"
    ]

    if hard_failures:
        overall = "FAIL"
    elif warnings:
        overall = "WARNING"
    else:
        overall = "PASS"

    return {
        "status": overall,
        "checks": checks,
        "hard_failures": hard_failures,
        "warnings": warnings,
    }


def print_report(result: Dict[str, Any]) -> None:
    print("=" * 78)
    print(f" DIAMOND DISK / MEMORY / RUNTIME PRE-CANARY AUDIT v{VERSION}")
    print("=" * 78)

    for item in result["checks"]:
        print(
            f"[{item['status']:<7}] "
            f"{item['name']:<30} | {item['detail']}"
        )

    print("\n=== EINDOORDEEL ===")
    print(result["status"])

    if result["hard_failures"]:
        print("Blokkers:")
        for item in result["hard_failures"]:
            print(f"- {item['name']}: {item['detail']}")

    if result["warnings"]:
        print("Warnings:")
        for item in result["warnings"]:
            print(f"- {item['name']}: {item['detail']}")

    print("\nRestart uitgevoerd   : NEE")
    print("Bot-state gewijzigd  : NEE")
    print("Echte orders         : NEE")
    print("Private API          : NEE")
    print("Live/config wijziging: NEE")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only Diamond Trader disk/memory/runtime audit."
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--data-dir", default="/var/data")
    parser.add_argument("--proc-root", default="/proc")
    parser.add_argument(
        "--skip-processes",
        action="store_true",
        help="Alleen voor offline tests.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = audit(
        Path(args.root).resolve(),
        Path(args.data_dir).resolve(),
        Path(args.proc_root).resolve(),
        skip_processes=args.skip_processes,
    )
    print_report(result)

    if result["status"] == "FAIL":
        return 2
    if result["status"] == "WARNING":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
