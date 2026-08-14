#!/usr/bin/env python3
# Diamond Trader API-key / Security Audit v1.0
#
# Read-only security audit:
# - geen private exchange-API;
# - geen orders;
# - geen live/config wijziging;
# - geheime waarden worden nooit geprint.

from __future__ import annotations

import argparse
import os
import re
import stat
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


VERSION = "1.0"

SECRET_ENV_NAMES = (
    "BITVAVO_API_KEY",
    "BITVAVO_API_SECRET",
    "BITVAVO_OPERATOR_ID",
)

TEXT_SUFFIXES = {
    ".py", ".yaml", ".yml", ".json", ".csv", ".log", ".txt",
    ".md", ".sh", ".toml", ".ini", ".cfg", ".conf",
}
EXTRA_TEXT_NAMES = {
    "chat", "start.sh", "requirements.txt", "Dockerfile", "Procfile",
}
SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache",
    "node_modules", ".mypy_cache",
}
MAX_FILE_BYTES = 2 * 1024 * 1024

ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    \b(
        api[_-]?key|
        api[_-]?secret|
        bitvavo[_-]?api[_-]?key|
        bitvavo[_-]?api[_-]?secret|
        access[_-]?token|
        password|
        passwd
    )\b
    \s*[:=]\s*
    ["']([^"']+)["']
    """
)

LOG_RE = re.compile(
    r"(?i)\b(?:log|logger)\s*\.\s*"
    r"(?:debug|info|warning|error|exception|critical)\s*\("
)
SECRET_VAR_RE = re.compile(
    r"(?i)\b(?:api_key|api_secret|bitvavo_api_key|bitvavo_api_secret)\b"
)

SAFE_EXACT_VALUES = {
    "", "changeme", "change_me", "placeholder", "example",
    "dummy", "test", "offline", "none", "null",
    "your_api_key", "your_api_secret",
}
SAFE_PREFIXES = ("${", "{{", "<")


def relpath(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except Exception:
        return str(path)


def is_text_candidate(path: Path) -> bool:
    return path.name in EXTRA_TEXT_NAMES or path.suffix.lower() in TEXT_SUFFIXES


def iter_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            path = Path(current) / name
            if not is_text_candidate(path):
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield path


def read_text(path: Path) -> Optional[str]:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data:
        return None
    for encoding in ("utf-8", "utf-8-sig"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    return None


def safe_literal(value: str) -> bool:
    lower = value.strip().lower()
    return (
        lower in SAFE_EXACT_VALUES
        or any(lower.startswith(prefix) for prefix in SAFE_PREFIXES)
    )


def finding(
    findings: List[Dict[str, Any]],
    severity: str,
    category: str,
    path: str,
    line: Optional[int],
    detail: str,
) -> None:
    findings.append(
        {
            "severity": severity,
            "category": category,
            "path": path,
            "line": line,
            "detail": detail,
        }
    )


def runtime_secrets() -> Dict[str, str]:
    return {
        name: os.getenv(name, "")
        for name in SECRET_ENV_NAMES
        if os.getenv(name, "")
    }


def scan_file(
    path: Path,
    display_root: Path,
    secrets: Dict[str, str],
    findings: List[Dict[str, Any]],
) -> None:
    text = read_text(path)
    if text is None:
        return

    shown_path = relpath(path, display_root)
    lines = text.splitlines()

    # Exacte runtime-secret waarden in bron/config/log/state.
    for env_name, secret_value in secrets.items():
        if len(secret_value) < 6 or secret_value not in text:
            continue
        for idx, line in enumerate(lines, 1):
            if secret_value in line:
                finding(
                    findings,
                    "FAIL",
                    "EXACT_SECRET_LEAK",
                    shown_path,
                    idx,
                    f"exacte waarde van {env_name} aangetroffen; waarde verborgen",
                )

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue

        # Hardcoded waarde zoals api_secret = "abc..."
        for match in ASSIGNMENT_RE.finditer(line):
            value = match.group(2)
            if safe_literal(value):
                continue
            # Environment lookup zelf is juist gewenst.
            lower_line = line.lower()
            if "getenv" in lower_line or "environ" in lower_line:
                continue
            finding(
                findings,
                "FAIL",
                "HARDCODED_SECRET",
                shown_path,
                idx,
                f"hardcoded waarde bij '{match.group(1)}'; waarde verborgen",
            )

        # LOG.info(... self.api_secret ...) etc.
        if LOG_RE.search(line) and SECRET_VAR_RE.search(line):
            finding(
                findings,
                "FAIL",
                "SECRET_IN_LOG_STATEMENT",
                shown_path,
                idx,
                "logging-call verwijst direct naar API-key/secret variabele",
            )

        # .env plaintext is een waarschuwing; exacte runtime waarde blijft FAIL.
        if path.name.lower().startswith(".env"):
            lower = line.lower()
            if (
                "=" in line
                and not stripped.startswith("#")
                and any(
                    token in lower
                    for token in (
                        "bitvavo_api_key",
                        "bitvavo_api_secret",
                        "api_key",
                        "api_secret",
                    )
                )
            ):
                finding(
                    findings,
                    "WARNING",
                    "PLAINTEXT_ENV_FILE",
                    shown_path,
                    idx,
                    "secretveld in lokaal .env-bestand; waarde verborgen",
                )


def permission_checks(root: Path, findings: List[Dict[str, Any]]) -> None:
    for path in (
        root / ".env",
        root / ".env.production",
        root / "config.yaml",
        root / "config.yml",
    ):
        if not path.exists():
            continue
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            continue
        if mode & 0o022:
            finding(
                findings,
                "WARNING",
                "FILE_PERMISSIONS",
                relpath(path, root),
                None,
                f"group/other-writable | mode={oct(mode)}",
            )


def audit(root: Path, data_dir: Path) -> Dict[str, Any]:
    findings: List[Dict[str, Any]] = []
    secrets = runtime_secrets()
    scanned = 0
    seen = set()

    for scan_root, display_root in (
        (root, root),
        (data_dir, data_dir),
    ):
        if not scan_root.exists():
            continue
        for path in iter_files(scan_root):
            try:
                key = str(path.resolve())
            except OSError:
                key = str(path)
            if key in seen:
                continue
            seen.add(key)
            scanned += 1
            scan_file(path, display_root, secrets, findings)

    permission_checks(root, findings)

    fail_count = sum(1 for f in findings if f["severity"] == "FAIL")
    warning_count = sum(1 for f in findings if f["severity"] == "WARNING")

    if fail_count:
        status = "FAIL"
    elif warning_count:
        status = "WARNING"
    else:
        status = "PASS"

    return {
        "status": status,
        "files_scanned": scanned,
        "exact_runtime_check": "PASS" if secrets else "SKIPPED",
        "secret_names": sorted(secrets),
        "fail_count": fail_count,
        "warning_count": warning_count,
        "findings": findings,
    }


def print_report(result: Dict[str, Any]) -> None:
    print("=" * 78)
    print(f" DIAMOND API-KEY / SECURITY AUDIT v{VERSION}")
    print("=" * 78)
    print(f"Bestanden gescand    : {result['files_scanned']}")
    print(f"Exact runtime check  : {result['exact_runtime_check']}")
    if result["secret_names"]:
        print("Runtime secretnamen : " + ", ".join(result["secret_names"]))
    else:
        print("Runtime secretnamen : niet zichtbaar in deze shell")
    print(f"FAIL findings        : {result['fail_count']}")
    print(f"Warnings             : {result['warning_count']}")

    if result["findings"]:
        print("\n=== BEVINDINGEN ===")
        for item in result["findings"]:
            line = f":{item['line']}" if item["line"] is not None else ""
            print(
                f"[{item['severity']}] {item['category']} | "
                f"{item['path']}{line} | {item['detail']}"
            )
    else:
        print("\nGeen secret-lekken of onveilige logging gevonden.")

    print("\n=== EINDOORDEEL ===")
    print(result["status"])

    if result["exact_runtime_check"] == "SKIPPED" and result["status"] == "PASS":
        print(
            "LET OP: statische audit PASS; exacte runtime-secret vergelijking "
            "kon in deze shell niet worden uitgevoerd."
        )

    print("\nSecretwaarden getoond : NEE")
    print("Echte orders          : NEE")
    print("Private API           : NEE")
    print("Live/config wijziging : NEE")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Diamond Trader read-only API-key/security audit."
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--data-dir", default="/var/data")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = audit(
        Path(args.root).resolve(),
        Path(args.data_dir).resolve(),
    )
    print_report(result)
    return 2 if result["status"] == "FAIL" else (1 if result["status"] == "WARNING" else 0)


if __name__ == "__main__":
    raise SystemExit(main())
