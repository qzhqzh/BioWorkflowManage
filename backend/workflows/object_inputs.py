from __future__ import annotations

import hashlib
import ipaddress
import json
import multiprocessing
import os
import re
import shutil
import signal
import socket
import stat as stat_module
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

import boto3
from botocore.awsrequest import (
    AWSHTTPConnection,
    AWSHTTPConnectionPool,
    AWSHTTPSConnection,
    AWSHTTPSConnectionPool,
)
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, HTTPClientError
from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from urllib3 import PoolManager
from urllib3.exceptions import NewConnectionError
from urllib3.util import connection as urllib3_connection

from .integration_outputs import GzipProbeLineLimitError, _read_gzip_text_lines
from .models import AnalysisRun, InputStagingCoordinator, InputStagingLease


PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CLIENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
BUCKET_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$")
SHA256_PATTERN = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")
MAX_PROFILE_BYTES = 64 * 1024
REFERENCE_FIELDS = {
    "type",
    "profile",
    "bucket",
    "key",
    "version_id",
    "etag",
    "size",
    "sha256",
}
STAGING_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_HEAD_CALL_SLOTS = threading.BoundedSemaphore(
    max(1, int(settings.ANALYSIS_OBJECT_HEAD_MAX_CONCURRENT))
)
_HEAD_PROCESS_CONTEXT = multiprocessing.get_context("fork")


class ObjectInputError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        http_status: int = 400,
    ):
        super().__init__(message)
        self.code = code
        self.category = "input"
        self.retryable = retryable
        self.details = details or {}
        self.http_status = http_status


@dataclass(frozen=True, repr=False)
class ObjectStorageProfile:
    name: str
    endpoint_url: str
    region: str
    allowed_buckets: tuple[str, ...]
    client_grants: dict[str, dict[str, tuple[str, ...]]]
    access_key_id: str
    secret_access_key: str
    session_token: str
    allow_http: bool
    allow_private_network: bool
    allowed_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    expected_bucket_owner: str


class _ObjectDeadline(BaseException):
    pass


def _profile_error(code: str, message: str) -> ObjectInputError:
    return ObjectInputError(code, message, http_status=503)


def _load_profile(name: str) -> ObjectStorageProfile:
    if not PROFILE_PATTERN.fullmatch(name):
        raise ObjectInputError(
            "OBJECT_INPUT_PROFILE_INVALID",
            "对象存储 profile 名称无效。",
        )
    root = Path(settings.ANALYSIS_OBJECT_STORAGE_PROFILE_DIR)
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise _profile_error(
            "OBJECT_INPUT_PROFILE_NOT_FOUND",
            "对象存储 profile 目录不存在。",
        ) from error
    path = resolved_root / f"{name}.json"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise _profile_error(
            "OBJECT_INPUT_PROFILE_NOT_FOUND",
            "对象存储 profile 不存在或不可读取。",
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat_module.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_PROFILE_BYTES:
            raise _profile_error(
                "OBJECT_INPUT_PROFILE_INVALID",
                "对象存储 profile 必须是受限大小的普通文件。",
            )
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            raw = handle.read(MAX_PROFILE_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise _profile_error(
            "OBJECT_INPUT_PROFILE_INVALID",
            "对象存储 profile 不是有效 UTF-8 JSON。",
        ) from error
    if not isinstance(value, dict):
        raise _profile_error(
            "OBJECT_INPUT_PROFILE_INVALID",
            "对象存储 profile 必须是 JSON object。",
        )
    endpoint_url = str(value.get("endpoint_url") or "").strip()
    region = str(value.get("region") or "us-east-1").strip()
    access_key_id = str(value.get("access_key_id") or "")
    secret_access_key = str(value.get("secret_access_key") or "")
    session_token = str(value.get("session_token") or "")
    raw_buckets = value.get("allowed_buckets")
    if (
        not endpoint_url
        or not region
        or not access_key_id
        or not secret_access_key
        or not isinstance(raw_buckets, list)
        or not raw_buckets
    ):
        raise _profile_error(
            "OBJECT_INPUT_PROFILE_INVALID",
            "对象存储 profile 缺少 endpoint、凭据或 bucket 白名单。",
        )
    allowed_buckets = tuple(str(item).strip() for item in raw_buckets)
    if any(not BUCKET_PATTERN.fullmatch(item) for item in allowed_buckets):
        raise _profile_error(
            "OBJECT_INPUT_PROFILE_INVALID",
            "对象存储 profile 的 bucket 白名单无效。",
        )
    raw_grants = value.get("client_grants")
    if not isinstance(raw_grants, dict) or not raw_grants:
        raise _profile_error(
            "OBJECT_INPUT_PROFILE_INVALID",
            "对象存储 profile 必须配置 Service Account 授权。",
        )
    client_grants: dict[str, dict[str, tuple[str, ...]]] = {}
    for client_id, raw_bucket_grants in raw_grants.items():
        if (
            not isinstance(client_id, str)
            or not CLIENT_PATTERN.fullmatch(client_id)
            or not isinstance(raw_bucket_grants, dict)
            or not raw_bucket_grants
        ):
            raise _profile_error(
                "OBJECT_INPUT_PROFILE_INVALID",
                "对象存储 profile 的 Service Account 授权无效。",
            )
        bucket_grants: dict[str, tuple[str, ...]] = {}
        for bucket, raw_prefixes in raw_bucket_grants.items():
            if (
                bucket not in allowed_buckets
                or not isinstance(raw_prefixes, list)
                or not raw_prefixes
            ):
                raise _profile_error(
                    "OBJECT_INPUT_PROFILE_INVALID",
                    "对象存储 profile 的 bucket/prefix 授权无效。",
                )
            prefixes: list[str] = []
            for raw_prefix in raw_prefixes:
                if not isinstance(raw_prefix, str) or not raw_prefix:
                    raise _profile_error(
                        "OBJECT_INPUT_PROFILE_INVALID",
                        "对象存储 profile 的 key 前缀无效。",
                    )
                try:
                    encoded_prefix = raw_prefix.encode("utf-8")
                except UnicodeEncodeError as error:
                    raise _profile_error(
                        "OBJECT_INPUT_PROFILE_INVALID",
                        "对象存储 profile 的 key 前缀无效。",
                    ) from error
                if len(encoded_prefix) > 1024 or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in raw_prefix
                ):
                    raise _profile_error(
                        "OBJECT_INPUT_PROFILE_INVALID",
                        "对象存储 profile 的 key 前缀无效。",
                    )
                prefixes.append(raw_prefix)
            bucket_grants[bucket] = tuple(prefixes)
        client_grants[client_id] = bucket_grants
    raw_networks = value.get("allowed_cidrs") or []
    if not isinstance(raw_networks, list):
        raise _profile_error(
            "OBJECT_INPUT_PROFILE_INVALID",
            "对象存储 profile 的 allowed_cidrs 必须是数组。",
        )
    try:
        allowed_networks = tuple(
            ipaddress.ip_network(str(item), strict=False) for item in raw_networks
        )
    except ValueError as error:
        raise _profile_error(
            "OBJECT_INPUT_PROFILE_INVALID",
            "对象存储 profile 的 allowed_cidrs 无效。",
        ) from error
    return ObjectStorageProfile(
        name=name,
        endpoint_url=endpoint_url,
        region=region,
        allowed_buckets=allowed_buckets,
        client_grants=client_grants,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        session_token=session_token,
        allow_http=value.get("allow_http") is True,
        allow_private_network=value.get("allow_private_network") is True,
        allowed_networks=allowed_networks,
        expected_bucket_owner=str(value.get("expected_bucket_owner") or "").strip(),
    )


def _validate_endpoint(profile: ObjectStorageProfile) -> tuple[str, ...]:
    try:
        parsed = urlsplit(profile.endpoint_url)
        port = parsed.port
    except ValueError as error:
        raise _profile_error(
            "OBJECT_INPUT_ENDPOINT_INVALID",
            "对象存储 endpoint URL 无效。",
        ) from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(ord(character) < 32 or ord(character) == 127 for character in parsed.path)
        or ".." in Path(parsed.path).parts
    ):
        raise _profile_error(
            "OBJECT_INPUT_ENDPOINT_INVALID",
            "对象存储 endpoint URL 不符合安全约束。",
        )
    if parsed.scheme != "https" and not profile.allow_http:
        raise _profile_error(
            "OBJECT_INPUT_ENDPOINT_FORBIDDEN",
            "对象存储 endpoint 默认必须使用 HTTPS。",
        )
    host = parsed.hostname
    try:
        host.encode("ascii")
    except UnicodeEncodeError as error:
        raise _profile_error(
            "OBJECT_INPUT_ENDPOINT_INVALID",
            "对象存储 endpoint hostname 必须使用 ASCII。",
        ) from error
    try:
        addresses = socket.getaddrinfo(
            host,
            port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError as error:
        raise ObjectInputError(
            "OBJECT_INPUT_ENDPOINT_UNAVAILABLE",
            "对象存储 endpoint 无法解析。",
            retryable=True,
            http_status=503,
        ) from error
    resolved: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for item in addresses:
        raw_address = str(item[4][0]).split("%", 1)[0]
        try:
            resolved.add(ipaddress.ip_address(raw_address))
        except ValueError as error:
            raise _profile_error(
                "OBJECT_INPUT_ENDPOINT_INVALID",
                "对象存储 endpoint 解析到了无效地址。",
            ) from error
    if not resolved:
        raise ObjectInputError(
            "OBJECT_INPUT_ENDPOINT_UNAVAILABLE",
            "对象存储 endpoint 没有可用地址。",
            retryable=True,
            http_status=503,
        )
    for address in resolved:
        if (
            address.is_multicast
            or address.is_unspecified
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or getattr(address, "is_site_local", False)
        ):
            raise _profile_error(
                "OBJECT_INPUT_ENDPOINT_FORBIDDEN",
                "对象存储 endpoint 解析到了禁止的网络地址。",
            )
        if not address.is_global and not profile.allow_private_network:
            raise _profile_error(
                "OBJECT_INPUT_ENDPOINT_FORBIDDEN",
                "对象存储 endpoint 解析到了未授权的私有网络地址。",
            )
        if profile.allowed_networks and not any(
            address.version == network.version and address in network
            for network in profile.allowed_networks
        ):
            raise _profile_error(
                "OBJECT_INPUT_ENDPOINT_FORBIDDEN",
                "对象存储 endpoint 地址不在 profile 的网络白名单中。",
            )
    return tuple(
        str(address)
        for address in sorted(resolved, key=lambda item: (item.version, int(item)))
    )


def _pinned_connection_class(base_class, addresses: tuple[str, ...]):
    class PinnedConnection(base_class):
        def _new_conn(self):
            last_error: OSError | None = None
            for address in addresses:
                try:
                    return urllib3_connection.create_connection(
                        (address, self.port),
                        self.timeout,
                        source_address=self.source_address,
                        socket_options=self.socket_options,
                    )
                except OSError as error:
                    last_error = error
            raise NewConnectionError(
                self,
                "Failed to establish a connection to an approved endpoint address",
            ) from last_error

    return PinnedConnection


def _origin(value: str) -> tuple[str, str, int]:
    parsed = urlsplit(value)
    scheme = parsed.scheme.casefold()
    host = str(parsed.hostname or "").casefold().rstrip(".")
    port = parsed.port or (443 if scheme == "https" else 80)
    return scheme, host, port


def _pin_client_connections(client, profile: ObjectStorageProfile, addresses: tuple[str, ...]):
    if not addresses:
        raise _profile_error(
            "OBJECT_INPUT_ENDPOINT_UNAVAILABLE",
            "对象存储 endpoint 没有可固定的网络地址。",
        )
    http_connection = _pinned_connection_class(AWSHTTPConnection, addresses)
    https_connection = _pinned_connection_class(AWSHTTPSConnection, addresses)
    http_pool = type(
        "PinnedAWSHTTPConnectionPool",
        (AWSHTTPConnectionPool,),
        {"ConnectionCls": http_connection},
    )
    https_pool = type(
        "PinnedAWSHTTPSConnectionPool",
        (AWSHTTPSConnectionPool,),
        {"ConnectionCls": https_connection},
    )
    pools = {"http": http_pool, "https": https_pool}
    session = client._endpoint.http_session
    session._manager.clear()
    session._pool_classes_by_scheme = pools
    session._manager = PoolManager(**session._get_pool_manager_kwargs())
    session._manager.pool_classes_by_scheme = pools
    expected_origin = _origin(profile.endpoint_url)
    original_send = session.send

    def send(request):
        try:
            actual_origin = _origin(request.url)
        except ValueError as error:
            raise HTTPClientError(error=error) from error
        if actual_origin != expected_origin:
            raise HTTPClientError(
                error=RuntimeError("object storage request changed its approved origin")
            )
        return original_send(request)

    session.send = send
    return client


def _s3_client(profile: ObjectStorageProfile, addresses: tuple[str, ...]):
    timeout = max(0.1, float(settings.ANALYSIS_OBJECT_HEAD_TIMEOUT_SECONDS))
    client = boto3.client(
        "s3",
        endpoint_url=profile.endpoint_url,
        region_name=profile.region,
        aws_access_key_id=profile.access_key_id,
        aws_secret_access_key=profile.secret_access_key,
        aws_session_token=profile.session_token or None,
        config=Config(
            signature_version="s3v4",
            connect_timeout=min(5.0, timeout),
            read_timeout=timeout,
            retries={"total_max_attempts": 1, "mode": "standard"},
            s3={"addressing_style": "path"},
            proxies={},
        ),
    )
    return _pin_client_connections(client, profile, addresses)


def _head_process_entry(callback: Callable[[], Any], connection) -> None:
    try:
        connection.send(("success", callback()))
    except ObjectInputError as error:
        connection.send(
            (
                "object_error",
                {
                    "code": error.code,
                    "message": str(error),
                    "retryable": error.retryable,
                    "details": error.details,
                    "http_status": error.http_status,
                },
            )
        )
    except BaseException:
        connection.send(("internal_error", None))
    finally:
        connection.close()


def _stop_head_process(process) -> None:
    if process.pid is None:
        return
    process.join(timeout=0)
    if process.is_alive():
        process.terminate()
        process.join(timeout=0.1)
    if process.is_alive():
        process.kill()
        process.join()


def _bounded_call(callback: Callable[[], Any], timeout: float) -> Any:
    if not _HEAD_CALL_SLOTS.acquire(blocking=False):
        raise ObjectInputError(
            "OBJECT_INPUT_HEAD_BUSY",
            "对象存储预检并发槽暂时不可用。",
            retryable=True,
            http_status=503,
        )
    deadline = time.monotonic() + max(0.1, timeout)
    receive_connection = None
    send_connection = None
    process = None
    try:
        receive_connection, send_connection = _HEAD_PROCESS_CONTEXT.Pipe(duplex=False)
        process = _HEAD_PROCESS_CONTEXT.Process(
            target=_head_process_entry,
            args=(callback, send_connection),
            name="object-input-head",
            daemon=True,
        )
        process.start()
        send_connection.close()
        send_connection = None
        remaining = max(0, deadline - time.monotonic())
        if not receive_connection.poll(remaining):
            raise ObjectInputError(
                "OBJECT_INPUT_HEAD_TIMEOUT",
                "对象存储预检超过时间上限。",
                retryable=True,
                http_status=503,
            )
        try:
            status, value = receive_connection.recv()
        except EOFError as error:
            raise ObjectInputError(
                "OBJECT_INPUT_UNAVAILABLE",
                "对象存储预检进程异常退出。",
                retryable=True,
                http_status=503,
            ) from error
        if status == "success":
            return value
        if status == "object_error":
            raise ObjectInputError(
                value["code"],
                value["message"],
                retryable=value["retryable"],
                details=value["details"],
                http_status=value["http_status"],
            )
        raise ObjectInputError(
            "OBJECT_INPUT_UNAVAILABLE",
            "对象存储预检进程执行失败。",
            retryable=True,
            http_status=503,
        )
    finally:
        if send_connection is not None:
            send_connection.close()
        if receive_connection is not None:
            receive_connection.close()
        if process is not None:
            _stop_head_process(process)
            process.close()
        _HEAD_CALL_SLOTS.release()


def _normalized_etag(value: Any) -> str:
    result = str(value or "").strip()
    if result.startswith('"') and result.endswith('"') and len(result) >= 2:
        result = result[1:-1]
    return result


def _identity_token_is_safe(value: str) -> bool:
    return (
        len(value) <= 1024
        and value.isascii()
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _etag_is_safe(value: str) -> bool:
    return _identity_token_is_safe(value) and '"' not in value


def _reference(value: Any, *, input_name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("type") != "s3_object":
        raise ObjectInputError(
            "OBJECT_INPUT_REFERENCE_INVALID",
            f"输入 {input_name} 的对象引用无效。",
        )
    if set(value) - REFERENCE_FIELDS:
        raise ObjectInputError(
            "OBJECT_INPUT_REFERENCE_INVALID",
            f"输入 {input_name} 的对象引用包含未支持字段。",
        )
    raw_profile = value.get("profile")
    raw_bucket = value.get("bucket")
    raw_key = value.get("key")
    raw_version_id = value.get("version_id")
    raw_etag = value.get("etag")
    raw_digest = value.get("sha256")
    profile = raw_profile.strip() if isinstance(raw_profile, str) else ""
    bucket = raw_bucket.strip() if isinstance(raw_bucket, str) else ""
    key = raw_key if isinstance(raw_key, str) else ""
    version_id = raw_version_id.strip() if isinstance(raw_version_id, str) else ""
    etag = _normalized_etag(raw_etag) if isinstance(raw_etag, str) else ""
    size = value.get("size")
    digest_match = (
        re.fullmatch(r"sha256:([0-9a-fA-F]{64})", raw_digest.strip())
        if isinstance(raw_digest, str)
        else None
    )
    try:
        encoded_key = key.encode("utf-8")
    except UnicodeEncodeError:
        encoded_key = b""
    if (
        not isinstance(raw_profile, str)
        or not isinstance(raw_bucket, str)
        or not isinstance(raw_key, str)
        or (raw_version_id is not None and not isinstance(raw_version_id, str))
        or (raw_etag is not None and not isinstance(raw_etag, str))
        or not PROFILE_PATTERN.fullmatch(profile)
        or not BUCKET_PATTERN.fullmatch(bucket)
        or not key
        or not encoded_key
        or len(encoded_key) > 1024
        or any(ord(character) < 32 or ord(character) == 127 for character in key)
        or (not version_id and not etag)
        or (version_id and not _identity_token_is_safe(version_id))
        or (etag and not _etag_is_safe(etag))
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size <= 0
        or digest_match is None
    ):
        raise ObjectInputError(
            "OBJECT_INPUT_REFERENCE_INVALID",
            f"输入 {input_name} 必须提供有效的 profile、bucket、key、版本/ETag、size 和 SHA-256。",
        )
    if size > int(settings.ANALYSIS_OBJECT_STAGE_MAX_OBJECT_BYTES):
        raise ObjectInputError(
            "OBJECT_INPUT_TOO_LARGE",
            f"输入 {input_name} 超过单对象容量上限。",
            details={"input": input_name, "size": size},
            http_status=413,
        )
    return {
        "type": "s3_object",
        "profile": profile,
        "bucket": bucket,
        "key": key,
        "version_id": version_id,
        "etag": etag,
        "size": size,
        "sha256": f"sha256:{digest_match.group(1).lower()}",
    }


def _request_parameters(
    reference: dict[str, Any],
    profile: ObjectStorageProfile,
    *,
    conditional: bool,
) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "Bucket": reference["bucket"],
        "Key": reference["key"],
    }
    if reference.get("version_id"):
        parameters["VersionId"] = reference["version_id"]
    if conditional and reference.get("etag"):
        parameters["IfMatch"] = f'"{reference["etag"]}"'
    if profile.expected_bucket_owner:
        parameters["ExpectedBucketOwner"] = profile.expected_bucket_owner
    return parameters


def _authorize_reference(
    profile: ObjectStorageProfile,
    reference: dict[str, Any],
    *,
    client_id: str | None,
) -> None:
    if reference["bucket"] not in profile.allowed_buckets:
        raise ObjectInputError(
            "OBJECT_INPUT_BUCKET_FORBIDDEN",
            "对象 bucket 不在 profile 白名单中。",
            http_status=403,
        )
    bucket_grants = profile.client_grants.get(client_id or "")
    if bucket_grants is None:
        raise ObjectInputError(
            "OBJECT_INPUT_PROFILE_FORBIDDEN",
            "当前 Service Account 无权使用该对象存储 profile。",
            http_status=403,
        )
    prefixes = bucket_grants.get(reference["bucket"])
    if prefixes is None:
        raise ObjectInputError(
            "OBJECT_INPUT_BUCKET_FORBIDDEN",
            "当前 Service Account 无权使用该对象 bucket。",
            http_status=403,
        )
    if not any(reference["key"].startswith(prefix) for prefix in prefixes):
        raise ObjectInputError(
            "OBJECT_INPUT_KEY_FORBIDDEN",
            "对象 key 不在 profile 前缀白名单中。",
            http_status=403,
        )


def _mapped_client_error(error: BaseException, *, changed_on_missing: bool) -> ObjectInputError:
    if isinstance(error, ClientError):
        response = error.response if isinstance(error.response, dict) else {}
        metadata = response.get("ResponseMetadata") or {}
        error_value = response.get("Error") or {}
        status_code = int(metadata.get("HTTPStatusCode") or 0)
        code = str(error_value.get("Code") or "")
        if status_code == 412 or code in {"PreconditionFailed", "InvalidVersion"}:
            return ObjectInputError(
                "OBJECT_INPUT_CHANGED",
                "对象版本或 ETag 已发生变化。",
                http_status=409,
            )
        if status_code in {404, 405} or code in {"NoSuchKey", "NoSuchVersion", "NotFound"}:
            return ObjectInputError(
                "OBJECT_INPUT_CHANGED" if changed_on_missing else "OBJECT_INPUT_NOT_FOUND",
                "固定对象版本不存在或已不可访问。",
                http_status=409 if changed_on_missing else 404,
            )
        if status_code == 403 or code in {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"}:
            return ObjectInputError(
                "OBJECT_INPUT_ACCESS_DENIED",
                "对象存储拒绝访问。",
                http_status=403,
            )
    if isinstance(error, (BotoCoreError, OSError, ClientError)):
        return ObjectInputError(
            "OBJECT_INPUT_UNAVAILABLE",
            "对象存储当前不可用。",
            retryable=True,
            http_status=503,
        )
    return ObjectInputError(
        "OBJECT_INPUT_UNAVAILABLE",
        "对象存储操作失败。",
        retryable=True,
        http_status=503,
    )


def inspect_object_reference(
    value: Any,
    *,
    input_name: str,
    semantic_type: str,
    client_id: str | None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    reference = _reference(value, input_name=input_name)

    def inspect() -> dict[str, Any]:
        profile = _load_profile(reference["profile"])
        _authorize_reference(profile, reference, client_id=client_id)
        addresses = _validate_endpoint(profile)
        client = None
        try:
            client = _s3_client(profile, addresses)
            response = client.head_object(
                **_request_parameters(reference, profile, conditional=True)
            )
        except BaseException as error:
            if isinstance(error, ObjectInputError):
                raise
            raise _mapped_client_error(error, changed_on_missing=False) from error
        finally:
            close = getattr(client, "close", None) if client is not None else None
            if callable(close):
                close()
        return {
            "ContentLength": response.get("ContentLength"),
            "ETag": response.get("ETag"),
            "VersionId": response.get("VersionId"),
            "DeleteMarker": response.get("DeleteMarker") is True,
        }

    response = _bounded_call(
        inspect,
        float(settings.ANALYSIS_OBJECT_HEAD_TIMEOUT_SECONDS),
    )
    observed_size = response.get("ContentLength")
    observed_etag = _normalized_etag(response.get("ETag"))
    observed_version = str(response.get("VersionId") or "").strip()
    if response.get("DeleteMarker") is True:
        raise ObjectInputError(
            "OBJECT_INPUT_NOT_FOUND",
            f"输入 {input_name} 指向删除标记。",
            http_status=404,
        )
    if observed_size != reference["size"]:
        raise ObjectInputError(
            "OBJECT_INPUT_SIZE_MISMATCH",
            f"输入 {input_name} 的对象大小与声明不一致。",
            details={"input": input_name, "declared_size": reference["size"]},
            http_status=409,
        )
    if reference["etag"] and observed_etag != reference["etag"]:
        raise ObjectInputError(
            "OBJECT_INPUT_CHANGED",
            f"输入 {input_name} 的 ETag 已发生变化。",
            http_status=409,
        )
    if reference["version_id"] and observed_version != reference["version_id"]:
        raise ObjectInputError(
            "OBJECT_INPUT_CHANGED",
            f"输入 {input_name} 的版本已发生变化。",
            http_status=409,
        )
    if not observed_etag or not _etag_is_safe(observed_etag):
        raise ObjectInputError(
            "OBJECT_INPUT_IDENTITY_UNAVAILABLE",
            f"输入 {input_name} 的对象没有返回 ETag。",
            http_status=409,
        )
    if observed_version and not _identity_token_is_safe(observed_version):
        raise ObjectInputError(
            "OBJECT_INPUT_IDENTITY_UNAVAILABLE",
            f"输入 {input_name} 的对象返回了无效版本标识。",
            http_status=409,
        )
    pinned_version = reference["version_id"] or (
        observed_version if observed_version and observed_version != "null" else ""
    )
    pinned = {
        **reference,
        "version_id": pinned_version,
        "etag": observed_etag,
    }
    relative_path = _staging_relative_path(reference["sha256"], reference["key"])
    manifest = {
        "reference_type": "s3_object",
        "input": input_name,
        "semantic_type": semantic_type,
        "authorized_client_id": client_id or "",
        "kind": "file",
        "profile": pinned["profile"],
        "bucket": pinned["bucket"],
        "key": pinned["key"],
        "version_id": pinned["version_id"],
        "etag": pinned["etag"],
        "size": pinned["size"],
        "sha256": pinned["sha256"],
        "verification": "head+conditional-get+sha256",
        "head_checked_at": timezone.now().isoformat(),
        "staging_relative_path": relative_path,
    }
    execution_path = Path(settings.ANALYSIS_INPUT_STAGING_EXECUTION_ROOT) / relative_path
    check = {
        "check": "object_input_head",
        "input": input_name,
        "ready": True,
        "profile": pinned["profile"],
        "bucket": pinned["bucket"],
        "version_pinned": bool(pinned["version_id"]),
        "etag_pinned": True,
        "size": pinned["size"],
        "sha256": pinned["sha256"],
    }
    return str(execution_path), manifest, check


def _staging_relative_path(digest: str, key: str) -> str:
    match = SHA256_PATTERN.fullmatch(digest)
    if match is None:
        raise ObjectInputError(
            "OBJECT_INPUT_MANIFEST_INVALID",
            "对象输入清单的 SHA-256 无效。",
        )
    value = match.group(1).lower()
    safe_suffixes: list[str] = []
    suffix_length = 0
    for raw_suffix in reversed(Path(key).suffixes):
        suffix = raw_suffix.lower()
        if (
            len(safe_suffixes) >= 3
            or re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) is None
            or suffix_length + len(suffix) > 32
        ):
            break
        safe_suffixes.insert(0, suffix)
        suffix_length += len(suffix)
    suffixes = "".join(safe_suffixes)
    return f"sha256/{value[:2]}/{value}{suffixes}"


def _manifest_reference(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict) or item.get("reference_type") != "s3_object":
        raise ObjectInputError(
            "OBJECT_INPUT_MANIFEST_INVALID",
            "对象输入清单条目无效。",
        )
    client_id = item.get("authorized_client_id")
    sequence = item.get("input_sequence")
    if (
        not isinstance(item.get("input"), str)
        or not item["input"]
        or not isinstance(item.get("semantic_type"), str)
        or not item["semantic_type"]
        or not isinstance(client_id, str)
        or (client_id and not CLIENT_PATTERN.fullmatch(client_id))
        or not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 0
        or item.get("kind") != "file"
        or item.get("verification") != "head+conditional-get+sha256"
    ):
        raise ObjectInputError(
            "OBJECT_INPUT_MANIFEST_INVALID",
            "对象输入清单的审计证据无效。",
        )
    reference = _reference(
        {
            "type": "s3_object",
            "profile": item.get("profile"),
            "bucket": item.get("bucket"),
            "key": item.get("key"),
            "version_id": item.get("version_id"),
            "etag": item.get("etag"),
            "size": item.get("size"),
            "sha256": item.get("sha256"),
        },
        input_name=str(item.get("input") or "object"),
    )
    expected_relative = _staging_relative_path(reference["sha256"], reference["key"])
    if item.get("staging_relative_path") != expected_relative:
        raise ObjectInputError(
            "OBJECT_INPUT_MANIFEST_INVALID",
            "对象输入清单的暂存路径无效。",
        )
    return reference


def object_manifest_items(manifest: Any) -> list[dict[str, Any]]:
    if not isinstance(manifest, dict):
        return []
    raw_items = manifest.get("objects") or []
    if not isinstance(raw_items, list):
        raise ObjectInputError(
            "OBJECT_INPUT_MANIFEST_INVALID",
            "对象输入清单 objects 必须是数组。",
        )
    if raw_items and manifest.get("schema_version") != 2:
        raise ObjectInputError(
            "OBJECT_INPUT_MANIFEST_INVALID",
            "对象输入清单必须使用 schema_version=2。",
        )
    if len(raw_items) > int(settings.ANALYSIS_OBJECT_STAGE_MAX_ITEMS):
        raise ObjectInputError(
            "OBJECT_INPUT_LIMIT_EXCEEDED",
            "对象输入数量超过部署上限。",
        )
    items: list[dict[str, Any]] = []
    unique_sizes: dict[str, int] = {}
    transfer_identities: set[tuple[Any, ...]] = set()
    transfer_bytes = 0
    for raw_item in raw_items:
        reference = _manifest_reference(raw_item)
        staging_path = str(raw_item["staging_relative_path"])
        existing_size = unique_sizes.get(staging_path)
        if existing_size is not None and existing_size != reference["size"]:
            raise ObjectInputError(
                "OBJECT_INPUT_MANIFEST_INVALID",
                "同一对象摘要声明了不同大小。",
            )
        unique_sizes[staging_path] = reference["size"]
        identity = (
            reference["profile"],
            reference["bucket"],
            reference["key"],
            reference["version_id"],
            reference["etag"],
            reference["size"],
            reference["sha256"],
        )
        if identity not in transfer_identities:
            transfer_identities.add(identity)
            transfer_bytes += reference["size"]
        items.append(raw_item)
    if transfer_bytes > int(settings.ANALYSIS_OBJECT_STAGE_MAX_RUN_BYTES):
        raise ObjectInputError(
            "OBJECT_INPUT_RUN_TOO_LARGE",
            "任务对象输入传输总量超过部署上限。",
            http_status=413,
        )
    return items


@contextmanager
def _hard_timeout(seconds: float) -> Iterator[None]:
    if (
        threading.current_thread() is not threading.main_thread()
        or not hasattr(signal, "setitimer")
        or not hasattr(signal, "SIGALRM")
    ):
        raise ObjectInputError(
            "OBJECT_INPUT_STAGE_UNSUPPORTED",
            "当前 Worker 运行环境不支持对象暂存硬超时。",
        )
    started = time.monotonic()
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_delay, previous_interval = signal.setitimer(signal.ITIMER_REAL, 0)

    def deadline_handler(_signum, _frame) -> None:
        raise _ObjectDeadline()

    signal.signal(signal.SIGALRM, deadline_handler)
    signal.setitimer(signal.ITIMER_REAL, max(0.001, seconds))
    try:
        yield
    except _ObjectDeadline as error:
        raise ObjectInputError(
            "OBJECT_INPUT_STAGE_TIMEOUT",
            "对象输入暂存超过硬时间上限。",
            retryable=True,
        ) from error
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_delay > 0:
            remaining = max(0.001, previous_delay - (time.monotonic() - started))
            signal.setitimer(signal.ITIMER_REAL, remaining, previous_interval)


def _stage_io_error(message: str, error: BaseException) -> ObjectInputError:
    return ObjectInputError(
        "OBJECT_INPUT_STAGE_IO_ERROR",
        message,
        retryable=True,
    )


def _open_staging_root() -> int:
    root = Path(settings.ANALYSIS_INPUT_STAGING_ROOT)
    descriptor = -1
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o755)
        descriptor = os.open(root, STAGING_DIRECTORY_FLAGS)
        if not stat_module.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("staging root is not a directory")
        return descriptor
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise _stage_io_error("对象暂存根目录不可安全访问。", error) from error


def _open_directory_at(
    parent_descriptor: int,
    name: str,
    *,
    create: bool,
    mode: int = 0o755,
) -> int:
    descriptor = -1
    try:
        if create:
            try:
                os.mkdir(name, mode=mode, dir_fd=parent_descriptor)
            except FileExistsError:
                pass
        descriptor = os.open(name, STAGING_DIRECTORY_FLAGS, dir_fd=parent_descriptor)
        if not stat_module.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("staging path component is not a directory")
        return descriptor
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise _stage_io_error("对象暂存目录不可安全访问。", error) from error


@contextmanager
def _open_target_parent(reference: dict[str, Any]) -> Iterator[tuple[int, str]]:
    relative = Path(_staging_relative_path(reference["sha256"], reference["key"]))
    descriptors: list[int] = []
    try:
        current = _open_staging_root()
        descriptors.append(current)
        for part in relative.parent.parts:
            current = _open_directory_at(current, part, create=True)
            descriptors.append(current)
        yield current, relative.name
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _target_path(reference: dict[str, Any]) -> Path:
    with _open_target_parent(reference):
        pass
    return Path(settings.ANALYSIS_INPUT_STAGING_ROOT) / _staging_relative_path(
        reference["sha256"], reference["key"]
    )


def _target_exists_at(parent_descriptor: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    except OSError as error:
        raise _stage_io_error("对象暂存目标无法安全检查。", error) from error


def _file_identity(metadata) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_dev,
        metadata.st_ino,
    )


def _hash_target_at(
    parent_descriptor: int,
    name: str,
    *,
    expected_size: int,
    checkpoint=None,
) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as error:
        raise ObjectInputError(
            "OBJECT_INPUT_STAGING_MISSING",
            "暂存对象不存在或不可安全读取。",
        ) from error
    digest = hashlib.sha256()
    total = 0
    try:
        metadata = os.fstat(descriptor)
        before = _file_identity(metadata)
        if (
            not stat_module.S_ISREG(metadata.st_mode)
            or metadata.st_size != expected_size
            or metadata.st_mode & 0o222
        ):
            raise ObjectInputError(
                "OBJECT_INPUT_STAGING_CHANGED",
                "暂存对象大小、权限或文件类型已发生变化。",
            )
        while True:
            if checkpoint is not None:
                checkpoint()
            chunk = os.read(descriptor, int(settings.ANALYSIS_OBJECT_STAGE_CHUNK_BYTES))
            if not chunk:
                break
            total += len(chunk)
            if total > expected_size:
                raise ObjectInputError(
                    "OBJECT_INPUT_STAGING_CHANGED",
                    "暂存对象大小已发生变化。",
                )
            digest.update(chunk)
        after_metadata = os.fstat(descriptor)
    except ObjectInputError:
        raise
    except OSError as error:
        raise _stage_io_error("暂存对象无法稳定读取。", error) from error
    finally:
        os.close(descriptor)
    try:
        current_metadata = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        raise ObjectInputError(
            "OBJECT_INPUT_STAGING_CHANGED",
            "暂存对象在校验期间发生变化。",
        ) from error
    if (
        before != _file_identity(after_metadata)
        or _file_identity(after_metadata) != _file_identity(current_metadata)
        or not stat_module.S_ISREG(current_metadata.st_mode)
        or current_metadata.st_mode & 0o222
        or total != expected_size
    ):
        raise ObjectInputError(
            "OBJECT_INPUT_STAGING_CHANGED",
            "暂存对象在校验期间发生变化。",
        )
    return f"sha256:{digest.hexdigest()}"


@contextmanager
def _open_lease_directory(lease_id: uuid.UUID) -> Iterator[int]:
    root_descriptor = _open_staging_root()
    leases_descriptor = -1
    lease_descriptor = -1
    try:
        leases_descriptor = _open_directory_at(
            root_descriptor,
            ".leases",
            create=True,
            mode=0o700,
        )
        lease_descriptor = _open_directory_at(
            leases_descriptor,
            str(lease_id),
            create=True,
            mode=0o700,
        )
        yield lease_descriptor
    finally:
        if lease_descriptor >= 0:
            os.close(lease_descriptor)
        if leases_descriptor >= 0:
            os.close(leases_descriptor)
        os.close(root_descriptor)


def _remove_flat_directory(parent_descriptor: int, name: str) -> None:
    try:
        descriptor = os.open(
            name,
            STAGING_DIRECTORY_FLAGS,
            dir_fd=parent_descriptor,
        )
    except FileNotFoundError:
        return
    except OSError as error:
        raise _stage_io_error("对象暂存 lease 目录不可安全访问。", error) from error
    try:
        for child in os.listdir(descriptor):
            metadata = os.stat(child, dir_fd=descriptor, follow_symlinks=False)
            if not (
                stat_module.S_ISREG(metadata.st_mode)
                or stat_module.S_ISLNK(metadata.st_mode)
            ):
                raise OSError("lease directory contains an unsupported node")
            os.unlink(child, dir_fd=descriptor)
        os.fsync(descriptor)
    except OSError as error:
        raise _stage_io_error("对象暂存 lease 临时文件无法清理。", error) from error
    finally:
        os.close(descriptor)
    try:
        os.rmdir(name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    except FileNotFoundError:
        return
    except OSError as error:
        raise _stage_io_error("对象暂存 lease 目录无法清理。", error) from error


def _cleanup_stale_lease_directories(active_ids: set[str]) -> None:
    root_descriptor = _open_staging_root()
    try:
        try:
            leases_descriptor = _open_directory_at(
                root_descriptor,
                ".leases",
                create=False,
            )
        except ObjectInputError as error:
            if isinstance(error.__cause__, FileNotFoundError):
                return
            raise
        try:
            for name in os.listdir(leases_descriptor):
                try:
                    normalized = str(uuid.UUID(name))
                except ValueError:
                    continue
                if normalized not in active_ids:
                    _remove_flat_directory(leases_descriptor, normalized)
        finally:
            os.close(leases_descriptor)
    finally:
        os.close(root_descriptor)


def _remove_lease_directory(lease_id: uuid.UUID) -> None:
    root_descriptor = _open_staging_root()
    try:
        try:
            leases_descriptor = _open_directory_at(
                root_descriptor,
                ".leases",
                create=False,
            )
        except ObjectInputError as error:
            if isinstance(error.__cause__, FileNotFoundError):
                return
            raise
        try:
            _remove_flat_directory(leases_descriptor, str(lease_id))
        finally:
            os.close(leases_descriptor)
    finally:
        os.close(root_descriptor)


def _download_to_staging(
    item: dict[str, Any],
    *,
    client_id: str,
    temporary_directory: int,
    checkpoint=None,
) -> None:
    reference = _manifest_reference(item)
    client = None
    body = None
    temporary_name = ""
    descriptor = -1
    with _open_target_parent(reference) as (target_parent, target_name):
        target_exists = _target_exists_at(target_parent, target_name)
        if target_exists:
            observed = _hash_target_at(
                target_parent,
                target_name,
                expected_size=reference["size"],
                checkpoint=checkpoint,
            )
            if observed != reference["sha256"]:
                raise ObjectInputError(
                    "OBJECT_INPUT_STAGING_CHANGED",
                    "暂存对象 SHA-256 已发生变化。",
                )
        profile = _load_profile(reference["profile"])
        _authorize_reference(profile, reference, client_id=client_id)
        addresses = _validate_endpoint(profile)
        try:
            client = _s3_client(profile, addresses)
            response = client.get_object(
                **_request_parameters(reference, profile, conditional=True)
            )
            body = response.get("Body")
            observed_size = response.get("ContentLength")
            observed_etag = _normalized_etag(response.get("ETag"))
            observed_version = str(response.get("VersionId") or "").strip()
            if not _etag_is_safe(observed_etag) or (
                observed_version and not _identity_token_is_safe(observed_version)
            ):
                raise ObjectInputError(
                    "OBJECT_INPUT_CHANGED",
                    "对象在暂存前返回了无效身份标识。",
                )
            if observed_size != reference["size"]:
                raise ObjectInputError(
                    "OBJECT_INPUT_CHANGED",
                    "对象大小在暂存前发生变化。",
                )
            if observed_etag != reference["etag"]:
                raise ObjectInputError(
                    "OBJECT_INPUT_CHANGED",
                    "对象 ETag 在暂存前发生变化。",
                )
            if reference["version_id"] and observed_version != reference["version_id"]:
                raise ObjectInputError(
                    "OBJECT_INPUT_CHANGED",
                    "对象版本在暂存前发生变化。",
                )
            if body is None or not callable(getattr(body, "read", None)):
                raise ObjectInputError(
                    "OBJECT_INPUT_UNAVAILABLE",
                    "对象存储没有返回可读内容。",
                    retryable=True,
                )
            if not target_exists:
                temporary_name = f"{target_name}.{uuid.uuid4().hex}.part"
                flags = (
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                descriptor = os.open(
                    temporary_name,
                    flags,
                    0o600,
                    dir_fd=temporary_directory,
                )
            digest = hashlib.sha256()
            total = 0
            while True:
                if checkpoint is not None:
                    checkpoint()
                chunk = body.read(int(settings.ANALYSIS_OBJECT_STAGE_CHUNK_BYTES))
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise ObjectInputError(
                        "OBJECT_INPUT_UNAVAILABLE",
                        "对象存储返回了无效内容。",
                        retryable=True,
                    )
                total += len(chunk)
                if total > reference["size"]:
                    raise ObjectInputError(
                        "OBJECT_INPUT_CHANGED",
                        "对象大小在下载期间发生变化。",
                    )
                digest.update(chunk)
                if descriptor >= 0:
                    view = memoryview(chunk)
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:
                            raise OSError("staging write made no progress")
                        view = view[written:]
            if total != reference["size"]:
                raise ObjectInputError(
                    "OBJECT_INPUT_CHANGED",
                    "对象大小与声明不一致。",
                )
            observed_digest = f"sha256:{digest.hexdigest()}"
            if observed_digest != reference["sha256"]:
                raise ObjectInputError(
                    "OBJECT_INPUT_DIGEST_MISMATCH",
                    "对象 SHA-256 与声明不一致。",
                )
            if target_exists:
                current_digest = _hash_target_at(
                    target_parent,
                    target_name,
                    expected_size=reference["size"],
                    checkpoint=checkpoint,
                )
                if current_digest != reference["sha256"]:
                    raise ObjectInputError(
                        "OBJECT_INPUT_STAGING_CHANGED",
                        "暂存对象在远端复核期间发生变化。",
                    )
                return
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o444)
            os.close(descriptor)
            descriptor = -1
            try:
                os.link(
                    temporary_name,
                    target_name,
                    src_dir_fd=temporary_directory,
                    dst_dir_fd=target_parent,
                    follow_symlinks=False,
                )
            except FileExistsError:
                current_digest = _hash_target_at(
                    target_parent,
                    target_name,
                    expected_size=reference["size"],
                    checkpoint=checkpoint,
                )
                if current_digest != reference["sha256"]:
                    raise ObjectInputError(
                        "OBJECT_INPUT_STAGING_CHANGED",
                        "并发写入的暂存对象摘要不一致。",
                    )
            os.fsync(target_parent)
        except ObjectInputError:
            raise
        except (BotoCoreError, ClientError) as error:
            raise _mapped_client_error(error, changed_on_missing=True) from error
        except OSError as error:
            raise _stage_io_error("对象暂存文件系统操作失败。", error) from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if body is not None:
                close_body = getattr(body, "close", None)
                if callable(close_body):
                    close_body()
            close_client = getattr(client, "close", None) if client is not None else None
            if callable(close_client):
                close_client()
            if temporary_name:
                try:
                    os.unlink(temporary_name, dir_fd=temporary_directory)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass


def _validated_staged_metadata(
    parent_descriptor: int,
    name: str,
    *,
    expected_size: int,
):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        metadata = os.fstat(descriptor)
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise ObjectInputError(
            "OBJECT_INPUT_STAGING_MISSING",
            "暂存对象不存在或不可安全读取。",
        ) from error
    if (
        not stat_module.S_ISREG(metadata.st_mode)
        or metadata.st_size != expected_size
        or metadata.st_mode & 0o222
    ):
        os.close(descriptor)
        raise ObjectInputError(
            "OBJECT_INPUT_STAGING_CHANGED",
            "暂存对象大小、权限或文件类型已发生变化。",
        )
    return descriptor, metadata


def _validate_staged_semantic(
    item: dict[str, Any],
    *,
    checkpoint=None,
) -> tuple[int, str] | None:
    reference = _manifest_reference(item)
    semantic_type = str(item.get("semantic_type") or "")
    if semantic_type not in {"bio.fastq.gz.r1", "bio.fastq.gz.r2"} and (
        "fasta" not in semantic_type.casefold()
    ):
        return None
    with _open_target_parent(reference) as (parent_descriptor, name):
        descriptor, before_metadata = _validated_staged_metadata(
            parent_descriptor,
            name,
            expected_size=reference["size"],
        )
        try:
            if checkpoint is not None:
                checkpoint()
            if semantic_type in {"bio.fastq.gz.r1", "bio.fastq.gz.r2"}:
                if not reference["key"].lower().endswith((".fastq.gz", ".fq.gz")):
                    raise ObjectInputError(
                        "FASTQ_EXTENSION_INVALID",
                        "对象 FASTQ 文件扩展名无效。",
                    )
                with os.fdopen(os.dup(descriptor), "rb") as handle:
                    lines = _read_gzip_text_lines(
                        handle,
                        line_count=4,
                        max_chars=settings.ANALYSIS_INPUT_TEXT_LINE_MAX_CHARS,
                        encoding="utf-8",
                        checkpoint=checkpoint,
                    )
                header, sequence, plus, quality = lines
                if (
                    not header.startswith("@")
                    or not header[1:].split()
                    or not sequence
                    or not plus.startswith("+")
                    or len(sequence) != len(quality)
                ):
                    raise ObjectInputError(
                        "FASTQ_RECORD_INVALID",
                        "对象 FASTQ 首条记录结构无效。",
                    )
                expected_mate = 1 if semantic_type.endswith(".r1") else 2
                parts = header[1:].split()
                slash_mate = re.search(r"/([12])$", parts[0])
                if slash_mate and int(slash_mate.group(1)) != expected_mate:
                    raise ObjectInputError(
                        "FASTQ_MATE_INVALID",
                        "对象 FASTQ mate 与输入语义不一致。",
                    )
                read_id = re.sub(r"/[12]$", "", parts[0])
                if len(parts) > 1 and re.match(r"^[12]:", parts[1]):
                    if int(parts[1][0]) != expected_mate:
                        raise ObjectInputError(
                            "FASTQ_MATE_INVALID",
                            "对象 FASTQ mate 与输入语义不一致。",
                        )
                read_id_digest = (
                    f"sha256:{hashlib.sha256(read_id.encode('utf-8')).hexdigest()}"
                )
                evidence: tuple[int, str] | None = (expected_mate, read_id_digest)
            else:
                with os.fdopen(os.dup(descriptor), "rb") as handle:
                    if reference["key"].lower().endswith(".gz"):
                        first_line = _read_gzip_text_lines(
                            handle,
                            line_count=1,
                            max_chars=settings.ANALYSIS_INPUT_TEXT_LINE_MAX_CHARS,
                            encoding="utf-8",
                            checkpoint=checkpoint,
                        )[0]
                    else:
                        raw_line = handle.readline(
                            settings.ANALYSIS_INPUT_TEXT_LINE_MAX_CHARS + 1
                        )
                        if len(raw_line) > settings.ANALYSIS_INPUT_TEXT_LINE_MAX_CHARS:
                            raise ObjectInputError(
                                "INPUT_RECORD_TOO_LARGE",
                                "对象 FASTA 首行超过安全上限。",
                            )
                        first_line = raw_line.decode("utf-8", errors="strict").rstrip(
                            "\r\n"
                        )
                if not first_line.startswith(">"):
                    raise ObjectInputError(
                        "FASTA_CONTENT_INVALID",
                        "对象 FASTA 第一行缺少 > header。",
                    )
                evidence = None
            after_metadata = os.fstat(descriptor)
            current_metadata = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                _file_identity(before_metadata) != _file_identity(after_metadata)
                or _file_identity(after_metadata) != _file_identity(current_metadata)
                or not stat_module.S_ISREG(current_metadata.st_mode)
                or current_metadata.st_mode & 0o222
            ):
                raise ObjectInputError(
                    "OBJECT_INPUT_STAGING_CHANGED",
                    "暂存对象在语义校验期间发生变化。",
                )
            return evidence
        except ObjectInputError:
            raise
        except GzipProbeLineLimitError as error:
            raise ObjectInputError(
                "INPUT_RECORD_TOO_LARGE",
                "对象输入记录超过安全上限。",
            ) from error
        except (OSError, UnicodeError, ValueError) as error:
            code = (
                "FASTQ_GZIP_INVALID"
                if semantic_type in {"bio.fastq.gz.r1", "bio.fastq.gz.r2"}
                else "FASTA_CONTENT_INVALID"
            )
            raise ObjectInputError(code, "对象输入语义内容无效。") from error
        finally:
            os.close(descriptor)


def _try_claim_staging_lease(
    run: AnalysisRun,
    *,
    reserved_bytes: int,
) -> tuple[InputStagingLease | None, str]:
    if run.lease_token is None:
        raise ObjectInputError(
            "OBJECT_INPUT_STAGE_LEASE_LOST",
            "分析任务没有有效 Worker lease。",
            retryable=True,
        )
    root = Path(settings.ANALYSIS_INPUT_STAGING_ROOT)
    root_descriptor = _open_staging_root()
    os.close(root_descriptor)
    with transaction.atomic():
        InputStagingCoordinator.objects.select_for_update().get(pk=1)
        now = timezone.now()
        InputStagingLease.objects.filter(expires_at__lte=now).delete()
        existing = InputStagingLease.objects.filter(run=run).first()
        if existing is not None:
            if existing.worker_lease_token == run.lease_token:
                existing.reserved_bytes = reserved_bytes
                existing.expires_at = now + timedelta(
                    seconds=int(settings.ANALYSIS_OBJECT_STAGE_LEASE_SECONDS)
                )
                existing.save(
                    update_fields=["reserved_bytes", "expires_at", "updated_at"]
                )
                active_ids = {
                    str(item)
                    for item in InputStagingLease.objects.values_list("id", flat=True)
                }
                _cleanup_stale_lease_directories(active_ids)
                return existing, ""
            existing.delete()
        active = InputStagingLease.objects.all()
        active_ids = {str(item) for item in active.values_list("id", flat=True)}
        _cleanup_stale_lease_directories(active_ids)
        if active.count() >= int(settings.ANALYSIS_OBJECT_STAGE_MAX_CONCURRENT_RUNS):
            return None, "busy"
        active_bytes = int(active.aggregate(total=Sum("reserved_bytes"))["total"] or 0)
        if active_bytes + reserved_bytes > int(
            settings.ANALYSIS_OBJECT_STAGE_MAX_RESERVED_BYTES
        ):
            return None, "capacity"
        free_bytes = shutil.disk_usage(root).free
        if free_bytes < (
            active_bytes
            + reserved_bytes
            + int(settings.ANALYSIS_OBJECT_STAGE_MIN_FREE_BYTES)
        ):
            return None, "capacity"
        lease = InputStagingLease.objects.create(
            run=run,
            worker_lease_token=run.lease_token,
            reserved_bytes=reserved_bytes,
            expires_at=now
            + timedelta(seconds=int(settings.ANALYSIS_OBJECT_STAGE_LEASE_SECONDS)),
        )
        return lease, ""


def _claim_staging_lease(
    run: AnalysisRun,
    *,
    reserved_bytes: int,
    checkpoint=None,
) -> InputStagingLease:
    deadline = time.monotonic() + float(settings.ANALYSIS_OBJECT_STAGE_SLOT_WAIT_SECONDS)
    last_reason = "busy"
    while True:
        if checkpoint is not None:
            checkpoint()
        lease, reason = _try_claim_staging_lease(run, reserved_bytes=reserved_bytes)
        if lease is not None:
            return lease
        last_reason = reason
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            code = (
                "OBJECT_INPUT_STAGE_CAPACITY"
                if last_reason == "capacity"
                else "OBJECT_INPUT_STAGE_BUSY"
            )
            message = (
                "对象暂存容量暂时不足。"
                if last_reason == "capacity"
                else "对象暂存并发槽暂时不可用。"
            )
            raise ObjectInputError(code, message, retryable=True)
        time.sleep(min(0.25, remaining))


def _refresh_staging_lease(lease: InputStagingLease, run: AnalysisRun) -> None:
    updated = InputStagingLease.objects.filter(
        pk=lease.pk,
        run=run,
        worker_lease_token=run.lease_token,
    ).update(
        expires_at=timezone.now()
        + timedelta(seconds=int(settings.ANALYSIS_OBJECT_STAGE_LEASE_SECONDS)),
        updated_at=timezone.now(),
    )
    if updated != 1:
        raise ObjectInputError(
            "OBJECT_INPUT_STAGE_LEASE_LOST",
            "对象暂存 lease 已丢失。",
            retryable=True,
        )


def stage_run_object_inputs(run: AnalysisRun, *, checkpoint=None) -> int:
    manifest = run.request_payload.get("input_resource_manifest")
    items = object_manifest_items(manifest)
    if not items:
        return 0
    if run.service_account_id is None:
        raise ObjectInputError(
            "OBJECT_INPUT_PROFILE_FORBIDDEN",
            "对象输入任务必须关联 Service Account。",
        )
    client_id = str(run.service_account.client_id)
    unique_references: dict[tuple[Any, ...], tuple[dict[str, Any], dict[str, Any]]] = {}
    missing_paths: dict[str, int] = {}
    missing_bytes = 0
    for item in items:
        reference = _manifest_reference(item)
        if item.get("authorized_client_id") != client_id:
            raise ObjectInputError(
                "OBJECT_INPUT_MANIFEST_INVALID",
                "对象输入清单的 Service Account 证据无效。",
            )
        identity = (
            reference["profile"],
            reference["bucket"],
            reference["key"],
            reference["version_id"],
            reference["etag"],
            reference["size"],
            reference["sha256"],
        )
        unique_references.setdefault(identity, (item, reference))
        staging_path = str(item["staging_relative_path"])
        if staging_path in missing_paths:
            continue
        with _open_target_parent(reference) as (parent_descriptor, name):
            exists = _target_exists_at(parent_descriptor, name)
        missing_paths[staging_path] = 0 if exists else reference["size"]
        if not exists:
            missing_bytes += reference["size"]
    lease = _claim_staging_lease(
        run,
        reserved_bytes=max(1, missing_bytes),
        checkpoint=checkpoint,
    )
    run_deadline = time.monotonic() + float(
        settings.ANALYSIS_OBJECT_STAGE_RUN_TIMEOUT_SECONDS
    )
    reads: dict[int, list[tuple[int, str]]] = {1: [], 2: []}
    for managed in (manifest or {}).get("files") or []:
        evidence = managed.get("semantic_evidence") if isinstance(managed, dict) else None
        if not isinstance(evidence, dict) or evidence.get("kind") != "fastq":
            continue
        mate = evidence.get("mate")
        read_id = evidence.get("first_read_id_sha256")
        sequence = managed.get("input_sequence")
        if (
            mate not in {1, 2}
            or not isinstance(read_id, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", read_id) is None
            or not isinstance(sequence, int)
            or isinstance(sequence, bool)
        ):
            raise ObjectInputError(
                "OBJECT_INPUT_MANIFEST_INVALID",
                "受管 FASTQ 语义证据无效。",
            )
        reads[mate].append((sequence, read_id))
    stage_error: BaseException | None = None
    try:
        with _open_lease_directory(lease.id) as temporary_directory:
            for item, _reference_value in unique_references.values():
                _refresh_staging_lease(lease, run)
                remaining = run_deadline - time.monotonic()
                if remaining <= 0:
                    raise ObjectInputError(
                        "OBJECT_INPUT_STAGE_TIMEOUT",
                        "任务对象输入暂存超过总时间上限。",
                        retryable=True,
                    )
                with _hard_timeout(
                    min(float(settings.ANALYSIS_OBJECT_STAGE_TIMEOUT_SECONDS), remaining)
                ):
                    _download_to_staging(
                        item,
                        client_id=client_id,
                        temporary_directory=temporary_directory,
                        checkpoint=checkpoint,
                    )
            for item in items:
                _refresh_staging_lease(lease, run)
                remaining = run_deadline - time.monotonic()
                if remaining <= 0:
                    raise ObjectInputError(
                        "OBJECT_INPUT_STAGE_TIMEOUT",
                        "任务对象输入暂存超过总时间上限。",
                        retryable=True,
                    )
                with _hard_timeout(
                    min(float(settings.ANALYSIS_OBJECT_STAGE_TIMEOUT_SECONDS), remaining)
                ):
                    evidence = _validate_staged_semantic(item, checkpoint=checkpoint)
                    if evidence is not None:
                        sequence = item.get("input_sequence")
                        if not isinstance(sequence, int) or isinstance(sequence, bool):
                            raise ObjectInputError(
                                "OBJECT_INPUT_MANIFEST_INVALID",
                                "对象 FASTQ 输入顺序证据无效。",
                            )
                        mate, read_id = evidence
                        reads[mate].append((sequence, read_id))
        if reads[1] or reads[2]:
            read1 = [value for _, value in sorted(reads[1])]
            read2 = [value for _, value in sorted(reads[2])]
            if len(read1) != len(read2) or read1 != read2:
                raise ObjectInputError(
                    "FASTQ_PAIR_MISMATCH",
                    "R1/R2 FASTQ 首条 read ID 不配对。",
                )
    except BaseException as error:
        stage_error = error
        raise
    finally:
        try:
            try:
                _remove_lease_directory(lease.id)
            except BaseException:
                if stage_error is None:
                    raise
        finally:
            InputStagingLease.objects.filter(
                pk=lease.pk,
                run=run,
                worker_lease_token=run.lease_token,
            ).delete()
    return len(items)


def verify_run_object_inputs(run: AnalysisRun, *, checkpoint=None) -> int:
    items = object_manifest_items(run.request_payload.get("input_resource_manifest"))
    verified_paths: set[str] = set()
    run_deadline = time.monotonic() + float(
        settings.ANALYSIS_OBJECT_STAGE_RUN_TIMEOUT_SECONDS
    )
    for item in items:
        reference = _manifest_reference(item)
        staging_path = str(item["staging_relative_path"])
        if staging_path in verified_paths:
            continue
        remaining = run_deadline - time.monotonic()
        if remaining <= 0:
            raise ObjectInputError(
                "OBJECT_INPUT_STAGE_TIMEOUT",
                "对象输入执行前复核超过总时间上限。",
                retryable=True,
            )
        with _hard_timeout(
            min(float(settings.ANALYSIS_OBJECT_STAGE_TIMEOUT_SECONDS), remaining)
        ):
            with _open_target_parent(reference) as (parent_descriptor, name):
                digest = _hash_target_at(
                    parent_descriptor,
                    name,
                    expected_size=reference["size"],
                    checkpoint=checkpoint,
                )
        if digest != reference["sha256"]:
            raise ObjectInputError(
                "OBJECT_INPUT_STAGING_CHANGED",
                "暂存对象在启动流程前发生变化。",
            )
        verified_paths.add(staging_path)
    return len(items)
