from __future__ import annotations

import copy
import re
from typing import Any

from django.db import transaction

from compiler_core import canonical_digest

from .analysis_runs import _compile_published_workflow
from .models import (
    AnalysisProduct,
    AnalysisProductVersion,
    WorkflowDocument,
    WorkflowVersion,
)


CONTRACT_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")


class AnalysisProductError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _contract_port_names(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, list):
        raise AnalysisProductError(
            "ANALYSIS_PRODUCT_CONTRACT_INVALID",
            f"WorkflowVersion {label} 必须是数组。",
        )
    names: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise AnalysisProductError(
                "ANALYSIS_PRODUCT_CONTRACT_INVALID",
                f"WorkflowVersion {label}[{index}] 必须是 object。",
            )
        name = str(item.get("name") or "").strip()
        wdl_type = str(item.get("wdl_type") or "").strip()
        semantic_type = str(item.get("semantic_type") or "").strip()
        if not name or not wdl_type or not semantic_type:
            raise AnalysisProductError(
                "ANALYSIS_PRODUCT_CONTRACT_INVALID",
                f"WorkflowVersion {label}[{index}] 缺少 name、wdl_type 或 semantic_type。",
            )
        if name in names:
            raise AnalysisProductError(
                "ANALYSIS_PRODUCT_CONTRACT_INVALID",
                f"WorkflowVersion {label} 包含重复端口名称：{name}。",
            )
        names.append(name)
    return names


def _graph_port_names(workflow_version: WorkflowVersion, *, node_type: str) -> list[str]:
    nodes = workflow_version.workflow_graph.get("nodes")
    if not isinstance(nodes, list):
        return []
    return [
        str(node.get("id") or "").strip()
        for node in nodes
        if isinstance(node, dict) and node.get("type") == node_type
    ]


def normalize_contract_version(value: Any) -> str:
    contract_version = str(value or "").strip()
    if not CONTRACT_VERSION_PATTERN.fullmatch(contract_version):
        raise AnalysisProductError(
            "ANALYSIS_PRODUCT_CONTRACT_VERSION_INVALID",
            "contract_version 格式无效。",
        )
    return contract_version


def snapshot_workflow_contract(
    workflow_version: WorkflowVersion,
) -> tuple[str, dict[str, Any], str]:
    if (
        workflow_version.kind != WorkflowDocument.Kind.WORKFLOW
        or workflow_version.workflow.kind != WorkflowDocument.Kind.WORKFLOW
    ):
        raise AnalysisProductError(
            "ANALYSIS_PRODUCT_WORKFLOW_INVALID",
            "分析产品只能绑定已发布的 Workflow 版本。",
        )
    try:
        _, source_digest = _compile_published_workflow(workflow_version)
    except Exception as error:
        raise AnalysisProductError(
            "ANALYSIS_PRODUCT_WORKFLOW_NOT_RUNNABLE",
            str(error),
        ) from error

    interface_contract = workflow_version.interface_contract
    if not isinstance(interface_contract, dict):
        raise AnalysisProductError(
            "ANALYSIS_PRODUCT_CONTRACT_INVALID",
            "WorkflowVersion 接口契约无效。",
        )
    inputs = interface_contract.get("inputs")
    outputs = interface_contract.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise AnalysisProductError(
            "ANALYSIS_PRODUCT_CONTRACT_INVALID",
            "WorkflowVersion 必须声明输入与语义化输出契约。",
        )
    input_names = _contract_port_names(inputs, label="inputs")
    output_names = _contract_port_names(outputs, label="outputs")
    invalid_outputs = [
        str(item["name"])
        for item in outputs
        if item.get("semantic_type") == "core.output.unknown"
    ]
    if invalid_outputs:
        raise AnalysisProductError(
            "ANALYSIS_PRODUCT_CONTRACT_INVALID",
            "WorkflowVersion 输出缺少 semantic_type："
            + ", ".join(invalid_outputs),
        )
    graph_input_names = _graph_port_names(
        workflow_version,
        node_type="workflow_input",
    )
    graph_output_names = _graph_port_names(
        workflow_version,
        node_type="workflow_output",
    )
    if set(input_names) != set(graph_input_names) or set(output_names) != set(
        graph_output_names
    ):
        raise AnalysisProductError(
            "ANALYSIS_PRODUCT_CONTRACT_INVALID",
            "WorkflowVersion 接口契约端口与 Workflow Graph 不一致。",
        )

    snapshot = copy.deepcopy(interface_contract)
    return source_digest, snapshot, canonical_digest(snapshot)


def publish_analysis_product_version(
    product: AnalysisProduct,
    *,
    contract_version: Any,
    workflow_version: WorkflowVersion,
    actor: str,
) -> tuple[AnalysisProductVersion, bool]:
    normalized_version = normalize_contract_version(contract_version)
    source_digest, interface_contract, contract_digest = snapshot_workflow_contract(
        workflow_version
    )

    with transaction.atomic():
        locked_product = AnalysisProduct.objects.select_for_update().get(pk=product.pk)
        existing = AnalysisProductVersion.objects.filter(
            product=locked_product,
            contract_version=normalized_version,
        ).first()
        if existing is not None:
            if (
                existing.workflow_version_id == workflow_version.pk
                and existing.source_digest == source_digest
                and existing.interface_contract == interface_contract
                and existing.contract_digest == contract_digest
            ):
                return existing, False
            raise AnalysisProductError(
                "ANALYSIS_PRODUCT_VERSION_CONFLICT",
                "相同 analysis_code 与 contract_version 已绑定其他不可变契约。",
            )
        item = AnalysisProductVersion.objects.create(
            product=locked_product,
            contract_version=normalized_version,
            workflow_version=workflow_version,
            source_digest=source_digest,
            interface_contract=interface_contract,
            contract_digest=contract_digest,
            created_by=str(actor or "deployment")[:256],
        )
    return item, True


def analysis_product_version_is_current(item: AnalysisProductVersion) -> bool:
    try:
        source_digest, interface_contract, contract_digest = snapshot_workflow_contract(
            item.workflow_version
        )
    except AnalysisProductError:
        return False
    return (
        item.source_digest == source_digest
        and item.interface_contract == interface_contract
        and item.contract_digest == contract_digest
    )
