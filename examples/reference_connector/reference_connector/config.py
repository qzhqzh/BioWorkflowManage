from __future__ import annotations

import base64
import binascii
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .api import ConnectorError, IntegrationClient
from .connector import ReferenceConnector
from .mapping import MappingConfig
from .store import ConnectorStore


MAX_CONFIG_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    max_request_bytes: int


@dataclass(frozen=True)
class Runtime:
    connector: ReferenceConnector
    server: ServerConfig
    inbound_token: str


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        current = path.lstat()
        if not stat.S_ISREG(current.st_mode) or path.is_symlink():
            raise ConnectorError(
                "CONNECTOR_CONFIG_INVALID",
                "Connector 配置必须是普通文件，不能是符号链接。",
            )
        if current.st_size > MAX_CONFIG_BYTES:
            raise ConnectorError(
                "CONNECTOR_CONFIG_INVALID",
                "Connector 配置超过安全上限。",
            )
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConnectorError(
            "CONNECTOR_CONFIG_INVALID", f"无法读取 Connector 配置：{error}"
        ) from error
    except json.JSONDecodeError as error:
        raise ConnectorError(
            "CONNECTOR_CONFIG_INVALID", f"Connector 配置不是有效 JSON：{error}"
        ) from error
    if not isinstance(value, dict):
        raise ConnectorError(
            "CONNECTOR_CONFIG_INVALID", "Connector 配置顶层必须是 object。"
        )
    return value


def _relative_path(base: Path, value: Any, *, label: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ConnectorError("CONNECTOR_CONFIG_INVALID", f"{label} 不能为空。")
    path = Path(raw).expanduser()
    return path.absolute() if path.is_absolute() else (base / path).absolute()


def _valid_webhook_secret(value: str) -> bool:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError):
        return False
    return len(decoded) == 32


def load_runtime(
    config_path: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> Runtime:
    path = Path(config_path).expanduser().absolute()
    document = _read_json_file(path)
    allowed = {"schema_version", "listen", "analysis_api", "state", "mapping"}
    unknown = sorted(set(document) - allowed)
    if unknown or document.get("schema_version") != "1.0.0":
        raise ConnectorError(
            "CONNECTOR_CONFIG_INVALID",
            "Connector 配置版本或字段无效。",
            details={"unknown_fields": unknown},
        )
    analysis_api = document.get("analysis_api")
    state = document.get("state")
    listen = document.get("listen", {})
    if not isinstance(analysis_api, dict) or set(analysis_api) - {
        "base_url",
        "timeout_seconds",
        "recovery_attempts",
        "max_download_bytes",
    }:
        raise ConnectorError("CONNECTOR_CONFIG_INVALID", "analysis_api 配置无效。")
    if not isinstance(state, dict) or set(state) != {"database", "result_directory"}:
        raise ConnectorError("CONNECTOR_CONFIG_INVALID", "state 配置无效。")
    if not isinstance(listen, dict) or set(listen) - {
        "host",
        "port",
        "max_request_bytes",
    }:
        raise ConnectorError("CONNECTOR_CONFIG_INVALID", "listen 配置无效。")
    environment = os.environ if environ is None else environ
    token = str(environment.get("BIOWORKFLOW_TOKEN") or "").strip()
    webhook_secret = str(
        environment.get("BIOWORKFLOW_WEBHOOK_SECRET") or ""
    ).strip()
    inbound_token = str(
        environment.get("REFERENCE_CONNECTOR_INBOUND_TOKEN") or ""
    ).strip()
    if (
        not token
        or not _valid_webhook_secret(webhook_secret)
        or len(inbound_token) < 32
        or any(character.isspace() for character in inbound_token)
    ):
        raise ConnectorError(
            "CONNECTOR_SECRET_MISSING",
            "必须通过环境变量注入上游 Token、32-byte Webhook signing secret 和至少 32 字符的 Connector 入站 Token。",
        )
    base = path.parent
    mapping = MappingConfig.from_dict(document.get("mapping"))
    try:
        timeout_seconds = float(analysis_api.get("timeout_seconds", 15))
        recovery_attempts = int(analysis_api.get("recovery_attempts", 2))
        max_download_bytes = int(
            analysis_api.get("max_download_bytes", 512 * 1024 * 1024)
        )
        host = str(listen.get("host") or "127.0.0.1").strip()
        port = int(listen.get("port", 8090))
        max_request_bytes = int(listen.get("max_request_bytes", MAX_CONFIG_BYTES))
        client = IntegrationClient(
            base_url=str(analysis_api.get("base_url") or ""),
            token=token,
            timeout_seconds=timeout_seconds,
            recovery_attempts=recovery_attempts,
            max_download_bytes=max_download_bytes,
        )
    except (TypeError, ValueError) as error:
        raise ConnectorError(
            "CONNECTOR_CONFIG_INVALID",
            f"Connector URL 或数值配置无效：{error}",
        ) from error
    if (
        not host
        or not 1 <= port <= 65535
        or not 1 <= max_request_bytes <= 16 * 1024 * 1024
    ):
        raise ConnectorError("CONNECTOR_CONFIG_INVALID", "listen 参数超出安全范围。")
    connector = ReferenceConnector(
        mapping=mapping,
        client=client,
        store=ConnectorStore(
            _relative_path(base, state.get("database"), label="state.database")
        ),
        webhook_secret=webhook_secret,
        result_directory=_relative_path(
            base,
            state.get("result_directory"),
            label="state.result_directory",
        ),
    )
    return Runtime(
        connector=connector,
        server=ServerConfig(host, port, max_request_bytes),
        inbound_token=inbound_token,
    )
