#!/usr/bin/env python3
# Diamond Trader Research Data Retention / Rotation v1.2
#
# Veilige, begrensde retentie voor ALLEEN Lijst-4 researchdata.
# Kritieke bot-state, trades, canary logs, config en control-bestanden
# worden NOOIT door dit script verwijderd of gewijzigd.
#
# Standaard = DRY-RUN.
# Alleen met --apply worden dagelijkse gzip-snapshots gemaakt en oude
# research-archives binnen de eigen archive-map opgeruimd.

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


VERSION = "1.2"
DATA = Path("/var/data")
ARCHIVE = DATA / "diamond_research_archive"

RETENTION_DAYS = 14
MAX_SNAPSHOTS_PER_FAMILY = 14
MIN_KEEP_PER_FAMILY = 3
ARCHIVE_CAP_BYTES = 50 * 1024 * 1024
STALE_TMP_HOURS = 24

# ENIGE canonieke bronbestanden die dit script mag archiveren.
RESEARCH_SOURCES = {
    "dynamic_universe": DATA / "diamond_dynamic_universe.json",
    "crypto_news_events": DATA / "diamond_crypto_news_events.json",
    "event_market_fusion": DATA / "diamond_event_market_fusion.json",
    "shadow_admission_queue": DATA / "diamond_shadow_admission_queue.json",
    "deep_scan_schedule": DATA / "diamond_deep_scan_schedule.json",
    "multi_exchange_confirmation": DATA / "diamond_multi_exchange_confirmation.json",
    "selective_prospective_state": DATA / "diamond_selective_prospective_candidate_state.json",
    "selective_prospective_report": DATA / "diamond_selective_prospective_candidate_report.json",
    "event_outcome_state": DATA / "diamond_event_outcome_tracker_state.json",
    "event_outcome_report": DATA / "diamond_event_outcome_tracker_report.json",
    "entry_timing_prospective_state": DATA / "diamond_entry_timing_prospective_state.json",
    "entry_timing_prospective_report": DATA / "diamond_entry_timing_prospective_report.json",
}

# Extra expliciete beschermingscontrole. Deze bestanden vallen sowieso buiten
# RESEARCH_SOURCES, maar de guard voorkomt toekomstige programmeerfouten.
PROTECTED_BASENAMES = {
    "diamond_state.json",
    "diamond_agent_state.json",
    "diamond_control.json",
    "diamond_live_approval.json",
    "diamond_release_phase_status.json",
    "diamond_canary_execution.csv",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def human_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024.0 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GiB"


def safe_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def atomic_gzip_copy(source: Path, target: Path) -> int:
    if source.name in PROTECTED_BASENAMES:
        raise RuntimeError(f"PROTECTED_SOURCE:{source.name}")

    if not source.is_file() or source.is_symlink():
        raise RuntimeError(f"INVALID_SOURCE:{source}")

    target.parent.mkdir(parents=True, exist_ok=True)

    if not safe_under(target, ARCHIVE):
        raise RuntimeError(f"OUTSIDE_ARCHIVE:{target}")

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    os.close(fd)
    tmp = Path(tmp_name)

    try:
        with source.open("rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        os.replace(tmp, target)
        return target.stat().st_size
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def family_dir(family: str) -> Path:
    return ARCHIVE / family


def snapshot_target(family: str, day: str) -> Path:
    return family_dir(family) / f"{day}.json.gz"


def archive_files(family: str) -> List[Path]:
    directory = family_dir(family)
    if not directory.exists():
        return []

    files = []
    for path in directory.glob("*.json.gz"):
        if path.is_symlink() or not path.is_file():
            continue
        if not safe_under(path, ARCHIVE):
            continue
        files.append(path)

    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def stale_tmp_files(now: datetime) -> List[Path]:
    if not ARCHIVE.exists():
        return []

    cutoff = now.timestamp() - (STALE_TMP_HOURS * 3600)
    found = []

    for path in ARCHIVE.rglob("*.tmp"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            if not safe_under(path, ARCHIVE):
                continue
            if path.stat().st_mtime < cutoff:
                found.append(path)
        except OSError:
            continue

    return found


def retention_candidates(
    family: str,
    *,
    now: datetime,
) -> List[Tuple[Path, str]]:
    files = archive_files(family)
    if not files:
        return []

    keep = set(files[:MIN_KEEP_PER_FAMILY])
    delete: Dict[Path, str] = {}

    age_cutoff = now - timedelta(days=RETENTION_DAYS)
    for path in files:
        if path in keep:
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except OSError:
            continue
        if mtime < age_cutoff:
            delete[path] = "OUDER_DAN_RETENTIE"

    for path in files[MAX_SNAPSHOTS_PER_FAMILY:]:
        if path in keep:
            continue
        delete.setdefault(path, "BOVEN_MAX_SNAPSHOTS")

    return sorted(
        delete.items(),
        key=lambda item: item[0].stat().st_mtime if item[0].exists() else 0,
    )


def archive_size() -> int:
    total = 0
    if not ARCHIVE.exists():
        return 0

    for path in ARCHIVE.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            if not safe_under(path, ARCHIVE):
                continue
            total += path.stat().st_size
        except OSError:
            continue
    return total


def cap_candidates(
    *,
    already_marked: set[Path],
) -> List[Tuple[Path, str]]:
    current_size = archive_size()

    # Reeds gemarkeerde bestanden tellen virtueel al als verwijderd.
    virtual_size = current_size
    for path in already_marked:
        try:
            virtual_size -= path.stat().st_size
        except OSError:
            pass

    if virtual_size <= ARCHIVE_CAP_BYTES:
        return []

    all_files: List[Tuple[float, Path, str]] = []
    family_counts: Dict[str, int] = {}

    for family in RESEARCH_SOURCES:
        files = archive_files(family)
        remaining = [p for p in files if p not in already_marked]
        family_counts[family] = len(remaining)

        for path in remaining:
            try:
                all_files.append((path.stat().st_mtime, path, family))
            except OSError:
                continue

    all_files.sort(key=lambda x: x[0])
    selected = []

    for _, path, family in all_files:
        if virtual_size <= ARCHIVE_CAP_BYTES:
            break
        if family_counts.get(family, 0) <= MIN_KEEP_PER_FAMILY:
            continue

        try:
            size = path.stat().st_size
        except OSError:
            continue

        selected.append((path, "ARCHIVE_CAP"))
        family_counts[family] -= 1
        virtual_size -= size

    return selected


def build_plan(now: datetime) -> Dict[str, Any]:
    day = now.strftime("%Y-%m-%d")

    source_rows = []
    for family, source in RESEARCH_SOURCES.items():
        exists = source.is_file() and not source.is_symlink()
        target = snapshot_target(family, day)
        source_rows.append({
            "family": family,
            "source": str(source),
            "exists": exists,
            "source_size": source.stat().st_size if exists else 0,
            "today_snapshot_exists": target.is_file(),
            "snapshot_target": str(target),
        })

    delete_map: Dict[Path, str] = {}
    for family in RESEARCH_SOURCES:
        for path, reason in retention_candidates(family, now=now):
            delete_map[path] = reason

    for path, reason in cap_candidates(already_marked=set(delete_map)):
        delete_map.setdefault(path, reason)

    tmp_files = stale_tmp_files(now)

    return {
        "version": VERSION,
        "generated_at": now.isoformat(),
        "mode_default": "DRY_RUN",
        "research_only": True,
        "archive_root": str(ARCHIVE),
        "retention_days": RETENTION_DAYS,
        "max_snapshots_per_family": MAX_SNAPSHOTS_PER_FAMILY,
        "min_keep_per_family": MIN_KEEP_PER_FAMILY,
        "archive_cap_bytes": ARCHIVE_CAP_BYTES,
        "archive_size_before": archive_size(),
        "sources": source_rows,
        "delete_candidates": [
            {
                "path": str(path),
                "reason": reason,
                "size": path.stat().st_size if path.exists() else 0,
            }
            for path, reason in sorted(
                delete_map.items(),
                key=lambda item: str(item[0]),
            )
        ],
        "stale_tmp": [
            {
                "path": str(path),
                "size": path.stat().st_size if path.exists() else 0,
            }
            for path in tmp_files
        ],
        "protected_basenames": sorted(PROTECTED_BASENAMES),
        "critical_state_touched": False,
        "orders_used": False,
        "private_api_used": False,
        "config_changed": False,
        "symbols_changed": False,
        "live_changed": False,
    }


def apply_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    ARCHIVE.mkdir(parents=True, exist_ok=True)

    snapshots_created = []
    snapshots_skipped = []

    for row in plan["sources"]:
        if not row["exists"]:
            continue

        source = Path(row["source"])
        target = Path(row["snapshot_target"])

        if source.name in PROTECTED_BASENAMES:
            raise RuntimeError(f"PROTECTED_SOURCE:{source.name}")

        if target.exists():
            snapshots_skipped.append(str(target))
            continue

        size = atomic_gzip_copy(source, target)
        snapshots_created.append({
            "path": str(target),
            "size": size,
        })

    deleted = []
    for row in plan["delete_candidates"]:
        path = Path(row["path"])
        if not path.exists():
            continue
        if not safe_under(path, ARCHIVE):
            raise RuntimeError(f"DELETE_OUTSIDE_ARCHIVE:{path}")
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"INVALID_DELETE_TARGET:{path}")

        size = path.stat().st_size
        path.unlink()
        deleted.append({
            "path": str(path),
            "size": size,
            "reason": row["reason"],
        })

    stale_tmp_deleted = []
    for row in plan["stale_tmp"]:
        path = Path(row["path"])
        if not path.exists():
            continue
        if not safe_under(path, ARCHIVE):
            raise RuntimeError(f"TMP_OUTSIDE_ARCHIVE:{path}")
        if path.is_symlink() or not path.is_file():
            continue
        size = path.stat().st_size
        path.unlink()
        stale_tmp_deleted.append({
            "path": str(path),
            "size": size,
        })

    return {
        **plan,
        "applied": True,
        "snapshots_created": snapshots_created,
        "snapshots_skipped": snapshots_skipped,
        "deleted": deleted,
        "stale_tmp_deleted": stale_tmp_deleted,
        "archive_size_after": archive_size(),
        "critical_state_touched": False,
    }


def print_report(plan: Dict[str, Any], *, applied: bool) -> None:
    print("=" * 78)
    print(f" DIAMOND RESEARCH DATA RETENTION / ROTATION v{VERSION}")
    print("=" * 78)
    print(f"Modus                  : {'APPLY' if applied else 'DRY-RUN'}")
    print(f"Research-bronnen       : {len(plan['sources'])}")
    print(
        "Bronnen aanwezig      : "
        f"{sum(1 for row in plan['sources'] if row['exists'])}"
    )
    print(
        "Snapshots vandaag     : "
        f"{sum(1 for row in plan['sources'] if row['today_snapshot_exists'])}"
    )
    print(
        "Opruimkandidaten      : "
        f"{len(plan['delete_candidates'])}"
    )
    print(
        "Verouderde tmp        : "
        f"{len(plan['stale_tmp'])}"
    )
    print(
        "Archive vóór          : "
        f"{human_bytes(plan['archive_size_before'])}"
    )
    print(f"Archive limiet         : {human_bytes(ARCHIVE_CAP_BYTES)}")
    print(f"Retentie               : {RETENTION_DAYS} dagen")
    print(
        "Max snapshots/familie : "
        f"{MAX_SNAPSHOTS_PER_FAMILY}"
    )
    print(
        "Min bewaren/familie   : "
        f"{MIN_KEEP_PER_FAMILY}"
    )

    print("\n=== RESEARCH BRONNEN ===")
    for row in plan["sources"]:
        state = "AANWEZIG" if row["exists"] else "ONTBREEKT"
        today = "JA" if row["today_snapshot_exists"] else "NEE"
        print(
            f"{row['family']:<28} "
            f"{state:<9} "
            f"{human_bytes(row['source_size']):>10} "
            f"snapshot_vandaag={today}"
        )

    print("\n=== VEILIGHEID ===")
    print("Alleen eigen research archive : JA")
    print("Bot-state/trades/canary        : ONAANGERAAKT")
    print("Config/symbols                  : ONGEWIJZIGD")
    print("Orders/private API              : NEE")
    print("Live wijziging                  : NEE")

    if applied:
        print("\n=== APPLY RESULTAAT ===")
        print(
            "Snapshots gemaakt     : "
            f"{len(plan.get('snapshots_created', []))}"
        )
        print(
            "Snapshots overgeslagen: "
            f"{len(plan.get('snapshots_skipped', []))}"
        )
        print(
            "Archives verwijderd   : "
            f"{len(plan.get('deleted', []))}"
        )
        print(
            "Tmp verwijderd        : "
            f"{len(plan.get('stale_tmp_deleted', []))}"
        )
        print(
            "Archive na            : "
            f"{human_bytes(plan.get('archive_size_after', 0))}"
        )
    else:
        print("\nDRY-RUN: er is niets aangemaakt of verwijderd.")
        print("Gebruik --apply pas na controle van deze uitvoer.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Maak snapshots en voer retentie uit. Zonder deze vlag: dry-run.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    current = now_utc()
    plan = build_plan(current)

    if args.apply:
        result = apply_plan(plan)
        print_report(result, applied=True)
    else:
        print_report(plan, applied=False)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
