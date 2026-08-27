from __future__ import annotations

import signal
import subprocess
import sys
import time
from dataclasses import dataclass

STOP = False


@dataclass
class Child:
    cmd: list[str]
    critical: bool
    proc: subprocess.Popen | None = None
    restart_at: float = 0.0


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
    kind = 'critical' if child.critical else 'research'
    print(
        f'[SUPERVISOR] gestart pid={child.proc.pid} | {_label(child)} | {kind}',
        flush=True,
    )


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

    CHILDREN = [
        Child([sys.executable, '-u', 'main.py'], critical=True),
        Child([sys.executable, '-u', 'trend_v3_main.py'], critical=True),
        Child([sys.executable, '-u', 'continuation_v2_main.py'], critical=True),
        Child([sys.executable, '-u', 'audit_all.py'], critical=False),
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
                    continue

                if child.critical:
                    print(
                        f'[SUPERVISOR] critical child {_label(child)} gestopt rc={rc}',
                        flush=True,
                    )
                    exit_code = rc if rc != 0 else 1
                    STOP = True
                    break

                print(
                    f'[SUPERVISOR] research child {_label(child)} gestopt rc={rc}; '
                    'herstart over 5s',
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
