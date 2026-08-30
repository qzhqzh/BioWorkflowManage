#!/usr/bin/env python3
"""Dependency-free installer and diagnostics CLI for an Analysis Node bundle."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterable
from urllib.parse import urlparse
from uuid import uuid4


REQUIRED_IMAGE_ROLES = (
    "backend",
    "frontend",
    "postgres",
    "gateway",
    "dind",
    "smoke-task",
)
IMAGE_ENV_KEYS = {
    "backend": "ANALYSIS_NODE_BACKEND_IMAGE",
    "frontend": "ANALYSIS_NODE_FRONTEND_IMAGE",
    "postgres": "ANALYSIS_NODE_POSTGRES_IMAGE",
    "gateway": "ANALYSIS_NODE_GATEWAY_IMAGE",
    "dind": "ANALYSIS_NODE_DIND_IMAGE",
    "smoke-task": "ANALYSIS_NODE_SMOKE_TASK_IMAGE",
}
REQUIRED_CONFIG_KEYS = (
    "ANALYSIS_NODE_VERSION",
    "ANALYSIS_NODE_MODE",
    "ANALYSIS_NODE_RUNTIME",
    "ANALYSIS_NODE_BIND_ADDRESS",
    "ANALYSIS_NODE_API_PORT",
    "ANALYSIS_NODE_CONSOLE_PORT",
    "ANALYSIS_NODE_PUBLIC_BASE_URL",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "DJANGO_SECRET_KEY",
    "WEBHOOK_SIGNING_KEY",
    "ANALYSIS_NODE_DATA_ROOT",
    "ANALYSIS_NODE_BACKUP_ROOT",
    "ANALYSIS_NODE_POSTGRES_PATH",
    "ANALYSIS_NODE_DIND_PATH",
    "ANALYSIS_NODE_DIND_CERT_PATH",
    "ANALYSIS_WORKSPACE_HOST_PATH",
    "ANALYSIS_RAWDATA_HOST_PATH",
    "ANALYSIS_DATABASE_HOST_PATH",
    "ANALYSIS_RUN_HOST_PATH",
    "ANALYSIS_INPUT_STAGING_HOST_PATH",
    "ANALYSIS_CACHE_HOST_PATH",
    "ANALYSIS_ARTIFACT_EXPORT_HOST_PATH",
    "ANALYSIS_OBJECT_STORAGE_SECRETS_HOST_PATH",
    "ANALYSIS_ARTIFACT_EXPORT_SECRETS_HOST_PATH",
    "ANALYSIS_RAWDATA_EXECUTION_ROOT",
    "ANALYSIS_DATABASE_EXECUTION_ROOT",
    "ANALYSIS_RUN_EXECUTION_ROOT",
    "ANALYSIS_INPUT_STAGING_EXECUTION_ROOT",
    "MINIWDL_UID",
    "MINIWDL_GID",
    "MINIWDL_DOCKER_GID",
    "MINIWDL_CONTROL_SUBNET",
    "MINIWDL_EGRESS_SUBNET",
)
PATH_KEYS = tuple(key for key in REQUIRED_CONFIG_KEYS if key.endswith(("_ROOT", "_PATH")))
DIRECTORY_MODES = {
    "ANALYSIS_NODE_DATA_ROOT": 0o750,
    "ANALYSIS_NODE_BACKUP_ROOT": 0o700,
    "ANALYSIS_NODE_POSTGRES_PATH": 0o700,
    "ANALYSIS_NODE_DIND_PATH": 0o700,
    "ANALYSIS_NODE_DIND_CERT_PATH": 0o700,
    "ANALYSIS_WORKSPACE_HOST_PATH": 0o750,
    "ANALYSIS_RAWDATA_HOST_PATH": 0o750,
    "ANALYSIS_DATABASE_HOST_PATH": 0o750,
    "ANALYSIS_RUN_HOST_PATH": 0o750,
    "ANALYSIS_INPUT_STAGING_HOST_PATH": 0o750,
    "ANALYSIS_CACHE_HOST_PATH": 0o750,
    "ANALYSIS_ARTIFACT_EXPORT_HOST_PATH": 0o750,
    "ANALYSIS_OBJECT_STORAGE_SECRETS_HOST_PATH": 0o700,
    "ANALYSIS_ARTIFACT_EXPORT_SECRETS_HOST_PATH": 0o700,
}
EXECUTION_ROOTS = {
    "ANALYSIS_RAWDATA_EXECUTION_ROOT": (
        "ANALYSIS_RAWDATA_HOST_PATH",
        "/analysis/rawdata",
    ),
    "ANALYSIS_DATABASE_EXECUTION_ROOT": (
        "ANALYSIS_DATABASE_HOST_PATH",
        "/analysis/databases",
    ),
    "ANALYSIS_RUN_EXECUTION_ROOT": (
        "ANALYSIS_RUN_HOST_PATH",
        "/analysis/runs",
    ),
    "ANALYSIS_INPUT_STAGING_EXECUTION_ROOT": (
        "ANALYSIS_INPUT_STAGING_HOST_PATH",
        "/analysis/input-staging",
    ),
}
SECRET_KEYS = ("POSTGRES_PASSWORD", "DJANGO_SECRET_KEY", "WEBHOOK_SIGNING_KEY")
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$")
DIGEST_REF_PATTERN = re.compile(r"@sha256:[0-9a-f]{64}$")
IMAGE_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
FINAL_STATUSES = frozenset({"succeeded", "failed", "canceled"})
SIGSTORE_IDENTITY_REGEXP = (
    r"^https://github\.com/qzhqzh/BioWorkflowManage/\.github/workflows/"
    r"release-analysis-node\.yml@refs/(tags/analysis-node-v[0-9].*|heads/.+)$"
)
SIGSTORE_OIDC_ISSUER = "https://token.actions.githubusercontent.com"


class AnalysisNodeError(RuntimeError):
    """Actionable user or environment error."""


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    message: str


def _check(name: str, ok: bool, success: str, failure: str) -> Check:
    return Check(name, "pass" if ok else "fail", success if ok else failure)


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise AnalysisNodeError(f"配置文件不存在：{path}")
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise AnalysisNodeError(f"{path}:{line_number} 必须使用 KEY=VALUE。")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise AnalysisNodeError(f"{path}:{line_number} 包含非法变量名。")
        if key in values:
            raise AnalysisNodeError(f"{path}:{line_number} 重复定义 {key}。")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def _integer(values: dict[str, str], key: str, default: int) -> int:
    raw = values.get(key, str(default))
    try:
        return int(raw)
    except ValueError as error:
        raise AnalysisNodeError(f"{key} 必须是整数。") from error


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def validate_environment(values: dict[str, str]) -> list[Check]:
    checks: list[Check] = []
    missing = sorted(key for key in REQUIRED_CONFIG_KEYS if not values.get(key, "").strip())
    checks.append(
        _check(
            "required-config",
            not missing,
            "必填配置完整。",
            f"缺少配置：{', '.join(missing)}",
        )
    )
    if missing:
        return checks

    version = values["ANALYSIS_NODE_VERSION"]
    checks.append(
        _check(
            "version",
            bool(VERSION_PATTERN.fullmatch(version)),
            f"版本格式有效：{version}",
            "ANALYSIS_NODE_VERSION 必须是语义化版本。",
        )
    )
    mode = values["ANALYSIS_NODE_MODE"]
    runtime = values["ANALYSIS_NODE_RUNTIME"]
    checks.append(_check("mode", mode in {"headless", "console"}, f"界面模式：{mode}", "界面模式只能是 headless 或 console。"))
    checks.append(_check("runtime", runtime in {"isolated", "host"}, f"运行时：{runtime}", "运行时只能是 isolated 或 host。"))
    checks.append(
        _check(
            "product-only",
            values.get("INTEGRATION_REQUIRE_ANALYSIS_PRODUCT") == "1",
            "只允许已发布 Analysis Product。",
            "交付部署必须设置 INTEGRATION_REQUIRE_ANALYSIS_PRODUCT=1。",
        )
    )

    password_ok = len(values["POSTGRES_PASSWORD"]) >= 16 and values["POSTGRES_PASSWORD"].casefold() not in {
        "password",
        "changeme",
        "bioworkflow",
    }
    checks.append(_check("postgres-secret", password_ok, "数据库口令长度符合基线。", "POSTGRES_PASSWORD 至少 16 字符且不能使用默认值。"))
    checks.append(_check("django-secret", len(values["DJANGO_SECRET_KEY"]) >= 32, "Django 密钥长度符合基线。", "DJANGO_SECRET_KEY 至少 32 字符。"))
    checks.append(_check("webhook-secret", len(values["WEBHOOK_SIGNING_KEY"]) >= 32, "Webhook 签名密钥长度符合基线。", "WEBHOOK_SIGNING_KEY 至少 32 字符。"))

    try:
        ipaddress.ip_address(values["ANALYSIS_NODE_BIND_ADDRESS"])
        bind_valid = True
    except ValueError:
        bind_valid = False
    checks.append(_check("bind-address", bind_valid, "监听地址有效。", "ANALYSIS_NODE_BIND_ADDRESS 必须是 IPv4 或 IPv6 地址。"))
    for key in ("ANALYSIS_NODE_API_PORT", "ANALYSIS_NODE_CONSOLE_PORT"):
        try:
            port = _integer(values, key, 8082)
            valid = 1 <= port <= 65535
        except AnalysisNodeError:
            valid = False
        checks.append(_check(key.casefold(), valid, f"{key} 有效。", f"{key} 必须在 1-65535。"))

    public_url = urlparse(values.get("ANALYSIS_NODE_PUBLIC_BASE_URL", ""))
    url_valid = public_url.scheme == "https" and bool(public_url.netloc) and not public_url.username
    checks.append(_check("public-url", url_valid, "公开 URL 使用 HTTPS。", "ANALYSIS_NODE_PUBLIC_BASE_URL 必须是无内嵌凭据的 HTTPS URL。"))

    resolved_paths: dict[str, Path] = {}
    for key in PATH_KEYS:
        path = Path(values[key])
        if path.is_absolute():
            resolved_paths[key] = path.resolve(strict=False)
    checks.append(
        _check(
            "absolute-paths",
            len(resolved_paths) == len(PATH_KEYS),
            "所有数据、密钥与执行路径均为绝对路径。",
            "所有 *_ROOT 与 *_PATH 配置必须是绝对路径。",
        )
    )
    if len(resolved_paths) == len(PATH_KEYS):
        data_root = resolved_paths["ANALYSIS_NODE_DATA_ROOT"]
        backup_root = resolved_paths["ANALYSIS_NODE_BACKUP_ROOT"]
        separated = not _is_relative_to(data_root, backup_root) and not _is_relative_to(backup_root, data_root)
        checks.append(_check("backup-separation", separated, "备份目录与数据目录相互隔离。", "备份目录不能位于数据目录内，数据目录也不能位于备份目录内。"))
        secret_roots = (
            resolved_paths["ANALYSIS_OBJECT_STORAGE_SECRETS_HOST_PATH"],
            resolved_paths["ANALYSIS_ARTIFACT_EXPORT_SECRETS_HOST_PATH"],
        )
        checks.append(_check("secret-separation", all(not _is_relative_to(item, data_root) for item in secret_roots), "密钥目录不在数据根目录内。", "密钥目录不能放入可备份或导出的数据根目录。"))
        expected_workspace_children = (
            "ANALYSIS_RUN_HOST_PATH",
            "ANALYSIS_INPUT_STAGING_HOST_PATH",
            "ANALYSIS_CACHE_HOST_PATH",
            "ANALYSIS_ARTIFACT_EXPORT_HOST_PATH",
        )
        workspace = resolved_paths["ANALYSIS_WORKSPACE_HOST_PATH"]
        checks.append(_check("workspace-layout", all(_is_relative_to(resolved_paths[key], workspace) for key in expected_workspace_children), "运行、暂存、缓存与导出目录位于工作区内。", "运行、暂存、缓存与导出目录必须位于 ANALYSIS_WORKSPACE_HOST_PATH 内。"))

        roots_valid = True
        for execution_key, (host_key, isolated_path) in EXECUTION_ROOTS.items():
            expected = resolved_paths[host_key] if runtime == "host" else Path(isolated_path)
            roots_valid = roots_valid and resolved_paths[execution_key] == expected
        message = "执行路径与所选运行时一致。"
        failure = "host 运行时要求执行路径等于主机路径；isolated 运行时要求使用固定 /analysis 路径。"
        checks.append(_check("execution-paths", roots_valid, message, failure))

    try:
        control = ipaddress.ip_network(values.get("MINIWDL_CONTROL_SUBNET", ""))
        egress = ipaddress.ip_network(values.get("MINIWDL_EGRESS_SUBNET", ""))
        subnet_ok = control.version == 4 and egress.version == 4 and not control.overlaps(egress)
    except ValueError:
        subnet_ok = False
    checks.append(_check("runtime-subnets", subnet_ok, "隔离运行时子网有效且不重叠。", "MINIWDL_CONTROL_SUBNET 与 MINIWDL_EGRESS_SUBNET 必须是不同的 IPv4 CIDR。"))
    return checks


def _failed(checks: Iterable[Check]) -> bool:
    return any(item.status == "fail" for item in checks)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_image_lock(package_dir: Path, values: dict[str, str], image_values: dict[str, str]) -> list[Check]:
    path = package_dir / "images.lock.json"
    if not path.is_file():
        return [Check("image-lock", "fail", f"镜像锁不存在：{path}")]
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [Check("image-lock", "fail", f"镜像锁无法读取：{error}")]
    images = document.get("images") if isinstance(document, dict) else None
    valid = (
        document.get("schema_version") == 1
        and document.get("product_version") == values.get("ANALYSIS_NODE_VERSION")
        and document.get("platform") == "linux/amd64"
        and isinstance(images, dict)
        and set(images) == set(REQUIRED_IMAGE_ROLES)
    )
    if valid:
        for role in REQUIRED_IMAGE_ROLES:
            item = images.get(role)
            valid = valid and isinstance(item, dict)
            if not isinstance(item, dict):
                continue
            valid = valid and item.get("local_ref") == image_values.get(IMAGE_ENV_KEYS[role])
            valid = valid and bool(DIGEST_REF_PATTERN.search(str(item.get("source_ref") or "")))
            valid = valid and bool(IMAGE_ID_PATTERN.fullmatch(str(item.get("image_id") or "")))
    return [_check("image-lock", bool(valid), "镜像锁完整、版本一致且全部固定 digest。", "镜像锁格式、版本、角色或 digest 不符合交付契约。")]


def verify_checksums(package_dir: Path, *, required: bool = True) -> list[Check]:
    manifest_path = package_dir / "SHA256SUMS"
    if not manifest_path.is_file():
        status = "fail" if required else "warn"
        return [Check("checksums", status, "源码目录没有 SHA256SUMS；正式交付包必须包含。")]
    checks: list[Check] = []
    seen: set[str] = set()
    for line_number, line in enumerate(manifest_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\r\n]+)", line)
        if match is None:
            checks.append(Check("checksums", "fail", f"SHA256SUMS 第 {line_number} 行格式无效。"))
            continue
        expected, relative = match.groups()
        relative_path = Path(relative)
        candidate = (package_dir / relative_path).resolve(strict=False)
        if relative_path.is_absolute() or not _is_relative_to(candidate, package_dir.resolve()) or relative in seen:
            checks.append(Check("checksums", "fail", f"SHA256SUMS 包含不安全或重复路径：{relative}"))
            continue
        seen.add(relative)
        if not candidate.is_file() or candidate.is_symlink():
            checks.append(Check("checksums", "fail", f"校验文件缺失或为符号链接：{relative}"))
            continue
        actual = _sha256(candidate)
        checks.append(_check(f"sha256:{relative}", actual == expected, f"校验通过：{relative}", f"文件摘要不匹配：{relative}"))
    if not seen:
        checks.append(Check("checksums", "fail", "SHA256SUMS 不能为空。"))
    return checks


def verify_package_signature(package_dir: Path, *, required: bool = True) -> list[Check]:
    payload = package_dir / "SHA256SUMS"
    bundle = package_dir / "SHA256SUMS.sigstore.json"
    if not payload.is_file() or not bundle.is_file():
        status = "fail" if required else "warn"
        return [
            Check(
                "package-signature",
                status,
                "源码目录没有 Sigstore 签名；正式交付包必须包含。",
            )
        ]
    if shutil.which("cosign") is None:
        return [
            Check(
                "package-signature",
                "fail",
                "缺少 cosign，无法验证交付包发布者身份。",
            )
        ]
    try:
        _run(
            [
                "cosign",
                "verify-blob",
                "--offline",
                "--bundle",
                str(bundle),
                "--certificate-identity-regexp",
                SIGSTORE_IDENTITY_REGEXP,
                "--certificate-oidc-issuer",
                SIGSTORE_OIDC_ISSUER,
                str(payload),
            ],
            capture=True,
        )
    except AnalysisNodeError as error:
        return [Check("package-signature", "fail", str(error))]
    return [
        Check(
            "package-signature",
            "pass",
            "Sigstore 签名有效，发布者工作流身份匹配。",
        )
    ]


def verify_bundle(context: "Context", *, required: bool) -> list[Check]:
    checks = verify_checksums(context.package_dir, required=required)
    checks.extend(verify_package_signature(context.package_dir, required=required))
    lock_path = context.package_dir / "images.lock.json"
    if not lock_path.is_file() and not required:
        checks.append(
            Check(
                "image-lock",
                "warn",
                "源码目录尚未生成 images.lock.json；正式交付包必须包含。",
            )
        )
    else:
        checks.extend(
            validate_image_lock(
                context.package_dir,
                context.values,
                context.image_values,
            )
        )
    return checks


def _run(command: list[str], *, environment: dict[str, str] | None = None, capture: bool = False, stdout: BinaryIO | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            env=environment,
            check=True,
            text=stdout is None,
            capture_output=capture,
            stdout=stdout,
        )
    except FileNotFoundError as error:
        raise AnalysisNodeError(f"缺少命令：{command[0]}") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip() if capture else ""
        suffix = f"：{detail}" if detail else ""
        raise AnalysisNodeError(f"命令执行失败（exit {error.returncode}）：{' '.join(command)}{suffix}") from error


def _compose_command(context: "Context", *arguments: str) -> list[str]:
    mode_profile = context.values["ANALYSIS_NODE_MODE"]
    runtime_profile = f"{context.values['ANALYSIS_NODE_RUNTIME']}-runtime"
    return [
        "docker",
        "compose",
        "--project-directory",
        str(context.package_dir),
        "--env-file",
        str(context.env_file),
        "--env-file",
        str(context.images_env),
        "--file",
        str(context.package_dir / "compose.yml"),
        "--profile",
        mode_profile,
        "--profile",
        runtime_profile,
        *arguments,
    ]


@dataclass
class Context:
    package_dir: Path
    env_file: Path
    images_env: Path
    values: dict[str, str]
    image_values: dict[str, str]

    @property
    def environment(self) -> dict[str, str]:
        result = {**os.environ, **self.values, **self.image_values}
        result["ANALYSIS_NODE_ENV_FILE"] = str(self.env_file)
        return result


def build_context(args: argparse.Namespace) -> Context:
    package_dir = Path(args.package_dir).resolve()
    env_file = Path(args.env_file).resolve() if args.env_file else package_dir / ".env"
    images_env = Path(args.images_env).resolve() if args.images_env else package_dir / "images.env"
    return Context(
        package_dir=package_dir,
        env_file=env_file,
        images_env=images_env,
        values=parse_env_file(env_file),
        image_values=parse_env_file(images_env),
    )


def initialize_directories(context: Context) -> list[Check]:
    checks = validate_environment(context.values)
    if _failed(checks):
        raise AnalysisNodeError("配置校验未通过，未创建目录。")
    uid = _integer(context.values, "MINIWDL_UID", 1000)
    gid = _integer(context.values, "MINIWDL_GID", 1000)
    if uid < 1 or gid < 1:
        raise AnalysisNodeError("MINIWDL_UID 与 MINIWDL_GID 必须是正整数。")
    if os.geteuid() != 0 and (uid != os.geteuid() or gid != os.getegid()):
        raise AnalysisNodeError(
            "当前用户无法把新目录交给配置的 MINIWDL_UID:MINIWDL_GID；"
            "请使用管理员执行 init，或改成当前用户 UID/GID。"
        )
    worker_owned = {
        "ANALYSIS_WORKSPACE_HOST_PATH",
        "ANALYSIS_RAWDATA_HOST_PATH",
        "ANALYSIS_DATABASE_HOST_PATH",
        "ANALYSIS_RUN_HOST_PATH",
        "ANALYSIS_INPUT_STAGING_HOST_PATH",
        "ANALYSIS_CACHE_HOST_PATH",
        "ANALYSIS_ARTIFACT_EXPORT_HOST_PATH",
        "ANALYSIS_OBJECT_STORAGE_SECRETS_HOST_PATH",
        "ANALYSIS_ARTIFACT_EXPORT_SECRETS_HOST_PATH",
    }
    for key, mode in DIRECTORY_MODES.items():
        path = Path(context.values[key])
        existed = path.exists()
        if existed and not path.is_dir():
            raise AnalysisNodeError(f"配置路径不是目录：{key}={path}")
        path.mkdir(parents=True, exist_ok=True)
        if not existed:
            path.chmod(mode)
            if key in worker_owned and os.geteuid() == 0:
                os.chown(path, uid, gid)
    marker = Path(context.values["ANALYSIS_NODE_DATA_ROOT"]) / ".analysis-node"
    if marker.is_symlink() or (marker.exists() and not marker.is_file()):
        raise AnalysisNodeError(f"初始化标记不是普通文件：{marker}")
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "product_version": context.values["ANALYSIS_NODE_VERSION"],
                "initialized_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    marker.chmod(0o600)
    return [Check("directories", "pass", "数据、备份、工作区和密钥目录已安全初始化；未删除现有内容。")]


def _version_tuple(value: str) -> tuple[int, int, int]:
    numbers = re.findall(r"\d+", value)
    return tuple((int(item) for item in (numbers + ["0", "0", "0"])[:3]))  # type: ignore[return-value]


def preflight(context: Context, *, require_bundle: bool) -> list[Check]:
    checks = validate_environment(context.values)
    checks.extend(verify_bundle(context, required=require_bundle))
    checks.append(_check("platform-os", sys.platform.startswith("linux"), "操作系统为 Linux。", "Analysis Node 1.x 仅支持 Linux。"))
    machine = platform.machine().casefold()
    checks.append(_check("platform-arch", machine in {"x86_64", "amd64"}, f"CPU 架构：{machine}", "Analysis Node 1.x 离线包仅支持 amd64。"))
    if shutil.which("docker") is None:
        checks.append(Check("docker", "fail", "未安装 Docker CLI。"))
        return checks
    try:
        docker = _run(["docker", "version", "--format", "{{.Server.Version}}"], capture=True)
        checks.append(Check("docker", "pass", f"Docker Engine：{docker.stdout.strip()}"))
    except AnalysisNodeError as error:
        checks.append(Check("docker", "fail", str(error)))
        return checks
    try:
        compose = _run(["docker", "compose", "version", "--short"], capture=True)
        compose_version = compose.stdout.strip()
        checks.append(_check("docker-compose", _version_tuple(compose_version) >= (2, 27, 0), f"Docker Compose：{compose_version}", "Docker Compose 必须不低于 2.27.0。"))
    except AnalysisNodeError as error:
        checks.append(Check("docker-compose", "fail", str(error)))

    if context.values["ANALYSIS_NODE_RUNTIME"] == "host":
        socket_path = Path("/var/run/docker.sock")
        checks.append(_check("host-docker-socket", socket_path.is_socket(), "主机 Docker Socket 可见。", "host runtime 需要 /var/run/docker.sock。"))
    selected_port_key = "ANALYSIS_NODE_API_PORT" if context.values["ANALYSIS_NODE_MODE"] == "headless" else "ANALYSIS_NODE_CONSOLE_PORT"
    bind_address = context.values["ANALYSIS_NODE_BIND_ADDRESS"]
    port = _integer(context.values, selected_port_key, 8082)
    family = socket.AF_INET6 if ":" in bind_address else socket.AF_INET
    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        probe.bind((bind_address, port))
    except OSError as error:
        checks.append(Check("listen-port", "fail", f"监听地址 {bind_address}:{port} 不可用：{error}"))
    else:
        checks.append(Check("listen-port", "pass", f"监听地址 {bind_address}:{port} 可用。"))
    finally:
        probe.close()

    data_root = Path(context.values["ANALYSIS_NODE_DATA_ROOT"])
    existing = data_root
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    try:
        free_gb = shutil.disk_usage(existing).free / 1024**3
        minimum = float(context.values.get("ANALYSIS_NODE_MIN_FREE_GB", "50"))
        checks.append(_check("disk-space", free_gb >= minimum, f"数据盘可用 {free_gb:.1f} GiB。", f"数据盘仅剩 {free_gb:.1f} GiB，低于 {minimum:.1f} GiB。"))
    except (OSError, ValueError) as error:
        checks.append(Check("disk-space", "fail", f"无法检查磁盘空间：{error}"))
    try:
        _run(_compose_command(context, "config", "--quiet"), environment=context.environment, capture=True)
        checks.append(Check("compose-config", "pass", "产品 Compose 可解析。"))
    except AnalysisNodeError as error:
        checks.append(Check("compose-config", "fail", str(error)))
    return checks


def verify_loaded_images(context: Context) -> list[Check]:
    try:
        lock = json.loads(
            (context.package_dir / "images.lock.json").read_text(encoding="utf-8")
        )
        images = lock["images"]
        if set(images) != set(REQUIRED_IMAGE_ROLES):
            raise KeyError("images")
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise AnalysisNodeError("images.lock.json 无效，无法核对本地镜像。") from error
    checks: list[Check] = []
    for role in REQUIRED_IMAGE_ROLES:
        item = images[role]
        try:
            result = _run(["docker", "image", "inspect", item["local_ref"], "--format", "{{.Id}}"], capture=True)
            actual = result.stdout.strip()
            checks.append(_check(f"image:{role}", actual == item["image_id"], f"镜像已加载且 ID 匹配：{role}", f"镜像 ID 不匹配：{role}"))
        except AnalysisNodeError as error:
            checks.append(Check(f"image:{role}", "fail", str(error)))
    return checks


def _load_smoke_image_into_dind(context: Context) -> None:
    _run(_compose_command(context, "up", "-d", "--wait", "miniwdl-docker"), environment=context.environment)
    smoke_image = context.image_values["ANALYSIS_NODE_SMOKE_TASK_IMAGE"]
    save = subprocess.Popen(["docker", "image", "save", smoke_image], stdout=subprocess.PIPE)
    assert save.stdout is not None
    load_command = _compose_command(context, "exec", "-T", "miniwdl-docker", "docker", "load")
    load = subprocess.run(load_command, env=context.environment, stdin=save.stdout, capture_output=True, text=False)
    save.stdout.close()
    save_return = save.wait()
    if save_return or load.returncode:
        raise AnalysisNodeError("无法把可信 smoke task 镜像加载到隔离运行时。")


def load_images(context: Context) -> list[Check]:
    checks = validate_environment(context.values)
    checks.extend(verify_bundle(context, required=True))
    if _failed(checks):
        raise AnalysisNodeError("交付包校验失败，未加载镜像。")
    archive = context.package_dir / "images.tar"
    if not archive.is_file():
        raise AnalysisNodeError(f"离线镜像归档不存在：{archive}")
    _run(["docker", "load", "--input", str(archive)])
    image_checks = verify_loaded_images(context)
    if _failed(image_checks):
        raise AnalysisNodeError("加载后的镜像与 images.lock.json 不一致。")
    if context.values["ANALYSIS_NODE_RUNTIME"] == "isolated":
        _load_smoke_image_into_dind(context)
        image_checks.append(Check("isolated-smoke-image", "pass", "可信 smoke task 镜像已加载到隔离 Docker Engine。"))
    return image_checks


def compose_action(context: Context, action: str) -> list[Check]:
    if action != "down" and _failed(validate_environment(context.values)):
        raise AnalysisNodeError("配置校验未通过，拒绝修改服务状态。")
    if action == "up":
        _run(_compose_command(context, "up", "-d", "--wait"), environment=context.environment)
        return [Check("services", "pass", "Analysis Node 服务已启动并通过容器健康检查。")]
    if action == "down":
        _run(_compose_command(context, "down", "--remove-orphans"), environment=context.environment)
        return [Check("services", "pass", "Analysis Node 服务已停止；数据目录与卷未删除。")]
    if action == "migrate":
        _run(_compose_command(context, "run", "--rm", "backend", "python", "backend/manage.py", "migrate", "--noinput"), environment=context.environment)
        return [Check("database-migrations", "pass", "数据库迁移已完成。")]
    raise AnalysisNodeError(f"未知 Compose 操作：{action}")


def create_backup(context: Context) -> Path:
    if _failed(validate_environment(context.values)):
        raise AnalysisNodeError("配置校验未通过，拒绝创建备份。")
    backup_root = Path(context.values["ANALYSIS_NODE_BACKUP_ROOT"])
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_root / f"analysis-node-{timestamp}-{uuid4().hex[:8]}.dump"
    _run(_compose_command(context, "up", "-d", "--wait", "db"), environment=context.environment)
    command = _compose_command(
        context,
        "exec",
        "-T",
        "db",
        "pg_dump",
        "-U",
        context.values["POSTGRES_USER"],
        "-d",
        context.values["POSTGRES_DB"],
        "--format=custom",
        "--no-owner",
        "--no-privileges",
    )
    temporary = target.with_suffix(".dump.partial")
    try:
        with temporary.open("wb") as handle:
            _run(command, environment=context.environment, stdout=handle)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    digest = _sha256(target)
    manifest = target.with_suffix(".json")
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "product_version": context.values["ANALYSIS_NODE_VERSION"],
                "database": context.values["POSTGRES_DB"],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "file": target.name,
                "sha256": digest,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    target.chmod(0o600)
    manifest.chmod(0o600)
    return target


def restore_backup(context: Context, backup_path: Path, *, confirmed: bool) -> list[Check]:
    if not confirmed:
        raise AnalysisNodeError("恢复数据库必须显式传入 --confirm-database-restore。")
    backup_path = backup_path.resolve()
    backup_root = Path(context.values["ANALYSIS_NODE_BACKUP_ROOT"]).resolve()
    if not backup_path.is_file() or not _is_relative_to(backup_path, backup_root):
        raise AnalysisNodeError("只允许恢复 ANALYSIS_NODE_BACKUP_ROOT 中存在的备份。")
    manifest_path = backup_path.with_suffix(".json")
    if not manifest_path.is_file():
        raise AnalysisNodeError("备份清单不存在，拒绝恢复。")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("file") != backup_path.name or manifest.get("sha256") != _sha256(backup_path):
        raise AnalysisNodeError("备份摘要校验失败，拒绝恢复。")
    safety_backup = create_backup(context)
    _run(_compose_command(context, "stop"), environment=context.environment)
    _run(_compose_command(context, "up", "-d", "--wait", "db"), environment=context.environment)
    common = _compose_command(context, "exec", "-T", "db")
    user = context.values["POSTGRES_USER"]
    database = context.values["POSTGRES_DB"]
    _run([*common, "dropdb", "-U", user, "--force", "--if-exists", database], environment=context.environment)
    _run([*common, "createdb", "-U", user, database], environment=context.environment)
    with backup_path.open("rb") as handle:
        result = subprocess.run([*common, "pg_restore", "-U", user, "-d", database, "--no-owner", "--no-privileges"], env=context.environment, stdin=handle)
    if result.returncode:
        raise AnalysisNodeError(f"pg_restore 失败；恢复前安全备份位于 {safety_backup}")
    return [Check("database-restore", "pass", f"数据库已恢复；恢复前安全备份：{safety_backup}")]


def _http_json(url: str, *, token: str | None = None, method: str = "GET", payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:2000]
        raise AnalysisNodeError(f"HTTP {error.code} {url}：{detail}") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise AnalysisNodeError(f"请求失败 {url}：{error}") from error


def _local_api_base(values: dict[str, str]) -> str:
    override = values.get("ANALYSIS_NODE_SMOKE_BASE_URL", "").rstrip("/")
    if override:
        return override
    bind = values["ANALYSIS_NODE_BIND_ADDRESS"]
    host = "127.0.0.1" if bind in {"0.0.0.0", "::"} else bind
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    key = "ANALYSIS_NODE_API_PORT" if values["ANALYSIS_NODE_MODE"] == "headless" else "ANALYSIS_NODE_CONSOLE_PORT"
    return f"http://{host}:{_integer(values, key, 8082)}/api/v1"


def _gateway_headers(values: dict[str, str]) -> dict[str, str]:
    public_url = urlparse(values["ANALYSIS_NODE_PUBLIC_BASE_URL"])
    return {"Host": public_url.netloc}


def smoke(context: Context, *, timeout_seconds: int) -> list[Check]:
    if _failed(validate_environment(context.values)):
        raise AnalysisNodeError("配置校验未通过，拒绝执行冒烟任务。")

    def compose(*arguments: str) -> list[str]:
        return _compose_command(context, *arguments)

    _run(compose("exec", "-T", "backend", "python", "backend/manage.py", "prepare_analysis_node_smoke"), environment=context.environment, capture=True)
    issued = _run(
        compose(
            "exec",
            "-T",
            "backend",
            "python",
            "backend/manage.py",
            "manage_service_account",
            "--client-id",
            "analysis-node-installer",
            "--name",
            "Analysis Node installer",
            "--scope",
            "analysis:submit",
            "--scope",
            "analysis:read",
            "--issue-token",
            "--token-name",
            "installation-smoke",
            "--expires-days",
            "1",
        ),
        environment=context.environment,
        capture=True,
    )
    token_match = re.search(r"^TOKEN=(.+)$", issued.stdout, re.MULTILINE)
    prefix_match = re.search(r"^TOKEN_PREFIX=(.+)$", issued.stdout, re.MULTILINE)
    if token_match is None or prefix_match is None:
        raise AnalysisNodeError("无法签发临时冒烟 Service Token。")
    token = token_match.group(1).strip()
    prefix = prefix_match.group(1).strip()
    external_id = f"smoke-{uuid4().hex}"
    base = _local_api_base(context.values)
    gateway_headers = _gateway_headers(context.values)
    payload = {
        "external_ref": {
            "client_id": "analysis-node-installer",
            "external_run_id": external_id,
            "external_analysis_id": external_id,
        },
        "analysis_product": {
            "analysis_code": "analysis-node-smoke",
            "contract_version": "1.0.0",
        },
        "subject": {"sample_id": "analysis-node-smoke"},
        "inputs": {},
        "metadata": {"purpose": "installation-smoke"},
    }
    try:
        preflight_result = _http_json(
            f"{base}/integration/analysis-runs/preflight",
            token=token,
            method="POST",
            payload=payload,
            headers=gateway_headers,
        )
        if not preflight_result.get("ready"):
            raise AnalysisNodeError(f"API 冒烟预检未就绪：{preflight_result.get('checks')}")
        created = _http_json(
            f"{base}/integration/analysis-runs",
            token=token,
            method="POST",
            payload=payload,
            headers={**gateway_headers, "Idempotency-Key": external_id},
        )
        run_id = str(created.get("id") or "")
        if not run_id:
            raise AnalysisNodeError("API 冒烟提交没有返回 run id。")
        deadline = time.monotonic() + timeout_seconds
        current = created
        while current.get("status") not in FINAL_STATUSES and time.monotonic() < deadline:
            time.sleep(2)
            current = _http_json(
                f"{base}/integration/analysis-runs/{run_id}",
                token=token,
                headers=gateway_headers,
            )
        if current.get("status") != "succeeded" or current.get("output_status") != "complete":
            raise AnalysisNodeError(f"API 冒烟未成功：status={current.get('status')} output_status={current.get('output_status')} error={current.get('error')}")
        return [Check("headless-api-smoke", "pass", f"Analysis Product 经 API 提交并完成：{run_id}")]
    finally:
        _run(
            compose(
                "exec",
                "-T",
                "backend",
                "python",
                "backend/manage.py",
                "manage_service_account",
                "--client-id",
                "analysis-node-installer",
                "--revoke-prefix",
                prefix,
            ),
            environment=context.environment,
            capture=True,
        )


def doctor(context: Context, *, run_smoke: bool, timeout_seconds: int) -> list[Check]:
    checks = validate_environment(context.values)
    checks.extend(verify_bundle(context, required=True))
    try:
        checks.extend(verify_loaded_images(context))
    except AnalysisNodeError as error:
        checks.append(Check("loaded-images", "fail", str(error)))
    expected = {"db", "backend", "rawdata-indexer", "webhook-dispatcher", "artifact-exporter"}
    if context.values["ANALYSIS_NODE_MODE"] == "headless":
        expected.add("gateway-headless")
    else:
        expected.update({"gateway-console", "frontend"})
    if context.values["ANALYSIS_NODE_RUNTIME"] == "isolated":
        expected.update({"miniwdl-docker", "analysis-worker-isolated"})
    else:
        expected.add("analysis-worker-host")
    try:
        result = _run(_compose_command(context, "ps", "--status", "running", "--services"), environment=context.environment, capture=True)
        running = set(result.stdout.split())
        missing = sorted(expected - running)
        checks.append(_check("services-running", not missing, "所选 profile 的服务均在运行。", f"以下服务未运行：{', '.join(missing)}"))
        all_result = _run(_compose_command(context, "ps", "--all", "--services"), environment=context.environment, capture=True)
        all_services = set(all_result.stdout.split())
        if context.values["ANALYSIS_NODE_MODE"] == "headless":
            checks.append(_check("headless-no-frontend", "frontend" not in all_services, "Headless 部署未创建前端容器。", "Headless 部署不应创建前端容器。"))
    except AnalysisNodeError as error:
        checks.append(Check("services-running", "fail", str(error)))
    try:
        ready = _http_json(
            f"{_local_api_base(context.values)}/ready",
            headers=_gateway_headers(context.values),
        )
        checks.append(_check("api-ready", ready.get("status") == "ready", "API readiness 正常。", f"API readiness 异常：{ready}"))
    except AnalysisNodeError as error:
        checks.append(Check("api-ready", "fail", str(error)))
    if context.values["ANALYSIS_NODE_RUNTIME"] == "isolated":
        try:
            _run(_compose_command(context, "exec", "-T", "miniwdl-docker", "docker", "info"), environment=context.environment, capture=True)
            _run(_compose_command(context, "exec", "-T", "miniwdl-docker", "docker", "image", "inspect", context.image_values["ANALYSIS_NODE_SMOKE_TASK_IMAGE"]), environment=context.environment, capture=True)
            checks.append(Check("isolated-runtime", "pass", "隔离 Docker Engine 与 smoke task 镜像正常。"))
        except AnalysisNodeError as error:
            checks.append(Check("isolated-runtime", "fail", str(error)))
    if run_smoke and not _failed(checks):
        checks.extend(smoke(context, timeout_seconds=timeout_seconds))
    return checks


def _print_checks(checks: list[Check], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"ok": not _failed(checks), "checks": [asdict(item) for item in checks]}, ensure_ascii=False, indent=2))
        return
    symbols = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}
    for item in checks:
        print(f"[{symbols[item.status]}] {item.name}: {item.message}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="analysis-node", description="Install and diagnose a BioWorkflowManage Analysis Node.")
    parser.add_argument("--package-dir", required=True)
    parser.add_argument("--env-file")
    parser.add_argument("--images-env")
    parser.add_argument("--json", action="store_true", dest="json_output")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-config")
    verify = commands.add_parser("verify-bundle")
    verify.add_argument("--allow-source-tree", action="store_true")
    commands.add_parser("init")
    preflight_parser = commands.add_parser("preflight")
    preflight_parser.add_argument("--allow-source-tree", action="store_true")
    commands.add_parser("load-images")
    commands.add_parser("migrate")
    commands.add_parser("up")
    commands.add_parser("down")
    commands.add_parser("backup")
    restore = commands.add_parser("restore")
    restore.add_argument("backup")
    restore.add_argument("--confirm-database-restore", action="store_true")
    smoke_parser = commands.add_parser("smoke")
    smoke_parser.add_argument("--timeout", type=int, default=300)
    doctor_parser = commands.add_parser("doctor")
    doctor_parser.add_argument("--smoke", action="store_true")
    doctor_parser.add_argument("--timeout", type=int, default=300)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        context = build_context(args)
        if args.command == "validate-config":
            checks = validate_environment(context.values)
        elif args.command == "verify-bundle":
            checks = verify_bundle(context, required=not args.allow_source_tree)
        elif args.command == "init":
            checks = initialize_directories(context)
        elif args.command == "preflight":
            checks = preflight(context, require_bundle=not args.allow_source_tree)
        elif args.command == "load-images":
            checks = load_images(context)
        elif args.command in {"migrate", "up", "down"}:
            checks = compose_action(context, args.command)
        elif args.command == "backup":
            path = create_backup(context)
            checks = [Check("database-backup", "pass", f"备份已创建：{path}")]
        elif args.command == "restore":
            checks = restore_backup(context, Path(args.backup), confirmed=args.confirm_database_restore)
        elif args.command == "smoke":
            checks = smoke(context, timeout_seconds=max(1, args.timeout))
        elif args.command == "doctor":
            checks = doctor(context, run_smoke=args.smoke, timeout_seconds=max(1, args.timeout))
        else:
            raise AnalysisNodeError(f"未知命令：{args.command}")
        _print_checks(checks, json_output=args.json_output)
        return 1 if _failed(checks) else 0
    except KeyboardInterrupt:
        if not args.json_output:
            print("ERROR: 操作已中止；数据目录未删除。", file=sys.stderr)
        return 130
    except (AnalysisNodeError, OSError, ValueError, json.JSONDecodeError) as error:
        if args.json_output:
            print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        else:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
