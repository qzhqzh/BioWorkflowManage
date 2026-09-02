from __future__ import annotations

import base64
import json
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_slug
from django.db import transaction
from django.db.models import Max

from compiler_core import canonical_digest

from workflows.analysis_products import (
    AnalysisProductError,
    publish_analysis_product_version,
)
from workflows.execution_engines import (
    MAX_SOURCE_BYTES,
    MAX_SOURCE_FILES,
    NEXTFLOW,
    normalize_source_path,
    validate_execution_snapshot,
)
from workflows.models import AnalysisProduct, WorkflowDocument, WorkflowVersion


REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def _object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CommandError(f"{label} 必须是 object。")
    return value


def _string(value: Any, *, label: str, max_length: int = 256) -> str:
    result = str(value or "").strip()
    if not result or len(result) > max_length:
        raise CommandError(f"{label} 不能为空且长度不能超过 {max_length}。")
    return result


def _slug(value: Any, *, label: str) -> str:
    result = _string(value, label=label, max_length=128).lower()
    try:
        validate_slug(result)
    except ValidationError as error:
        raise CommandError(f"{label} 必须是有效 slug。") from error
    return result


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > 1024 * 1024:
            raise CommandError("Nextflow 产品清单超过 1 MiB。")
        value = json.loads(path.read_text(encoding="utf-8"))
    except CommandError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CommandError("无法读取 Nextflow 产品清单。") from error
    manifest = _object(value, label="产品清单")
    if manifest.get("schema_version") != 1:
        raise CommandError("产品清单 schema_version 必须为 1。")
    return manifest


def _read_source_files(
    source_root: Path,
    source: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    raw_files = source.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise CommandError("source.files 必须是非空数组。")
    if len(raw_files) > MAX_SOURCE_FILES:
        raise CommandError(f"source.files 不能超过 {MAX_SOURCE_FILES} 项。")
    executable_files = source.get("executable_files") or []
    if not isinstance(executable_files, list):
        raise CommandError("source.executable_files 必须是数组。")
    try:
        normalized_files = [normalize_source_path(item) for item in raw_files]
        normalized_executables = [
            normalize_source_path(item) for item in executable_files
        ]
    except ValueError as error:
        raise CommandError(str(error)) from error
    if len(set(normalized_files)) != len(normalized_files):
        raise CommandError("source.files 包含重复路径。")
    if not set(normalized_executables).issubset(normalized_files):
        raise CommandError("source.executable_files 必须属于 source.files。")

    resolved_root = source_root.resolve()
    files: dict[str, Any] = {}
    total_bytes = 0
    for relative_path in normalized_files:
        if any(
            part.startswith(".") or part in {"__pycache__", "node_modules"}
            for part in Path(relative_path).parts
        ):
            raise CommandError(f"source.files 包含禁止路径：{relative_path}。")
        path = source_root / relative_path
        try:
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                raise CommandError(f"源码文件不是普通文件：{relative_path}。")
            resolved = path.resolve()
            resolved.relative_to(resolved_root)
            content = resolved.read_bytes()
        except CommandError:
            raise
        except (OSError, ValueError) as error:
            raise CommandError(f"无法安全读取源码文件：{relative_path}。") from error
        total_bytes += len(content)
        if total_bytes > MAX_SOURCE_BYTES:
            raise CommandError(f"源码文件总量超过 {MAX_SOURCE_BYTES} 字节。")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            files[relative_path] = {
                "encoding": "base64",
                "content": base64.b64encode(content).decode("ascii"),
            }
        else:
            files[relative_path] = text
    return files, normalized_executables


def _verify_git_source(source_root: Path, expected_revision: str) -> None:
    try:
        top_level = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        ).stdout.strip()
        revision = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            encoding="ascii",
            timeout=10,
        ).stdout.strip()
        status_output = subprocess.run(
            [
                "git",
                "-C",
                str(source_root),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise CommandError("无法验证 Nextflow 源码仓库状态。") from error
    if Path(top_level).resolve() != source_root.resolve():
        raise CommandError("--source-dir 必须是 Nextflow 源码仓库根目录。")
    if revision != expected_revision:
        raise CommandError(
            f"源码 HEAD 与清单不一致：需要 {expected_revision}，实际 {revision}。"
        )
    if status_output.strip():
        raise CommandError("Nextflow 源码仓库存在未提交改动，拒绝导入。")


def _workflow_graph(
    workflow: dict[str, Any],
    interface_contract: dict[str, Any],
    runtime_manifest: dict[str, Any],
) -> dict[str, Any]:
    inputs = interface_contract.get("inputs")
    outputs = interface_contract.get("outputs")
    if not isinstance(inputs, list) or not isinstance(outputs, list) or not outputs:
        raise CommandError("interface_contract 必须声明 inputs 与非空 outputs。")
    nodes = []
    for node_type, ports in (("workflow_input", inputs), ("workflow_output", outputs)):
        for item in ports:
            port = _object(item, label=f"interface_contract.{node_type}")
            name = _string(port.get("name"), label=f"{node_type}.name", max_length=128)
            nodes.append(
                {
                    "id": name,
                    "type": node_type,
                    "label": str(port.get("label") or name),
                    "port": dict(port),
                }
            )
    return {
        "schema_version": "1.0.0",
        "id": _string(workflow.get("workflow_id"), label="workflow.workflow_id", max_length=128),
        "name": _string(workflow.get("name"), label="workflow.name"),
        "target": {
            "language": NEXTFLOW,
            "version": runtime_manifest["engine_version"],
            "profile": runtime_manifest["profile"],
        },
        "nodes": nodes,
        "edges": [],
    }


class Command(BaseCommand):
    help = "Import an immutable trusted Nextflow package and optionally publish its Analysis Product."

    def add_arguments(self, parser):
        parser.add_argument("--manifest", required=True)
        parser.add_argument("--source-dir", required=True)
        parser.add_argument("--actor", default="deployment")
        parser.add_argument("--publish-product", action="store_true")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        manifest_path = Path(options["manifest"])
        source_root = Path(options["source_dir"])
        manifest = _read_manifest(manifest_path)
        workflow = _object(manifest.get("workflow"), label="workflow")
        product_spec = _object(manifest.get("analysis_product"), label="analysis_product")
        source = _object(manifest.get("source"), label="source")
        interface_contract = _object(
            manifest.get("interface_contract"),
            label="interface_contract",
        )
        runtime_manifest = _object(
            manifest.get("runtime_manifest"),
            label="runtime_manifest",
        )
        slug = _slug(workflow.get("slug"), label="workflow.slug")
        product_code = _slug(product_spec.get("code"), label="analysis_product.code")
        revision = _string(
            source.get("revision"),
            label="source.revision",
            max_length=40,
        )
        if not REVISION_PATTERN.fullmatch(revision):
            raise CommandError("source.revision 必须是完整 40 位 Git commit。")
        _verify_git_source(source_root, revision)
        files, executable_files = _read_source_files(source_root, source)
        try:
            entrypoint = normalize_source_path(source.get("entrypoint"))
        except ValueError as error:
            raise CommandError(str(error)) from error
        bundle = {
            "entrypoint": entrypoint,
            "files": files,
            "executable_files": executable_files,
            "call_count": source.get("call_count"),
            "provenance": {
                "repository": _string(
                    source.get("repository"),
                    label="source.repository",
                    max_length=512,
                ),
                "revision": revision,
            },
            "execution": {
                "engine": NEXTFLOW,
                "runtime_manifest": runtime_manifest,
            },
        }
        output_names = {
            str(item.get("name") or "")
            for item in interface_contract.get("outputs") or []
            if isinstance(item, dict) and item.get("name")
        }
        try:
            validate_execution_snapshot(
                NEXTFLOW,
                bundle,
                runtime_manifest,
                output_names=output_names,
            )
        except ValueError as error:
            raise CommandError(str(error)) from error
        graph = _workflow_graph(workflow, interface_contract, runtime_manifest)
        compiled_digest = canonical_digest(bundle)
        if options["dry_run"]:
            self.stdout.write(
                self.style.SUCCESS(
                    f"VALID source_digest={compiled_digest} files={len(files)} "
                    f"bytes<={MAX_SOURCE_BYTES}"
                )
            )
            return

        actor = str(options.get("actor") or "deployment")[:256]
        try:
            with transaction.atomic():
                document, _ = WorkflowDocument.objects.select_for_update().get_or_create(
                    slug=slug,
                    defaults={
                        "name": str(workflow["name"]),
                        "description": str(workflow.get("description") or ""),
                        "kind": WorkflowDocument.Kind.WORKFLOW,
                        "workflow_graph": graph,
                        "tool_specs": [],
                    },
                )
                latest = document.versions.order_by("-version").first()
                reusable = bool(
                    latest
                    and latest.compiled_digest == compiled_digest
                    and latest.execution_engine == NEXTFLOW
                    and latest.runtime_manifest == runtime_manifest
                    and latest.interface_contract == interface_contract
                )
                if reusable:
                    version = latest
                    version_created = False
                else:
                    next_version = (
                        document.versions.aggregate(value=Max("version"))["value"] or 0
                    ) + 1
                    version = WorkflowVersion.objects.create(
                        workflow=document,
                        version=next_version,
                        name=str(workflow["name"]),
                        description=str(workflow.get("description") or ""),
                        kind=WorkflowDocument.Kind.WORKFLOW,
                        semantic_digest=canonical_digest(graph),
                        workflow_graph=graph,
                        editor_document={},
                        tool_specs=[],
                        compiled_bundle=bundle,
                        compiled_digest=compiled_digest,
                        compiler_profile=str(
                            workflow.get("compiler_profile") or "nextflow-import-v1"
                        ),
                        execution_engine=NEXTFLOW,
                        runtime_manifest=runtime_manifest,
                        interface_contract=interface_contract,
                        subworkflow_references=[],
                    )
                    version_created = True

                product_version = None
                product_version_created = False
                if options["publish_product"]:
                    product, _ = AnalysisProduct.objects.get_or_create(
                        code=product_code,
                        defaults={
                            "name": str(product_spec.get("name") or product_code),
                            "description": str(product_spec.get("description") or ""),
                            "created_by": actor,
                        },
                    )
                    product_version, product_version_created = (
                        publish_analysis_product_version(
                            product,
                            contract_version=product_spec.get("contract_version"),
                            workflow_version=version,
                            actor=actor,
                        )
                    )
        except AnalysisProductError as error:
            raise CommandError(f"{error.code}: {error}") from error

        self.stdout.write(
            self.style.SUCCESS(
                f"{'IMPORTED' if version_created else 'REUSED'} "
                f"workflow_version={version.pk} source_digest={compiled_digest}"
            )
        )
        if product_version is not None:
            self.stdout.write(
                self.style.SUCCESS(
                    f"{'PUBLISHED' if product_version_created else 'REUSED'} "
                    f"{product_code}@{product_version.contract_version}"
                )
            )
