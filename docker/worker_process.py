"""Supervise one role process; probe liveness without importing other role dependencies."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

HEARTBEAT = Path("/tmp/role-heartbeat.json")


def check():
    try:
        record = json.loads(HEARTBEAT.read_text())
        if not 0 <= time.time() - record["time"] < 30:
            return 1
        os.kill(int(record["pid"]), 0)
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        return 1


def run(command):
    if not command:
        return 2
    child = subprocess.Popen(command, start_new_session=True)

    def forward(signum, _frame):
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    try:
        while child.poll() is None:
            temporary = HEARTBEAT.with_suffix(".tmp")
            temporary.write_text(json.dumps({"pid": child.pid, "time": time.time()}))
            temporary.replace(HEARTBEAT)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        return child.returncode if child.returncode >= 0 else 128 - child.returncode
    finally:
        HEARTBEAT.unlink(missing_ok=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["check"]:
        sys.exit(check())
    if sys.argv[1:2] == ["run"]:
        sys.exit(run(sys.argv[2:]))
    sys.exit(2)
