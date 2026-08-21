#!/usr/bin/env python3
# Diamond Trader State / Backup Audit v1.0
#
# Controleert of kritieke state- en executionbestanden persistent en
# herstelbaar zijn. Plaatst geen orders en gebruikt geen private API.
#
# De enige schrijftest is een tijdelijk auditbestand in dezelfde directory.
# Dit tijdelijke bestand wordt altijd verwijderd en wijzigt de bot-state niet.

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except Exception:
    yaml = None


VERSION = "1.0"
DEFAULT_DATA_DIR = Path("/var/data")
DEFAULT_CONFIG = Path("config.yaml")
DEFAULT_BOT = Path("diamond_bot.py")


def cfg_value(cfg: Dict[str, Any], dotted: str, default: Any = None) -> Any:
    cur: Any = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def load_config(path: Path) -> Dict[str, Any]:
    if yaml is None or not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def resolve_path(raw: Any, root: Path) -> Path:
    path = Path(str(raw))
    if path.is_absolute():
        return path
    return (root / path).resolve()


def under_directory(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except Exception:
        return False


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


def temporary_atomic_write_test(directory: Path) -> tuple[bool, str]:
    """
    Test create -> flush/fsync -> os.replace -> read -> delete.
    Geen live statebestand wordt aangeraakt.
    """
    if not directory.exists() or not directory.is_dir():
        return False, "directory ontbreekt"

    first: Optional[Path] = None
    second: Optional[Path] = None
    try:
        fd, first_name = tempfile.mkstemp(
            prefix=".diamond_audit_",
            suffix=".tmp",
            dir=str(directory),
        )
        first = Path(first_name)
        payload = b"diamond-persistence-audit-v1\n"
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        second = directory / f"{first.name}.renamed"
        os.replace(first, second)

        if second.read_bytes() != payload:
            return False, "readback wijkt af"

        # fsync directory wanneer ondersteund.
        try:
            dir_fd = os.open(str(directory), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass

        return True, "create/fsync/atomic rename/read/delete OK"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        for candidate in (first, second):
            try:
                if candidate is not None and candidate.exists():
                    candidate.unlink()
            except OSError:
                pass


def temporary_backup_restore_test(state_path: Path) -> tuple[bool, str]:
    """
    Kopieert state naar een tijdelijk backupbestand, vergelijkt SHA256 en
    leest de JSON uit de kopie. De echte state wordt nooit overschreven.
    """
    if not state_path.exists():
        return False, "statebestand ontbreekt"

    backup: Optional[Path] = None
    try:
        fd, name = tempfile.mkstemp(
            prefix=".diamond_state_backup_audit_",
            suffix=".json",
            dir=str(state_path.parent),
        )
        os.close(fd)
        backup = Path(name)

        shutil.copy2(state_path, backup)

        original_hash = sha256_file(state_path)
        backup_hash = sha256_file(backup)

        if original_hash != backup_hash:
            return False, "backup checksum wijkt af"

        data = json.loads(backup.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False, "backup JSON is geen object"

        return True, f"checksum OK | sha256={original_hash[:12]}..."
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        try:
            if backup is not None and backup.exists():
                backup.unlink()
        except OSError:
            pass


def bot_source_checks(path: Path) -> Dict[str, bool]:
    try:
        source = path.read_text(encoding="utf-8")
    except Exception:
        source = ""

    return {
        "atomic_replace": "os.replace(" in source,
        "save_state": "def save_state(" in source,
        "pending_orders": '"pending_orders"' in source,
        "recovery_required": '"recovery_required"' in source,
        "canary_log": "diamond_canary_execution.csv" in source,
    }


def audit(
    *,
    root: Path,
    data_dir: Path,
    config_path: Path,
    bot_path: Path,
) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []
    cfg = load_config(config_path)

    if not cfg:
        add(
            checks,
            "Config",
            "FAIL",
            f"niet leesbaar: {config_path}",
        )
        return {"checks": checks, "status": "FAIL"}

    data_dir = data_dir.resolve()
    root = root.resolve()

    add(
        checks,
        "Persistent data directory",
        "PASS" if data_dir.exists() and data_dir.is_dir() else "FAIL",
        str(data_dir),
    )

    # Kritieke runtimebestanden.
    configured = {
        "State": cfg_value(
            cfg,
            "files.state_file",
            "/var/data/diamond_state.json",
        ),
        "Trades": cfg_value(
            cfg,
            "files.trades_file",
            "/var/data/transactions.csv",
        ),
        "Canary execution log": cfg_value(
            cfg,
            "files.canary_execution_file",
            "/var/data/diamond_canary_execution.csv",
        ),
        "Control": cfg_value(
            cfg,
            "files.control_file",
            "/var/data/diamond_control.json",
        ),
    }

    resolved: Dict[str, Path] = {}
    for name, raw in configured.items():
        path = resolve_path(raw, root)
        resolved[name] = path
        persistent = under_directory(path, data_dir)
        add(
            checks,
            f"{name} pad",
            "PASS" if persistent else "FAIL",
            f"{path} | persistent={'JA' if persistent else 'NEE'}",
        )

    state_path = resolved["State"]
    state = load_json(state_path)

    if state_path.exists() and state is not None:
        pending = state.get("pending_orders") or {}
        recovery = bool(state.get("recovery_required", False))
        add(
            checks,
            "State JSON",
            "PASS",
            f"leesbaar | pending={len(pending)} | recovery_required={recovery}",
        )
    elif state_path.exists():
        add(
            checks,
            "State JSON",
            "FAIL",
            "bestand bestaat maar JSON is ongeldig",
        )
    else:
        add(
            checks,
            "State JSON",
            "FAIL",
            f"ontbreekt: {state_path}",
        )

    source_checks = bot_source_checks(bot_path)
    add(
        checks,
        "Atomic state save code",
        "PASS"
        if source_checks["save_state"] and source_checks["atomic_replace"]
        else "FAIL",
        "save_state + os.replace"
        if source_checks["save_state"] and source_checks["atomic_replace"]
        else "atomic save niet volledig aangetroffen",
    )
    add(
        checks,
        "Recoveryvelden in code",
        "PASS"
        if source_checks["pending_orders"] and source_checks["recovery_required"]
        else "FAIL",
        "pending_orders + recovery_required",
    )
    add(
        checks,
        "Canary log code",
        "PASS" if source_checks["canary_log"] else "FAIL",
        "persistent canary execution-log aanwezig in code",
    )

    atomic_ok, atomic_detail = temporary_atomic_write_test(data_dir)
    add(
        checks,
        "Persistent atomic write test",
        "PASS" if atomic_ok else "FAIL",
        atomic_detail,
    )

    backup_ok, backup_detail = temporary_backup_restore_test(state_path)
    add(
        checks,
        "State backup/restore test",
        "PASS" if backup_ok else "FAIL",
        backup_detail,
    )

    # Files die vóór live nog niet hoeven te bestaan.
    future_files = {
        "Canary execution CSV": resolved["Canary execution log"],
        "Canary analysis JSON": data_dir / "diamond_canary_log_analysis.json",
        "Release phase status": data_dir / "diamond_release_phase_status.json",
    }

    for name, path in future_files.items():
        persistent = under_directory(path, data_dir)
        exists = path.exists()
        status = "PASS" if persistent else "FAIL"
        detail = (
            f"{path} | persistent=JA | "
            + ("aanwezig" if exists else "nog niet aanwezig (toegestaan vóór canary)")
            if persistent
            else f"{path} | persistent=NEE"
        )
        add(checks, name, status, detail)

    hard_failures = [
        check
        for check in checks
        if check.get("hard", True) and check["status"] == "FAIL"
    ]

    return {
        "checks": checks,
        "status": "PASS" if not hard_failures else "FAIL",
        "hard_failures": hard_failures,
        "state_file": str(state_path),
        "data_dir": str(data_dir),
    }


def print_report(result: Dict[str, Any]) -> None:
    print("=" * 78)
    print(f" DIAMOND STATE / BACKUP AUDIT v{VERSION}")
    print("=" * 78)

    for check in result["checks"]:
        print(
            f"[{check['status']:<4}] "
            f"{check['name']:<30} | {check['detail']}"
        )

    print("\n=== EINDOORDEEL ===")
    print(result["status"])

    if result["status"] != "PASS":
        print("Blokkers:")
        for check in result.get("hard_failures") or []:
            print(f"- {check['name']}: {check['detail']}")

    print("\nBot-state gewijzigd : NEE")
    print("Echte orders        : NEE")
    print("Private API         : NEE")
    print("Live/config wijziging: NEE")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit Diamond Trader persistent state en backup/herstelbaarheid."
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--data-dir", default="/var/data")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--bot", default="diamond_bot.py")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root)
    result = audit(
        root=root,
        data_dir=Path(args.data_dir),
        config_path=Path(args.config),
        bot_path=Path(args.bot),
    )
    print_report(result)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
