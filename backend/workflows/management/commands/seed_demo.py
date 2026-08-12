import hashlib
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from compiler_core import (
    canonical_digest,
    compile_workflow,
    validate_tool_spec,
)
from workflows.models import (
    CompilationRecord,
    ToolDocument,
    ToolVersion,
    WDLRevision,
    WorkflowDocument,
    WorkflowVersion,
)


class Command(BaseCommand):
    help = "Create the deterministic Phase 1 demos when they are absent."

    def handle(self, *args, **options):
        root = Path(__file__).resolve().parents[4]
        fixture = root / "examples" / "phase1-fastp-bwa"
        fastp_fixture = root / "examples" / "phase1-fastp"
        graph = json.loads((fixture / "workflow-graph.json").read_text(encoding="utf-8"))
        tools = [
            json.loads((fastp_fixture / "tool-fastp.json").read_text(encoding="utf-8")),
            json.loads((fixture / "tool-bwa-mem.json").read_text(encoding="utf-8")),
        ]
        for tool in tools:
            ToolDocument.objects.get_or_create(
                tool_id=tool["id"],
                defaults={
                    "draft_spec": tool,
                    "validation": validate_tool_spec(tool),
                },
            )
            ToolVersion.objects.get_or_create(
                tool_id=tool["id"],
                version=tool["tool_version"],
                defaults={
                    "name": tool.get("display_name") or tool["name"],
                    "digest": canonical_digest(tool),
                    "tool_spec": tool,
                },
            )

        editor_document = self._editor_document(graph)
        workflow, created = WorkflowDocument.objects.get_or_create(
            slug=graph["id"],
            defaults={
                "name": graph["name"],
                "description": graph.get("description", ""),
                "workflow_graph": graph,
                "editor_document": editor_document,
                "tool_specs": tools,
                "created_by": "zhuqin",
                "updated_by": "zhuqin",
            },
        )
        self._seed_compiled_snapshot(
            workflow=workflow,
            graph=graph,
            editor_document=editor_document,
            tools=tools,
        )

        fastp_graph = json.loads(
            (fastp_fixture / "workflow-graph.json").read_text(encoding="utf-8")
        )
        fastp_editor = self._editor_document(fastp_graph)
        fastp_workflow, _ = WorkflowDocument.objects.get_or_create(
            slug=fastp_graph["id"],
            defaults={
                "name": fastp_graph["name"],
                "description": fastp_graph.get("description", ""),
                "workflow_graph": fastp_graph,
                "editor_document": fastp_editor,
                "tool_specs": [tools[0]],
                "created_by": "zhuqin",
                "updated_by": "zhuqin",
            },
        )
        self._seed_compiled_snapshot(
            workflow=fastp_workflow,
            graph=fastp_graph,
            editor_document=fastp_editor,
            tools=[tools[0]],
        )

        subflow_graph = json.loads(
            (fastp_fixture / "workflow-graph.json").read_text(encoding="utf-8")
        )
        subflow_graph.update(
            {
                "id": "fastp_qc_subflow",
                "name": "fastp QC reusable subflow",
                "description": (
                    "Reusable paired-end FASTQ preprocessing with fixed inputs "
                    "and outputs."
                ),
            }
        )
        subflow_editor = self._editor_document(subflow_graph)
        subflow, _ = WorkflowDocument.objects.get_or_create(
            slug=subflow_graph["id"],
            defaults={
                "name": subflow_graph["name"],
                "description": subflow_graph["description"],
                "kind": WorkflowDocument.Kind.SUBWORKFLOW,
                "workflow_graph": subflow_graph,
                "editor_document": subflow_editor,
                "tool_specs": [tools[0]],
                "created_by": "zhuqin",
                "updated_by": "zhuqin",
            },
        )
        contract = {
            "contract_version": "1.0.0",
            "inputs": [
                self._contract_port(node)
                for node in subflow_graph["nodes"]
                if node["type"] == "workflow_input"
            ],
            "outputs": [
                self._contract_port(node)
                for node in subflow_graph["nodes"]
                if node["type"] == "workflow_output"
            ],
        }
        self._seed_compiled_snapshot(
            workflow=subflow,
            graph=subflow_graph,
            editor_document=subflow_editor,
            tools=[tools[0]],
            kind=WorkflowDocument.Kind.SUBWORKFLOW,
            interface_contract=contract,
        )
        WorkflowDocument.objects.filter(
            slug__in=[graph["id"], fastp_graph["id"], subflow_graph["id"]],
            created_by="local-user",
        ).update(created_by="zhuqin")
        self.stdout.write(
            "Created Phase 1 demos." if created else "Phase 1 demos already exist."
        )

    @staticmethod
    def _editor_document(graph):
        positions = graph.get("layout", {}).get("nodes", {})
        return {
            "nodes": [
                {"id": node_id, "position": position}
                for node_id, position in positions.items()
            ],
            "viewport": graph.get("layout", {}).get(
                "viewport", {"x": 0, "y": 0, "zoom": 1}
            ),
        }

    @staticmethod
    def _seed_compiled_snapshot(
        *,
        workflow,
        graph,
        editor_document,
        tools,
        kind=WorkflowDocument.Kind.WORKFLOW,
        interface_contract=None,
    ):
        request_id = f"seed_demo:{workflow.slug}:v1"
        has_version = WorkflowVersion.objects.filter(
            workflow=workflow,
            version=1,
        ).exists()
        has_compilation = CompilationRecord.objects.filter(
            workflow=workflow,
            request_id=request_id,
        ).exists()
        has_revision = WDLRevision.objects.filter(
            workflow=workflow,
            version=1,
        ).exists()
        if has_version and has_compilation and has_revision:
            return

        validation, artifacts = compile_workflow(graph, tools)
        if validation["status"] != "valid":
            raise CommandError(f"Demo workflow {workflow.slug} did not compile.")
        compiled_bundle = {
            "entrypoint": "workflow.wdl",
            "files": {
                item["name"]: item["content"]
                for item in artifacts
                if item.get("media_type") == "application/wdl"
            },
            "call_count": sum(
                1
                for node in graph.get("nodes", [])
                if node.get("type") in {"tool", "subworkflow"}
            ),
        }
        compiled_digest = canonical_digest(compiled_bundle)
        workflow_version, _ = WorkflowVersion.objects.get_or_create(
            workflow=workflow,
            version=1,
            defaults={
                "name": workflow.name,
                "description": workflow.description,
                "kind": kind,
                "semantic_digest": validation["source"]["digest"],
                "workflow_graph": graph,
                "editor_document": editor_document,
                "tool_specs": tools,
                "interface_contract": interface_contract or {},
                "subworkflow_references": [],
                "compiled_bundle": compiled_bundle,
                "compiled_digest": compiled_digest,
                "compiler_profile": "compiler-core-v1",
            },
        )
        if not workflow_version.compiled_bundle or not workflow_version.compiled_digest:
            workflow_version.compiled_bundle = compiled_bundle
            workflow_version.compiled_digest = compiled_digest
            workflow_version.compiler_profile = "compiler-core-v1"
            workflow_version.save(
                update_fields=[
                    "compiled_bundle",
                    "compiled_digest",
                    "compiler_profile",
                ]
            )
        CompilationRecord.objects.get_or_create(
            workflow=workflow,
            request_id=request_id,
            defaults={
                "workflow_version": workflow_version,
                "status": "succeeded",
                "semantic_digest": validation["source"]["digest"],
                "validation": validation,
                "artifacts": artifacts,
            },
        )
        wdl_artifact = next(
            item for item in artifacts if item.get("name") == "workflow.wdl"
        )
        content = wdl_artifact["content"]
        WDLRevision.objects.get_or_create(
            workflow=workflow,
            version=1,
            defaults={
                "workflow_version": workflow_version,
                "source": WDLRevision.Source.SYSTEM,
                "content": content,
                "digest": "sha256:"
                + hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "validation": {"status": "valid", "diagnostics": []},
            },
        )

    @staticmethod
    def _contract_port(node):
        port = node.get("port") or {}
        return {
            "name": node.get("id"),
            "label": node.get("label") or node.get("id"),
            "wdl_type": port.get("wdl_type"),
            "semantic_type": port.get("semantic_type"),
            "required": bool(port.get("required", False)),
        }
