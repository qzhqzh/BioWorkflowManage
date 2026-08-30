from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import queue
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
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import AnalysisRun, InputStagingCoordinator, InputStagingLease


PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
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
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        session_token=session_token,
        allow_http=value.get("allow_http") is True,
        allow_private_network=value.get("allow_private_network") is True,
        allowed_networks=allowed_networks,
        expected_bucket_owner=str(value.get("expected_bucket_owner") or "").strip(),
    )


def _validate_endpoint(profile: ObjectStorageProfile) -> None:
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
        if address.is_multicast or address.is_unspecified or address.is_link_local:
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


def _s3_client(profile: ObjectStorageProfile):
    timeout = max(0.1, float(settings.ANALYSIS_OBJECT_HEAD_TIMEOUT_SECONDS))
    return boto3.client(
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
        ),
    )


def _bounded_call(callback: Callable[[], Any], timeout: float) -> Any:
    results: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            results.put((True, callback()))
        except BaseException as error:
            results.put((False, error))

    thread = threading.Thread(target=invoke, name="object-input-head", daemon=True)
    thread.start()
    thread.join(timeout=max(0.1, timeout))
    if thread.is_alive():
        raise ObjectInputError(
            "OBJECT_INPUT_HEAD_TIMEOUT",
            "对象存储预检超过时间上限。",
            retryable=True,
            http_status=503,
        )
    succeeded, value = results.get_nowait()
    if succeeded:
        return value
    raise value


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
    profile = str(value.get("profile") or "").strip()
    bucket = str(value.get("bucket") or "").strip()
    key = str(value.get("key") or "")
    version_id = str(value.get("version_id") or "").strip()
    etag = _normalized_etag(value.get("etag"))
    size = value.get("size")
    digest_match = SHA256_PATTERN.fullmatch(str(value.get("sha256") or "").strip())
    if (
        not PROFILE_PATTERN.fullmatch(profile)
        or not BUCKET_PATTERN.fullmatch(bucket)
        or not key
        or len(key.encode("utf-8")) > 1024
        or any(ord(character) < 32 or ord(character) == 127 for character in key)
        or (not version_id and not etag)
        or len(version_id) > 1024
        or len(etag) > 1024
        or (version_id and not version_id.isascii())
        or (etag and not etag.isascii())
        or any(ord(character) < 32 or ord(character) == 127 for character in version_id)
        or any(ord(character) < 32 or ord(character) == 127 for character in etag)
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
        parameters["IfMatch"] = reference["etag"]
    if profile.expected_bucket_owner:
        parameters["ExpectedBucketOwner"] = profile.expected_bucket_owner
    return parameters


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
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    reference = _reference(value, input_name=input_name)

    def inspect() -> tuple[ObjectStorageProfile, dict[str, Any]]:
        profile = _load_profile(reference["profile"])
        if reference["bucket"] not in profile.allowed_buckets:
            raise ObjectInputError(
                "OBJECT_INPUT_BUCKET_FORBIDDEN",
                f"输入 {input_name} 的 bucket 不在 profile 白名单中。",
                http_status=403,
            )
        _validate_endpoint(profile)
        client = _s3_client(profile)
        try:
            response = client.head_object(
                **_request_parameters(reference, profile, conditional=True)
            )
        except BaseException as error:
            if isinstance(error, ObjectInputError):
                raise
            raise _mapped_client_error(error, changed_on_missing=False) from error
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        return profile, response

    _, response = _bounded_call(
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
    if not observed_etag or not _identity_token_is_safe(observed_etag):
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
    suffixes = "".join(Path(key).suffixes[-3:]).lower()
    if len(suffixes) > 32 or not re.fullmatch(r"(?:\.[a-z0-9]{1,10}){0,3}", suffixes):
        suffixes = ""
    return f"sha256/{value[:2]}/{value}{suffixes}"


def _manifest_reference(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict) or item.get("reference_type") != "s3_object":
        raise ObjectInputError(
            "OBJECT_INPUT_MANIFEST_INVALID",
            "对象输入清单条目无效。",
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
        items.append(raw_item)
    if sum(unique_sizes.values()) > int(settings.ANALYSIS_OBJECT_STAGE_MAX_RUN_BYTES):
        raise ObjectInputError(
            "OBJECT_INPUT_RUN_TOO_LARGE",
            "任务对象输入总量超过部署上限。",
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


def _target_path(reference: dict[str, Any]) -> Path:
    root = Path(settings.ANALYSIS_INPUT_STAGING_ROOT)
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o755)
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise OSError("staging root is not a directory")
        relative = Path(
            _staging_relative_path(reference["sha256"], reference["key"])
        )
        current = resolved_root
        for part in relative.parent.parts:
            current = current / part
            try:
                current.mkdir(mode=0o755)
            except FileExistsError:
                pass
            metadata = os.stat(current, follow_symlinks=False)
            if not stat_module.S_ISDIR(metadata.st_mode):
                raise OSError("staging path contains a non-directory ancestor")
        return current / relative.name
    except OSError as error:
        raise ObjectInputError(
            "OBJECT_INPUT_STAGE_IO_ERROR",
            "对象暂存目录不可安全访问。",
            retryable=True,
        ) from error


def _hash_regular_file(path: Path, *, expected_size: int, checkpoint=None) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ObjectInputError(
            "OBJECT_INPUT_STAGING_MISSING",
            "暂存对象不存在或不可安全读取。",
        ) from error
    digest = hashlib.sha256()
    total = 0
    try:
        metadata = os.fstat(descriptor)
        before = (
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
            metadata.st_dev,
            metadata.st_ino,
        )
        if (
            not stat_module.S_ISREG(metadata.st_mode)
            or metadata.st_size != expected_size
            or metadata.st_mode & 0o222
        ):
            raise ObjectInputError(
                "OBJECT_INPUT_STAGING_CHANGED",
                "暂存对象大小或文件类型已发生变化。",
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
        raise ObjectInputError(
            "OBJECT_INPUT_STAGE_IO_ERROR",
            "暂存对象无法稳定读取。",
            retryable=True,
        ) from error
    finally:
        os.close(descriptor)
    try:
        current_metadata = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise ObjectInputError(
            "OBJECT_INPUT_STAGING_CHANGED",
            "暂存对象在校验期间发生变化。",
        ) from error
    after = (
        after_metadata.st_size,
        after_metadata.st_mtime_ns,
        after_metadata.st_ctime_ns,
        after_metadata.st_dev,
        after_metadata.st_ino,
    )
    current = (
        current_metadata.st_size,
        current_metadata.st_mtime_ns,
        current_metadata.st_ctime_ns,
        current_metadata.st_dev,
        current_metadata.st_ino,
    )
    if (
        before != after
        or after != current
        or not stat_module.S_ISREG(current_metadata.st_mode)
        or current_metadata.st_mode & 0o222
    ):
        raise ObjectInputError(
            "OBJECT_INPUT_STAGING_CHANGED",
            "暂存对象在校验期间发生变化。",
        )
    if total != expected_size:
        raise ObjectInputError(
            "OBJECT_INPUT_STAGING_CHANGED",
            "暂存对象大小已发生变化。",
        )
    return f"sha256:{digest.hexdigest()}"


def _download_to_staging(
    item: dict[str, Any],
    *,
    checkpoint=None,
) -> None:
    reference = _manifest_reference(item)
    target = _target_path(reference)
    if os.path.lexists(target):
        observed = _hash_regular_file(
            target,
            expected_size=reference["size"],
            checkpoint=checkpoint,
        )
        if observed != reference["sha256"]:
            raise ObjectInputError(
                "OBJECT_INPUT_STAGING_CHANGED",
                "暂存对象 SHA-256 已发生变化。",
            )
        return
    profile = _load_profile(reference["profile"])
    if reference["bucket"] not in profile.allowed_buckets:
        raise ObjectInputError(
            "OBJECT_INPUT_BUCKET_FORBIDDEN",
            "对象 bucket 不在 profile 白名单中。",
        )
    _validate_endpoint(profile)
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.part"
    client = None
    response: dict[str, Any] | None = None
    body = None
    try:
        client = _s3_client(profile)
        response = client.get_object(
            **_request_parameters(reference, profile, conditional=True)
        )
        observed_size = response.get("ContentLength")
        observed_etag = _normalized_etag(response.get("ETag"))
        observed_version = str(response.get("VersionId") or "").strip()
        if not _identity_token_is_safe(observed_etag) or (
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
        body = response.get("Body")
        if body is None or not callable(getattr(body, "read", None)):
            raise ObjectInputError(
                "OBJECT_INPUT_UNAVAILABLE",
                "对象存储没有返回可读内容。",
                retryable=True,
            )
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(temporary, flags, 0o600)
        digest = hashlib.sha256()
        total = 0
        try:
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
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("staging write made no progress")
                    view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
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
        os.chmod(temporary, 0o444, follow_symlinks=False)
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            observed_digest = _hash_regular_file(
                target,
                expected_size=reference["size"],
                checkpoint=checkpoint,
            )
            if observed_digest != reference["sha256"]:
                raise ObjectInputError(
                    "OBJECT_INPUT_STAGING_CHANGED",
                    "并发写入的暂存对象摘要不一致。",
                )
        parent_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        parent_descriptor = os.open(target.parent, parent_flags)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except ObjectInputError:
        raise
    except (BotoCoreError, ClientError) as error:
        raise _mapped_client_error(error, changed_on_missing=True) from error
    except OSError as error:
        raise ObjectInputError(
            "OBJECT_INPUT_STAGE_IO_ERROR",
            "对象暂存文件系统操作失败。",
            retryable=True,
        ) from error
    finally:
        if body is not None:
            close_body = getattr(body, "close", None)
            if callable(close_body):
                close_body()
        close_client = getattr(client, "close", None) if client is not None else None
        if callable(close_client):
            close_client()
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


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
    root.mkdir(parents=True, exist_ok=True, mode=0o755)
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
                return existing, ""
            existing.delete()
        active = InputStagingLease.objects.all()
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
    unique_references: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    missing_bytes = 0
    for item in items:
        reference = _manifest_reference(item)
        staging_path = str(item["staging_relative_path"])
        if staging_path in unique_references:
            continue
        unique_references[staging_path] = (item, reference)
        if not os.path.lexists(_target_path(reference)):
            missing_bytes += reference["size"]
    lease = _claim_staging_lease(
        run,
        reserved_bytes=max(1, missing_bytes),
        checkpoint=checkpoint,
    )
    run_deadline = time.monotonic() + float(
        settings.ANALYSIS_OBJECT_STAGE_RUN_TIMEOUT_SECONDS
    )
    try:
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
                _download_to_staging(item, checkpoint=checkpoint)
    finally:
        InputStagingLease.objects.filter(
            pk=lease.pk,
            run=run,
            worker_lease_token=run.lease_token,
        ).delete()
    return len(items)
