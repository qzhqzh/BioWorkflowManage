#!/usr/bin/env python3
"""Build a versioned, offline Analysis Node delivery bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SOURCE = PROJECT_ROOT / "deploy" / "analysis-node"
RELEASE_IMAGES = PACKAGE_SOURCE / "release-images.json"
REQUIRED_ROLES = (
    "backend",
    "frontend",
    "postgres",
    "gateway",
    "dind",
    "smoke-task",
)
LOCAL_REPOSITORIES = {
    role: f"bioworkflowmanage/{role}" for role in REQUIRED_ROLES
}
DIGEST_REFERENCE = re.compile(r"^\S+@sha256:[0-9a-f]{64}$")
IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$")


class BundleError(RuntimeError):
    pass


def _run(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            text=True,
            capture_output=capture,
        )
    except FileNotFoundError as error:
        raise BundleError(f"缺少构建命令：{command[0]}") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        suffix = f"：{detail}" if detail else ""
        raise BundleError(
            f"命令执行失败（exit {error.returncode}）：{' '.join(command)}{suffix}"
        ) from error


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BundleError(f"无法读取 JSON：{path}：{error}") from error


def validate_sources(
    version: str,
    *,
    backend_ref: str,
    frontend_ref: str,
    release_document: dict[str, Any],
) -> dict[str, str]:
    if not VERSION.fullmatch(version):
        raise BundleError("version 必须是语义化版本。")
    third_party = release_document.get("third_party_images")
    application_repositories = release_document.get("application_repositories")
    if release_document.get("schema_version") != 1 or not isinstance(
        third_party, dict
    ) or not isinstance(application_repositories, dict):
        raise BundleError("release-images.json 格式无效。")
    sources = {
        "backend": backend_ref,
        "frontend": frontend_ref,
        **{role: str(third_party.get(role) or "") for role in REQUIRED_ROLES[2:]},
    }
    invalid = [
        role for role, reference in sources.items() if not DIGEST_REFERENCE.fullmatch(reference)
    ]
    if invalid:
        raise BundleError(
            "所有交付镜像必须固定 digest；无效角色：" + ", ".join(invalid)
        )
    for role in ("backend", "frontend"):
        repository = str(application_repositories.get(role) or "")
        if not repository or not sources[role].startswith(f"{repository}@sha256:"):
            raise BundleError(f"{role} 镜像必须来自固定仓库 {repository}。")
    return sources


def render_images_env(version: str) -> str:
    keys = {
        "backend": "ANALYSIS_NODE_BACKEND_IMAGE",
        "frontend": "ANALYSIS_NODE_FRONTEND_IMAGE",
        "postgres": "ANALYSIS_NODE_POSTGRES_IMAGE",
        "gateway": "ANALYSIS_NODE_GATEWAY_IMAGE",
        "dind": "ANALYSIS_NODE_DIND_IMAGE",
        "smoke-task": "ANALYSIS_NODE_SMOKE_TASK_IMAGE",
    }
    return "".join(
        f"{keys[role]}={LOCAL_REPOSITORIES[role]}:{version}\n"
        for role in REQUIRED_ROLES
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checksum_manifest(package_dir: Path) -> str:
    excluded_names = {
        "SHA256SUMS",
        "SHA256SUMS.sigstore.json",
    }
    paths = sorted(
        path
        for path in package_dir.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.name not in excluded_names
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
    )
    return "".join(
        f"{_sha256(path)}  {path.relative_to(package_dir).as_posix()}\n"
        for path in paths
    )


def _copy_package(target: Path) -> None:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in {"__pycache__", "SHA256SUMS", "SHA256SUMS.sigstore.json"}
            or name.endswith(".pyc")
        }

    shutil.copytree(PACKAGE_SOURCE, target, ignore=ignore)
    shutil.copy2(PROJECT_ROOT / "docs" / "19-analysis-node-deployment.md", target / "README.md")
    shutil.copy2(
        PROJECT_ROOT / "docs" / "14-integration-api-and-mcp.md",
        target / "14-integration-api-and-mcp.md",
    )
    for generated in (
        target / "images.env.example",
        target / "images.lock.json",
        target / "images.tar",
    ):
        generated.unlink(missing_ok=True)


def _inspect_image(reference: str) -> tuple[str, str]:
    image_id = _run(
        ["docker", "image", "inspect", reference, "--format", "{{.Id}}"],
        capture=True,
    ).stdout.strip()
    image_platform = _run(
        [
            "docker",
            "image",
            "inspect",
            reference,
            "--format",
            "{{.Os}}/{{.Architecture}}",
        ],
        capture=True,
    ).stdout.strip()
    if not IMAGE_ID.fullmatch(image_id):
        raise BundleError(f"镜像 ID 格式无效：{reference} -> {image_id}")
    return image_id, image_platform


def _prepare_images(
    package_dir: Path,
    *,
    version: str,
    platform_name: str,
    sources: dict[str, str],
) -> dict[str, dict[str, str]]:
    if shutil.which("docker") is None:
        raise BundleError("构建完整离线包需要 Docker CLI。")
    if shutil.which("syft") is None:
        raise BundleError("构建正式交付包需要 syft 生成逐镜像 SBOM。")
    images: dict[str, dict[str, str]] = {}
    local_references: list[str] = []
    sbom_dir = package_dir / "sbom"
    sbom_dir.mkdir(mode=0o755)
    for role in REQUIRED_ROLES:
        source_ref = sources[role]
        local_ref = f"{LOCAL_REPOSITORIES[role]}:{version}"
        last_error: BundleError | None = None
        for attempt in range(1, 4):
            try:
                _run(["docker", "pull", "--platform", platform_name, source_ref])
                last_error = None
                break
            except BundleError as error:
                last_error = error
                if attempt < 3:
                    time.sleep(5 * attempt)
        if last_error is not None:
            raise last_error
        _run(["docker", "tag", source_ref, local_ref])
        image_id, actual_platform = _inspect_image(local_ref)
        if actual_platform != platform_name:
            raise BundleError(
                f"镜像平台不匹配：{role} 期望 {platform_name}，实际 {actual_platform}"
            )
        _run(
            [
                "syft",
                local_ref,
                "--output",
                f"spdx-json={sbom_dir / f'{role}.spdx.json'}",
            ]
        )
        images[role] = {
            "local_ref": local_ref,
            "source_ref": source_ref,
            "image_id": image_id,
        }
        local_references.append(local_ref)
    _run(
        [
            "docker",
            "image",
            "save",
            "--output",
            str(package_dir / "images.tar"),
            *local_references,
        ]
    )
    return images


def _archive(package_dir: Path, archive_path: Path) -> None:
    archive_path.unlink(missing_ok=True)
    with tarfile.open(archive_path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        archive.add(package_dir, arcname=package_dir.name, recursive=True)


def build_bundle(args: argparse.Namespace) -> tuple[Path, Path]:
    release_document = _read_json(RELEASE_IMAGES)
    sources = validate_sources(
        args.version,
        backend_ref=args.backend_ref,
        frontend_ref=args.frontend_ref,
        release_document=release_document,
    )
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    package_dir = output_dir / f"analysis-node-{args.version}"
    if package_dir.exists():
        raise BundleError(f"目标目录已存在，拒绝覆盖：{package_dir}")
    _copy_package(package_dir)
    env_example = package_dir / ".env.example"
    env_text = env_example.read_text(encoding="utf-8")
    env_text, count = re.subn(
        r"^ANALYSIS_NODE_VERSION=.*$",
        f"ANALYSIS_NODE_VERSION={args.version}",
        env_text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise BundleError(".env.example 缺少 ANALYSIS_NODE_VERSION。")
    env_example.write_text(env_text, encoding="utf-8")
    (package_dir / "images.env").write_text(
        render_images_env(args.version),
        encoding="utf-8",
    )
    images = _prepare_images(
        package_dir,
        version=args.version,
        platform_name=args.platform,
        sources=sources,
    )
    lock = {
        "schema_version": 1,
        "product_version": args.version,
        "platform": args.platform,
        "images": images,
    }
    (package_dir / "images.lock.json").write_text(
        json.dumps(lock, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (package_dir / "bundle.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "product": "BioWorkflowManage Analysis Node",
                "product_version": args.version,
                "platform": args.platform,
                "built_at": datetime.now(timezone.utc).isoformat(),
                "git_revision": args.git_revision,
                "interface_profiles": ["headless", "console"],
                "runtime_profiles": ["isolated-runtime", "host-runtime"],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if args.signature_dir:
        signature_source = Path(args.signature_dir).resolve()
        if not signature_source.is_dir():
            raise BundleError(f"镜像签名目录不存在：{signature_source}")
        signature_target = package_dir / "signatures" / "images"
        shutil.copytree(signature_source, signature_target)
    checksum = checksum_manifest(package_dir)
    (package_dir / "SHA256SUMS").write_text(checksum, encoding="utf-8")
    archive_path = output_dir / f"analysis-node-{args.version}-linux-amd64.tar.gz"
    _archive(package_dir, archive_path)
    return package_dir, archive_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a signed-ready offline Analysis Node bundle."
    )
    parser.add_argument("--version", required=True)
    parser.add_argument("--backend-ref", required=True)
    parser.add_argument("--frontend-ref", required=True)
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "dist"))
    parser.add_argument("--platform", default="linux/amd64", choices=["linux/amd64"])
    parser.add_argument(
        "--git-revision",
        default=os.environ.get("GITHUB_SHA", "unknown"),
    )
    parser.add_argument("--signature-dir")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        package_dir, archive_path = build_bundle(args)
    except (BundleError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print(package_dir)
    print(archive_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
