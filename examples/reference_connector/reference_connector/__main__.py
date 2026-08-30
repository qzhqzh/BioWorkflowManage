from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path
from typing import Any

from .api import ConnectorError
from .config import load_runtime
from .server import serve


def _json_file(path: str) -> dict[str, Any]:
    try:
        source = Path(path)
        current = source.lstat()
        if (
            not stat.S_ISREG(current.st_mode)
            or source.is_symlink()
            or current.st_size > 1024 * 1024
        ):
            raise ConnectorError(
                "CONNECTOR_REQUEST_INVALID",
                "JSON 输入必须是小于 1 MiB 的普通文件。",
            )
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConnectorError(
            "CONNECTOR_REQUEST_INVALID", f"无法读取 JSON：{error}"
        ) from error
    if not isinstance(value, dict):
        raise ConnectorError(
            "CONNECTOR_REQUEST_INVALID", "JSON 顶层必须是 object。"
        )
    return value


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="BioWorkflowManage reference Connector")
    parser.add_argument(
        "--config",
        required=True,
        help="Connector JSON config; secrets are read from environment",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    commands.add_parser("products")
    submit = commands.add_parser("submit")
    submit.add_argument("--order", required=True)
    for name in ("status", "reconcile", "collect", "complete-export"):
        command = commands.add_parser(name)
        command.add_argument("--external-run-id", required=True)
    export = commands.add_parser("export")
    export.add_argument("--external-run-id", required=True)
    export.add_argument("--profile", required=True)
    export.add_argument("--no-ack", action="store_true")
    export.add_argument("--retain-until")
    args = parser.parse_args()
    try:
        runtime = load_runtime(args.config)
        connector = runtime.connector
        if args.command == "serve":
            serve(runtime)
            return 0
        if args.command == "products":
            _print({"results": connector.client.list_products()})
        elif args.command == "submit":
            _print(connector.submit_order(_json_file(args.order)))
        elif args.command == "status":
            _print(connector.order_status(args.external_run_id))
        elif args.command == "reconcile":
            _print(connector.reconcile(args.external_run_id))
        elif args.command == "collect":
            _print(connector.collect_results(args.external_run_id))
        elif args.command == "export":
            _print(
                connector.request_export(
                    args.external_run_id,
                    profile=args.profile,
                    requires_ack=not args.no_ack,
                    retain_until=args.retain_until,
                )
            )
        elif args.command == "complete-export":
            _print(connector.complete_export(args.external_run_id))
        return 0
    except ConnectorError as error:
        _print(
            {
                "error": {
                    "code": error.code,
                    "message": str(error),
                    "retryable": error.retryable,
                    "details": error.details,
                }
            }
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
