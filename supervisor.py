from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

STOP = False
REPORT_MAX_AGE_SECONDS = 35 * 60
REPORT_STARTUP_GRACE_SECONDS = 5 * 60
REPORT_HEALTH_CHECK_SECONDS = 30


@dataclass
class Child:
    cmd: list[str]
    critical: bool
    report_path: str | None = None
    proc: subprocess.Popen | None = None
    restart_at: float = 0.0
    started_at: float = 0.0
    next_health_check_at: float = 0.0
    health_stop_requested: bool = False


CHILDREN: list[Child] = []


def _stop(signum: int, frame: object) -> None:
    global STOP
    STOP = True
    print(f'[SUPERVISOR] stop-signaal ontvangen: {signum}', flush=True)


def _label(child: Child) -> str:
    return ' '.join(child.cmd[2:])


def _start_child(child: Child) -> None:
    child.proc = subprocess.Popen(child.cmd)
    child.restart_at = 0.0
    child.started_at = time.time()
    child.next_health_check_at = child.started_at + REPORT_STARTUP_GRACE_SECONDS
    child.health_stop_requested = False
    kind = 'critical' if child.critical else 'monitor'
    print(f'[SUPERVISOR] gestart pid={child.proc.pid} | {_label(child)} | {kind}', flush=True)


def _default_data_path(filename: str) -> str:
    data = Path('/var/data')
    return str(data / filename) if data.exists() else str(Path('data') / filename)


def _report_health_error(child: Child, now: float | None = None) -> str | None:
    if not child.report_path:
        return None
    current = time.time() if now is None else now
    if current - child.started_at < REPORT_STARTUP_GRACE_SECONDS:
        return None
    path = Path(child.report_path)
    if not path.exists():
        return f'rapport ontbreekt: {path}'
    try:
        report = json.loads(path.read_text(encoding='utf-8'))
        generated_ms = int(report.get('generated_at_ms', 0))
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return f'rapport ongeldig: {type(exc).__name__}'
    age_seconds = current - generated_ms / 1000.0
    if generated_ms <= 0 or age_seconds > REPORT_MAX_AGE_SECONDS:
        return f'rapport verouderd: {max(0.0, age_seconds)/60.0:.1f} min'
    return None


def _terminate_children() -> None:
    for child in CHILDREN:
        proc = child.proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
    deadline = time.time() + 10.0
    for child in CHILDREN:
        proc = child.proc
        if proc is None:
            continue
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.1)
        if proc.poll() is None:
            proc.kill()
    for child in CHILDREN:
        proc = child.proc
        if proc is None:
            continue
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def main() -> int:
    global STOP, CHILDREN
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # De verliesgevende PAPER-strategieën en langdurige researchprocessen blijven als
    # broncode en database bewaard, maar worden niet meer gestart. Alleen de twee
    # actuele read-only monitors blijven actief; beide kunnen nooit orders plaatsen.
    scanner_report = (
        os.getenv('SCANNER_V3_REPORT_PATH')
        or os.getenv('SCANNER_V2_REPORT_PATH')
        or _default_data_path('cryptobot_scanner_v3.json')
    )
    funding_report = os.getenv('FUNDING_MONITOR_REPORT_PATH') or _default_data_path(
        'cryptobot_funding_basis_v3.json'
    )
    CHILDREN = [
        Child(
            [sys.executable, '-u', 'crypto_scanner_v2.py'],
            critical=False,
            report_path=scanner_report,
        ),
        Child(
            [sys.executable, '-u', 'funding_basis_monitor.py'],
            critical=False,
            report_path=funding_report,
        ),
    ]

    for child in CHILDREN:
        _start_child(child)

    exit_code = 0
    try:
        while not STOP:
            now = time.time()
            for child in CHILDREN:
                proc = child.proc
                if proc is None:
                    if not child.critical and now >= child.restart_at:
                        _start_child(child)
                    continue
                rc = proc.poll()
                if rc is None:
                    if not child.health_stop_requested and now >= child.next_health_check_at:
                        child.next_health_check_at = now + REPORT_HEALTH_CHECK_SECONDS
                        health_error = _report_health_error(child, now)
                        if health_error:
                            print(
                                f'[SUPERVISOR] {_label(child)} ongezond: {health_error}; herstart',
                                flush=True,
                            )
                            child.health_stop_requested = True
                            proc.terminate()
                    continue
                if child.critical:
                    print(f'[SUPERVISOR] critical child {_label(child)} gestopt rc={rc}', flush=True)
                    exit_code = rc if rc != 0 else 1
                    STOP = True
                    break
                print(
                    f'[SUPERVISOR] research child {_label(child)} gestopt rc={rc}; herstart over 5s',
                    flush=True,
                )
                child.proc = None
                child.restart_at = now + 5.0
            if not STOP:
                time.sleep(0.5)
    finally:
        _terminate_children()
    return exit_code


if __name__ == '__main__':
    sys.exit(main())

