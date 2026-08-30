from __future__ import annotations

import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from compiler_core import canonical_digest
from workflows.analysis_products import (
    AnalysisProductError,
    publish_analysis_product_version,
)
from workflows.models import AnalysisProduct, WorkflowDocument, WorkflowVersion


SMOKE_PRODUCT_CODE = "analysis-node-smoke"
SMOKE_CONTRACT_VERSION = "1.0.0"
DEFAULT_SMOKE_IMAGE = "bioworkflowmanage/smoke-task:1.0.0"


def smoke_workflow_snapshot(image: str) -> dict:
    workflow_name = "analysis_node_smoke"
    wdl = f'''version 1.0

task analysis_node_smoke_task {{
  command <<<
    python -c "from pathlib import Path; Path('result.txt').write_text('analysis-node-ok\\n')"
  >>>
  output {{
    File result = "result.txt"
  }}
  runtime {{
    docker: "{image}"
  }}
}}

workflow {workflow_name} {{
  call analysis_node_smoke_task
  output {{
    File result = analysis_node_smoke_task.result
  }}
}}
'''
    graph = {
        "schema_version": "1.0.0",
        "id": workflow_name,
        "name": "Analysis Node smoke",
        "nodes": [
            {
                "id": "smoke",
                "type": "tool",
                "label": "Analysis Node smoke task",
                "tool_ref": {
                    "id": "analysis_node_smoke_task",
                    "tool_version": SMOKE_CONTRACT_VERSION,
                    "spec_version": "1.0.0",
                    "digest": f"sha256:{canonical_digest({'image': image})}",
                },
                "parameter_values": {},
            },
            {
                "id": "result",
                "type": "workflow_output",
                "label": "Smoke result",
                "port": {
                    "name": "value",
                    "wdl_type": "File",
                    "semantic_type": "report.analysis_node_smoke",
                    "required": True,
                },
            },
        ],
        "edges": [
            {
                "id": "smoke-result",
                "source": {"node_id": "smoke", "port": "result"},
                "target": {"node_id": "result", "port": "value"},
            }
        ],
    }
    bundle = {
        "entrypoint": "workflow.wdl",
        "files": {"workflow.wdl": wdl},
        "call_count": 1,
    }
    interface = {
        "contract_version": SMOKE_CONTRACT_VERSION,
        "inputs": [],
        "outputs": [
            {
                "name": "result",
                "label": "Smoke result",
                "wdl_type": "File",
                "semantic_type": "report.analysis_node_smoke",
                "required": True,
            }
        ],
    }
    return {"graph": graph, "bundle": bundle, "interface": interface}


class Command(BaseCommand):
    help = "Create the immutable trusted Analysis Node smoke product."

    def add_arguments(self, parser):
        parser.add_argument("--actor", default="analysis-node-installer")

    @transaction.atomic
    def handle(self, *args, **options):
        image = str(
            os.environ.get("ANALYSIS_NODE_SMOKE_TASK_IMAGE", DEFAULT_SMOKE_IMAGE)
        ).strip()
        if not image or (":" not in image and "@sha256:" not in image):
            raise CommandError("ANALYSIS_NODE_SMOKE_TASK_IMAGE 必须固定 tag 或 digest。")
        actor = str(options["actor"] or "analysis-node-installer")[:256]
        snapshot = smoke_workflow_snapshot(image)
        graph = snapshot["graph"]
        bundle = snapshot["bundle"]
        interface = snapshot["interface"]

        document, created = WorkflowDocument.objects.get_or_create(
            slug=SMOKE_PRODUCT_CODE,
            defaults={
                "name": "Analysis Node smoke",
                "description": "Trusted installation smoke workflow.",
                "workflow_graph": graph,
                "tool_specs": [],
                "created_by": actor,
                "updated_by": actor,
            },
        )
        if not created and (
            document.workflow_graph != graph or document.kind != WorkflowDocument.Kind.WORKFLOW
        ):
            raise CommandError(
                "现有 analysis-node-smoke Workflow 与可信快照不一致；不会覆盖。"
            )

        version = WorkflowVersion.objects.filter(workflow=document, version=1).first()
        expected = {
            "semantic_digest": canonical_digest(graph),
            "compiled_digest": canonical_digest(bundle),
        }
        if version is None:
            version = WorkflowVersion.objects.create(
                workflow=document,
                version=1,
                name=document.name,
                description=document.description,
                semantic_digest=expected["semantic_digest"],
                workflow_graph=graph,
                tool_specs=[],
                compiled_bundle=bundle,
                compiled_digest=expected["compiled_digest"],
                compiler_profile="analysis-node-installer-v1",
                interface_contract=interface,
            )
        elif any(
            (
                version.workflow_graph != graph,
                version.compiled_bundle != bundle,
                version.interface_contract != interface,
                version.semantic_digest != expected["semantic_digest"],
                version.compiled_digest != expected["compiled_digest"],
            )
        ):
            raise CommandError(
                "现有 analysis-node-smoke WorkflowVersion 与可信快照不一致；不会覆盖。"
            )

        product, _ = AnalysisProduct.objects.get_or_create(
            code=SMOKE_PRODUCT_CODE,
            defaults={
                "name": "Analysis Node smoke",
                "description": "Trusted installation smoke contract.",
                "created_by": actor,
            },
        )
        if not product.is_active:
            product.is_active = True
            product.save(update_fields=["is_active", "updated_at"])
        try:
            product_version, published = publish_analysis_product_version(
                product,
                contract_version=SMOKE_CONTRACT_VERSION,
                workflow_version=version,
                actor=actor,
            )
        except AnalysisProductError as error:
            raise CommandError(f"{error.code}: {error}") from error
        action = "PUBLISHED" if published else "REUSED"
        self.stdout.write(
            self.style.SUCCESS(
                f"{action} {product.code}@{product_version.contract_version} "
                f"workflow_version={version.pk} image={image}"
            )
        )
