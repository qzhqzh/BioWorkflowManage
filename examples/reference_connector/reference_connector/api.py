from __future__ import annotations

import json
import re
import socket
import uuid
from dataclasses import dataclass, field
from ipaddress import ip_address
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


DEFAULT_RESPONSE_LIMIT = 8 * 1024 * 1024
INTEGRATION_API_MIN_VERSION = (1, 5, 0)
INTEGRATION_API_VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def _is_loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


class _RejectRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        # A redirect could move the bearer token outside the configured API origin.
        return None


class ConnectorError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class ConnectorConflictError(ConnectorError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            "CONNECTOR_IDEMPOTENCY_CONFLICT",
            message,
            details=details,
        )


class ConnectorIntegrityError(ConnectorError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            "CONNECTOR_UPSTREAM_INTEGRITY_FAILED",
            message,
            details=details,
        )


class IntegrationCompatibilityError(ConnectorError):
    def __init__(self, message: str, *, details: dict[str, Any]) -> None:
        super().__init__(
            "CONNECTOR_INTEGRATION_API_INCOMPATIBLE",
            message,
            details=details,
        )


class IntegrationTransportError(ConnectorError):
    def __init__(self, message: str) -> None:
        super().__init__(
            "CONNECTOR_UPSTREAM_UNAVAILABLE",
            message,
            retryable=True,
        )


class SubmissionUncertainError(ConnectorError):
    def __init__(self, external_run_id: str) -> None:
        super().__init__(
            "CONNECTOR_SUBMISSION_UNCERTAIN",
            "提交结果未知；必须继续使用相同 external_run_id 和幂等键找回，不能改投其他引擎。",
            retryable=True,
            details={"external_run_id": external_run_id},
        )


class IntegrationAPIError(ConnectorError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code, message, retryable=retryable, details=details)
        self.status_code = status_code


@dataclass(frozen=True)
class TransportResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


class HTTPTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> TransportResponse: ...


@dataclass
class UrllibTransport:
    user_agent: str = "BioWorkflowManage-Reference-Connector/1.0"

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> TransportResponse:
        request = Request(
            url,
            data=body,
            method=method,
            headers={"User-Agent": self.user_agent, **headers},
        )
        try:
            handlers = [_RejectRedirectHandler()]
            if _is_loopback_host(urlsplit(url).hostname):
                handlers.insert(0, ProxyHandler({}))
            with build_opener(*handlers).open(
                request,
                timeout=timeout_seconds,
            ) as response:
                payload = response.read(max_response_bytes + 1)
                status_code = int(response.status)
                response_headers = dict(response.headers.items())
        except HTTPError as error:
            payload = error.read(max_response_bytes + 1)
            status_code = int(error.code)
            response_headers = dict(error.headers.items())
        except (URLError, TimeoutError, socket.timeout, OSError) as error:
            raise IntegrationTransportError(f"Integration API 请求失败：{error}") from error
        if len(payload) > max_response_bytes:
            raise IntegrationTransportError("Integration API 响应超过 Connector 安全上限。")
        return TransportResponse(status_code, response_headers, payload)


@dataclass
class IntegrationClient:
    base_url: str
    token: str
    timeout_seconds: float = 15.0
    recovery_attempts: int = 2
    max_download_bytes: int = 512 * 1024 * 1024
    transport: HTTPTransport = field(default_factory=UrllibTransport)

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url.strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("analysis_api.base_url 必须是不含凭据、query 和 fragment 的 HTTP(S) URL。")
        if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
            raise ValueError("非 loopback 的 analysis_api.base_url 必须使用 HTTPS。")
        if not self.token.strip() or any(
            character.isspace() for character in self.token.strip()
        ):
            raise ValueError("BIOWORKFLOW_TOKEN 不能为空。")
        normalized_path = parsed.path.rstrip("/")
        if not normalized_path.endswith("/api/v1/integration"):
            raise ValueError("analysis_api.base_url 必须以 /api/v1/integration 结尾。")
        self.base_url = self.base_url.strip().rstrip("/")
        self.token = self.token.strip()
        self.timeout_seconds = max(0.1, float(self.timeout_seconds))
        self.recovery_attempts = max(1, int(self.recovery_attempts))
        self.max_download_bytes = max(1, int(self.max_download_bytes))

    def _resolve_url(self, path_or_url: str) -> str:
        value = str(path_or_url or "").strip()
        if not value:
            raise ValueError("Integration API path 不能为空。")
        base = urlsplit(self.base_url)
        if value.startswith("/"):
            resolved = f"{base.scheme}://{base.netloc}{value}"
        else:
            resolved = urljoin(self.base_url + "/", value)
        target = urlsplit(resolved)
        if (target.scheme, target.netloc) != (base.scheme, base.netloc):
            raise ConnectorIntegrityError("拒绝跟随跨 origin 的 Integration API 链接。")
        decoded_path = unquote(target.path)
        base_path = unquote(base.path.rstrip("/"))
        if (
            "\\" in decoded_path
            or any(part in {".", ".."} for part in decoded_path.split("/"))
            or any(ord(character) < 32 for character in decoded_path)
        ):
            raise ConnectorIntegrityError("Integration API 链接包含不安全路径。")
        if not (
            decoded_path == base_path
            or decoded_path.startswith(base_path + "/")
        ):
            raise ConnectorIntegrityError("拒绝访问 Integration API 前缀之外的链接。")
        return resolved

    def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        expected_statuses: set[int] | None = None,
        max_response_bytes: int = DEFAULT_RESPONSE_LIMIT,
    ) -> TransportResponse:
        body = None
        request_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            "X-Request-ID": str(uuid.uuid4()),
            **(headers or {}),
        }
        if payload is not None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        response = self.transport.request(
            method,
            self._resolve_url(path_or_url),
            headers=request_headers,
            body=body,
            timeout_seconds=self.timeout_seconds,
            max_response_bytes=max_response_bytes,
        )
        if 300 <= response.status_code < 400:
            raise IntegrationTransportError(
                "Integration API 返回重定向；Connector 已拒绝跟随。"
            )
        accepted = expected_statuses or {200}
        if response.status_code not in accepted:
            self._raise_api_error(response)
        return response

    @staticmethod
    def _decode_json(response: TransportResponse) -> dict[str, Any]:
        try:
            value = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise IntegrationTransportError("Integration API 返回了无效 JSON。") from error
        if not isinstance(value, dict):
            raise IntegrationTransportError("Integration API JSON 顶层必须是 object。")
        return value

    def _request_json(
        self,
        method: str,
        path_or_url: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        expected_statuses: set[int] | None = None,
    ) -> dict[str, Any]:
        return self._decode_json(
            self._request(
                method,
                path_or_url,
                payload=payload,
                headers=headers,
                expected_statuses=expected_statuses,
            )
        )

    @staticmethod
    def _raise_api_error(response: TransportResponse) -> None:
        code = "INTEGRATION_API_ERROR"
        message = f"Integration API 返回 HTTP {response.status_code}。"
        retryable = response.status_code >= 500
        details: dict[str, Any] = {}
        try:
            envelope = json.loads(response.body.decode("utf-8"))
            error = envelope.get("error") if isinstance(envelope, dict) else None
            if isinstance(error, dict):
                code = str(error.get("code") or code)
                message = str(error.get("message") or message)
                retryable = bool(error.get("retryable", retryable))
                if isinstance(error.get("details"), dict):
                    details = error["details"]
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        raise IntegrationAPIError(
            response.status_code,
            code,
            message,
            retryable=retryable,
            details=details,
        )

    def list_products(self) -> list[dict[str, Any]]:
        payload = self._request_json("GET", "analysis-products")
        results = payload.get("results")
        if not isinstance(results, list):
            raise IntegrationTransportError("分析产品目录缺少 results 数组。")
        return [item for item in results if isinstance(item, dict)]

    def require_compatible_api(self) -> None:
        document = self._request_json("GET", "openapi")
        info = document.get("info")
        version = info.get("version") if isinstance(info, dict) else None
        match = INTEGRATION_API_VERSION_PATTERN.fullmatch(str(version or ""))
        parsed_version = tuple(int(part) for part in match.groups()) if match else None
        components = document.get("components")
        schemas = components.get("schemas") if isinstance(components, dict) else None
        analysis_run = schemas.get("AnalysisRun") if isinstance(schemas, dict) else None
        required = analysis_run.get("required") if isinstance(analysis_run, dict) else None
        properties = analysis_run.get("properties") if isinstance(analysis_run, dict) else None
        request_digest = (
            properties.get("request_digest") if isinstance(properties, dict) else None
        )
        compatible_version = (
            parsed_version is not None
            and parsed_version[0] == INTEGRATION_API_MIN_VERSION[0]
            and parsed_version >= INTEGRATION_API_MIN_VERSION
        )
        compatible_digest = (
            isinstance(required, list)
            and "request_digest" in required
            and isinstance(request_digest, dict)
            and request_digest.get("type") == "string"
            and request_digest.get("pattern") == r"^sha256:[0-9a-f]{64}$"
        )
        if not compatible_version or not compatible_digest:
            raise IntegrationCompatibilityError(
                "Integration API 不兼容；Reference Connector 要求 1.5.0+ 的 1.x 契约和 request_digest 能力。",
                details={
                    "detected_version": version,
                    "minimum_version": "1.5.0",
                    "required_capability": "AnalysisRun.request_digest",
                },
            )

    def get_product(self, analysis_code: str, contract_version: str) -> dict[str, Any]:
        return self._request_json(
            "GET",
            "analysis-products/"
            f"{quote(analysis_code, safe='')}/versions/{quote(contract_version, safe='')}",
        )

    def preflight(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("POST", "analysis-runs/preflight", payload=payload)

    def submit(self, payload: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        return self._request_json(
            "POST",
            "analysis-runs",
            payload=payload,
            headers={"Idempotency-Key": idempotency_key},
            expected_statuses={200, 201},
        )

    def find_run(self, external_run_id: str) -> dict[str, Any] | None:
        query = urlencode({"external_run_id": external_run_id})
        try:
            return self._request_json(
                "GET",
                f"analysis-runs/by-external-ref?{query}",
            )
        except IntegrationAPIError as error:
            if error.status_code == 404:
                return None
            raise

    def submit_with_recovery(
        self,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        external_run_id: str,
    ) -> dict[str, Any]:
        last_error: ConnectorError | None = None
        for _ in range(self.recovery_attempts + 1):
            try:
                return self.submit(payload, idempotency_key=idempotency_key)
            except IntegrationTransportError as error:
                last_error = error
            except IntegrationAPIError as error:
                if error.status_code < 500 and not error.retryable:
                    raise
                last_error = error
            try:
                existing = self.find_run(external_run_id)
            except IntegrationTransportError as error:
                last_error = error
                continue
            if existing is not None:
                return existing
        if last_error is not None:
            raise SubmissionUncertainError(external_run_id) from last_error
        raise SubmissionUncertainError(external_run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._request_json("GET", f"analysis-runs/{quote(run_id, safe='')}")

    def list_outputs(self, run_id: str) -> dict[str, Any]:
        return self._request_json(
            "GET",
            f"analysis-runs/{quote(run_id, safe='')}/outputs",
        )

    def download_output(self, item: dict[str, Any]) -> bytes:
        expected_size = item.get("size")
        if (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 0
        ):
            raise ConnectorIntegrityError("输出清单中的 size 无效。")
        if expected_size > self.max_download_bytes:
            raise ConnectorIntegrityError("输出文件超过 Connector 下载上限。")
        download_url = str(item.get("download_url") or "").strip()
        if not download_url:
            raise ConnectorIntegrityError("输出清单缺少受保护 download_url。")
        response = self._request(
            "GET",
            download_url,
            expected_statuses={200},
            max_response_bytes=expected_size,
        )
        return response.body

    def create_artifact_export(
        self,
        run_id: str,
        *,
        profile: str,
        idempotency_key: str,
        requires_ack: bool = True,
        retain_until: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "target": {"profile": profile},
            "requires_ack": requires_ack,
        }
        if retain_until is not None:
            payload["retain_until"] = retain_until
        return self._request_json(
            "POST",
            f"analysis-runs/{quote(run_id, safe='')}/artifact-exports",
            payload=payload,
            headers={"Idempotency-Key": idempotency_key},
            expected_statuses={200, 201},
        )

    def get_artifact_export(self, export_id: str) -> dict[str, Any]:
        return self._request_json(
            "GET",
            f"artifact-exports/{quote(export_id, safe='')}",
        )

    def acknowledge_artifact_export(
        self,
        export_id: str,
        *,
        manifest_digest: str,
        external_receipt: str,
    ) -> dict[str, Any]:
        return self._request_json(
            "POST",
            f"artifact-exports/{quote(export_id, safe='')}/acknowledge",
            payload={
                "manifest_digest": manifest_digest,
                "external_receipt": external_receipt,
            },
        )
