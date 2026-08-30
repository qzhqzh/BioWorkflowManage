from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any


MAX_REQUEST_BYTES = 64 * 1024
PR_SET_PDEATHSIG = 1


class _ObjectHeadDeadline(BaseException):
    pass


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--deadline-seconds", type=float, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    return parser.parse_args()


def _arm_deadline(seconds: float) -> None:
    if not hasattr(signal, "setitimer") or not hasattr(signal, "SIGALRM"):
        raise OSError(errno.ENOTSUP, "POSIX wall-clock timers are required")

    def deadline_handler(_signum, _frame) -> None:
        raise _ObjectHeadDeadline()

    signal.signal(signal.SIGALRM, deadline_handler)
    signal.setitimer(signal.ITIMER_REAL, max(0.1, seconds))


def _arm_parent_death_signal(expected_parent_pid: int) -> None:
    if not sys.platform.startswith("linux"):
        raise OSError(errno.ENOTSUP, "Linux parent-death signals are required")
    if expected_parent_pid <= 1:
        raise OSError(errno.EINVAL, "invalid parent pid")
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    if os.getppid() != expected_parent_pid:
        os.kill(os.getpid(), signal.SIGKILL)


def _request() -> tuple[dict[str, Any], str | None]:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("object HEAD request is too large")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or set(payload) != {"client_id", "reference"}:
        raise ValueError("invalid object HEAD request")
    client_id = payload["client_id"]
    if client_id is not None and not isinstance(client_id, str):
        raise ValueError("invalid object HEAD client")
    reference = payload["reference"]
    if not isinstance(reference, dict):
        raise ValueError("invalid object HEAD reference")
    return reference, client_id


def _stable_error(error) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": error.code,
            "message": str(error),
            "retryable": error.retryable,
            "details": error.details,
            "http_status": error.http_status,
        },
    }


def _unavailable() -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": "OBJECT_INPUT_UNAVAILABLE",
            "message": "对象存储预检进程执行失败。",
            "retryable": True,
            "details": {},
            "http_status": 503,
        },
    }


def _timeout() -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": "OBJECT_INPUT_HEAD_TIMEOUT",
            "message": "对象存储预检超过时间上限。",
            "retryable": True,
            "details": {},
            "http_status": 503,
        },
    }


def main() -> int:
    options = _arguments()
    _arm_deadline(options.deadline_seconds)
    try:
        _arm_parent_death_signal(options.parent_pid)
        backend_root = Path(__file__).resolve().parents[1]
        if str(backend_root) not in sys.path:
            sys.path.insert(0, str(backend_root))
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        import django

        django.setup()
        from workflows.object_inputs import (
            ObjectInputError,
            _inspect_object_reference_metadata,
            _reference,
        )

        object_error_type = ObjectInputError
        raw_reference, client_id = _request()
        reference = _reference(raw_reference, input_name="object")
        metadata = _inspect_object_reference_metadata(
            reference,
            client_id=client_id,
        )
        payload = {"ok": True, "metadata": metadata}
    except _ObjectHeadDeadline:
        payload = _timeout()
    except BaseException as error:
        object_error_type = locals().get("object_error_type")
        if object_error_type is not None and isinstance(error, object_error_type):
            payload = _stable_error(error)
        else:
            payload = _unavailable()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        encoded = json.dumps(
            _unavailable(),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
