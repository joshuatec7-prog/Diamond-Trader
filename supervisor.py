from __future__ import annotations

import signal
import subprocess
import sys
import time

STOP = False
CHILDREN: list[subprocess.Popen] = []


def _stop(signum: int, frame: object) -> None:
    global STOP
    STOP = True
    print(f'[SUPERVISOR] stop-signaal ontvangen: {signum}', flush=True)


def _terminate_children() -> None:
    for proc in CHILDREN:
        if proc.poll() is None:
            proc.terminate()

    deadline = time.time() + 10.0
    for proc in CHILDREN:
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.1)
        if proc.poll() is None:
            proc.kill()

    for proc in CHILDREN:
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def main() -> int:
    global STOP
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    commands = [
        [sys.executable, '-u', 'main.py'],
        [sys.executable, '-u', 'trend_main.py'],
    ]

    for cmd in commands:
        proc = subprocess.Popen(cmd)
        CHILDREN.append(proc)
        print(f'[SUPERVISOR] gestart pid={proc.pid} | {" ".join(cmd[2:])}', flush=True)

    exit_code = 0
    try:
        while not STOP:
            for proc in CHILDREN:
                rc = proc.poll()
                if rc is not None:
                    print(f'[SUPERVISOR] child pid={proc.pid} gestopt rc={rc}', flush=True)
                    exit_code = rc if rc != 0 else 1
                    STOP = True
                    break
            if not STOP:
                time.sleep(0.5)
    finally:
        _terminate_children()

    return exit_code


if __name__ == '__main__':
    sys.exit(main())
