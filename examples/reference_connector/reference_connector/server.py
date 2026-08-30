from __future__ import annotations

import json
import hmac
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit

from .api import (
    ConnectorConflictError,
    ConnectorError,
    IntegrationAPIError,
    IntegrationTransportError,
    SubmissionUncertainError,
)
from .config import Runtime
from .mapping import MappingError


LOGGER = logging.getLogger(__name__)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class ReferenceConnectorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, runtime: Runtime) -> None:
        self.connector = runtime.connector
        self.max_request_bytes = runtime.server.max_request_bytes
        self.inbound_token = runtime.inbound_token
        super().__init__(
            (runtime.server.host, runtime.server.port),
            ReferenceConnectorHandler,
        )


class ReferenceConnectorHandler(BaseHTTPRequestHandler):
    server: ReferenceConnectorHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        # Do not log request bodies, tokens, signatures, or customer identifiers.
        return

    def _send(self, status_code: int, payload: Any) -> None:
        self.close_connection = True
        body = _json_bytes(payload)
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def send_error(  # noqa: D102
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        del message, explain
        self._send(
            int(code),
            {
                "error": {
                    "code": "CONNECTOR_HTTP_ERROR",
                    "message": "Connector 拒绝了 HTTP 请求。",
                    "retryable": False,
                    "details": {"status": int(code)},
                }
            },
        )

    def _send_file(self, receipt: dict[str, Any], handle) -> None:  # noqa: ANN001
        self.close_connection = True
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(receipt["size"]))
        self.send_header("Content-Disposition", "attachment")
        self.send_header("X-Checksum-SHA256", str(receipt["sha256"]))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        while chunk := handle.read(1024 * 1024):
            self.wfile.write(chunk)

    def _read_body(self, *, require_json: bool) -> bytes:
        if self.headers.get("Transfer-Encoding") is not None:
            raise ConnectorError(
                "CONNECTOR_REQUEST_INVALID",
                "Connector 不接受 Transfer-Encoding 请求体。",
            )
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError as error:
            raise ConnectorError(
                "CONNECTOR_REQUEST_INVALID", "Content-Length 无效。"
            ) from error
        if length < 0 or length > self.server.max_request_bytes:
            raise ConnectorError(
                "CONNECTOR_REQUEST_TOO_LARGE", "请求体超过 Connector 安全上限。"
            )
        if require_json and "application/json" not in str(
            self.headers.get("Content-Type") or ""
        ).casefold():
            raise ConnectorError(
                "CONNECTOR_REQUEST_INVALID", "请求 Content-Type 必须是 application/json。"
            )
        body = self.rfile.read(length)
        if len(body) != length:
            raise ConnectorError(
                "CONNECTOR_REQUEST_INVALID", "请求体未完整接收。"
            )
        return body

    def _read_json(self) -> dict[str, Any]:
        body = self._read_body(require_json=True)
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ConnectorError(
                "CONNECTOR_REQUEST_INVALID", "请求体不是有效 UTF-8 JSON。"
            ) from error
        if not isinstance(value, dict):
            raise ConnectorError(
                "CONNECTOR_REQUEST_INVALID", "请求 JSON 顶层必须是 object。"
            )
        return value

    def _require_inbound_auth(self) -> None:
        provided = str(self.headers.get("Authorization") or "")
        expected = f"Bearer {self.server.inbound_token}"
        if not hmac.compare_digest(provided, expected):
            raise ConnectorError(
                "CONNECTOR_INBOUND_AUTH_INVALID",
                "Connector 调用凭据无效。",
            )

    def _path_segments(self) -> list[str]:
        return [
            unquote(part)
            for part in urlsplit(self.path).path.split("/")
            if part
        ]

    def _handle_error(self, error: Exception) -> None:
        if isinstance(error, IntegrationAPIError):
            status_code = error.status_code
        elif isinstance(error, ConnectorConflictError):
            status_code = HTTPStatus.CONFLICT
        elif isinstance(error, (IntegrationTransportError, SubmissionUncertainError)):
            status_code = HTTPStatus.SERVICE_UNAVAILABLE
        elif isinstance(error, MappingError):
            status_code = HTTPStatus.BAD_REQUEST
        elif isinstance(error, ConnectorError):
            if error.code == "CONNECTOR_INBOUND_AUTH_INVALID":
                status_code = HTTPStatus.UNAUTHORIZED
            elif error.code in {
                "CONNECTOR_ROUTE_NOT_FOUND",
                "CONNECTOR_ORDER_NOT_FOUND",
                "CONNECTOR_EXPORT_NOT_FOUND",
                "CONNECTOR_OUTPUT_NOT_FOUND",
            }:
                status_code = HTTPStatus.NOT_FOUND
            else:
                status_code = HTTPStatus.BAD_REQUEST
        else:
            LOGGER.exception("Reference Connector request failed unexpectedly")
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": {
                        "code": "CONNECTOR_INTERNAL_ERROR",
                        "message": "Connector 发生未预期错误。",
                        "retryable": False,
                        "details": {},
                    }
                },
            )
            return
        assert isinstance(error, ConnectorError)
        self._send(
            int(status_code),
            {
                "error": {
                    "code": error.code,
                    "message": str(error),
                    "retryable": error.retryable,
                    "details": error.details,
                }
            },
        )

    def do_GET(self) -> None:  # noqa: N802
        try:
            segments = self._path_segments()
            if segments == ["health"]:
                self._send(HTTPStatus.OK, {"status": "ok"})
                return
            self._require_inbound_auth()
            if segments == ["v1", "products"]:
                self._send(
                    HTTPStatus.OK,
                    {"results": self.server.connector.client.list_products()},
                )
                return
            if len(segments) == 3 and segments[:2] == ["v1", "orders"]:
                self._send(
                    HTTPStatus.OK,
                    self.server.connector.order_status(segments[2]),
                )
                return
            if (
                len(segments) == 5
                and segments[:2] == ["v1", "orders"]
                and segments[3] == "outputs"
            ):
                receipt, handle = self.server.connector.open_output(
                    segments[2],
                    segments[4],
                )
                try:
                    self._send_file(receipt, handle)
                finally:
                    handle.close()
                return
            raise ConnectorError(
                "CONNECTOR_ROUTE_NOT_FOUND",
                "Connector 路由不存在。",
            )
        except Exception as error:  # noqa: BLE001
            self._handle_error(error)

    def do_POST(self) -> None:  # noqa: N802
        try:
            segments = self._path_segments()
            if segments == ["v1", "orders"]:
                self._require_inbound_auth()
                run = self.server.connector.submit_order(self._read_json())
                self._send(HTTPStatus.ACCEPTED, run)
                return
            if segments == ["v1", "webhooks", "bioworkflow"]:
                body = self._read_body(require_json=True)
                result = self.server.connector.handle_webhook(
                    {key: value for key, value in self.headers.items()},
                    body,
                )
                self._send(
                    HTTPStatus.OK,
                    {
                        "accepted": True,
                        "replayed": result.replayed,
                        "applied": result.applied,
                        "stale": result.stale,
                    },
                )
                return
            self._require_inbound_auth()
            if len(segments) == 4 and segments[:2] == ["v1", "orders"]:
                external_run_id = segments[2]
                action = segments[3]
                if action == "reconcile":
                    self._read_body(require_json=True)
                    self._send(
                        HTTPStatus.OK,
                        self.server.connector.reconcile(external_run_id),
                    )
                    return
                if action == "results":
                    self._read_body(require_json=True)
                    self._send(
                        HTTPStatus.OK,
                        self.server.connector.collect_results(external_run_id),
                    )
                    return
                if action == "exports":
                    payload = self._read_json()
                    allowed = {"profile", "requires_ack", "retain_until"}
                    if set(payload) - allowed or not str(payload.get("profile") or ""):
                        raise ConnectorError(
                            "CONNECTOR_REQUEST_INVALID", "Artifact Export 请求无效。"
                        )
                    requires_ack = payload.get("requires_ack", True)
                    if not isinstance(requires_ack, bool):
                        raise ConnectorError(
                            "CONNECTOR_REQUEST_INVALID",
                            "requires_ack 必须是 boolean。",
                        )
                    self._send(
                        HTTPStatus.ACCEPTED,
                        self.server.connector.request_export(
                            external_run_id,
                            profile=str(payload["profile"]),
                            requires_ack=requires_ack,
                            retain_until=payload.get("retain_until"),
                        ),
                    )
                    return
                if action == "complete-export":
                    self._read_body(require_json=True)
                    self._send(
                        HTTPStatus.OK,
                        self.server.connector.complete_export(external_run_id),
                    )
                    return
            raise ConnectorError(
                "CONNECTOR_ROUTE_NOT_FOUND",
                "Connector 路由不存在。",
            )
        except Exception as error:  # noqa: BLE001
            self._handle_error(error)


def serve(runtime: Runtime) -> None:
    server = ReferenceConnectorHTTPServer(runtime)
    try:
        server.serve_forever()
    finally:
        server.server_close()
