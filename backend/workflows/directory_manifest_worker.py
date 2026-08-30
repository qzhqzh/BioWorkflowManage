from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("path")
    parser.add_argument("--max-entries", type=int, required=True)
    parser.add_argument("--max-depth", type=int, required=True)
    parser.add_argument("--deadline-seconds", type=float, required=True)
    parser.add_argument("--containment-root")
    return parser.parse_args()


def main() -> int:
    backend_root = Path(__file__).resolve().parents[1]
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))
    from workflows.directory_identity import (
        DirectoryIdentityChangedError,
        DirectoryIdentityLimitError,
        scan_directory_identity,
    )

    options = _arguments()
    try:
        path = Path(options.path)
        if options.containment_root:
            root = Path(options.containment_root).resolve(strict=True)
            path = path.resolve(strict=True)
            path.relative_to(root)
        manifest = scan_directory_identity(
            path,
            deadline_seconds=max(0.001, options.deadline_seconds),
            max_entries=max(1, options.max_entries),
            max_depth=max(1, options.max_depth),
        )
        payload = {"ok": True, "manifest": manifest}
    except DirectoryIdentityLimitError as error:
        payload = {"ok": False, "kind": "limit", "message": str(error)}
    except DirectoryIdentityChangedError as error:
        payload = {"ok": False, "kind": "changed", "message": str(error)}
    except (OSError, ValueError) as error:
        payload = {"ok": False, "kind": "invalid", "message": str(error)}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
