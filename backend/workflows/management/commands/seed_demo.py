import json
from pathlib import Path

from django.core.management.base import BaseCommand

from compiler_core import (
    canonical_digest,
    validate_tool_spec,
    validate_workflow_graph,
)
from workflows.models import (
    ToolDocument,
    ToolVersion,
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
        positions = graph.get("layout", {}).get("nodes", {})
        editor_document = {
            "nodes": [{"id": node_id, "position": position} for node_id, position in positions.items()],
            "viewport": graph.get("layout", {}).get("viewport", {"x": 0, "y": 0, "zoom": 1}),
        }
        _, created = WorkflowDocument.objects.get_or_create(
            slug=graph["id"],
            defaults={
                "name": graph["name"],
                "workflow_graph": graph,
                "editor_document": editor_document,
                "tool_specs": tools,
            },
        )

        fastp_graph = json.loads(
            (fastp_fixture / "workflow-graph.json").read_text(encoding="utf-8")
        )
        fastp_positions = fastp_graph.get("layout", {}).get("nodes", {})
        fastp_editor = {
            "nodes": [
                {"id": node_id, "position": position}
                for node_id, position in fastp_positions.items()
            ],
            "viewport": fastp_graph.get("layout", {}).get(
                "viewport", {"x": 0, "y": 0, "zoom": 1
            ),
        }
        WorkflowDocument.objects.get_or_create(
            slug=fastp_graph["id"],
            defaults={
                "name": fastp_graph["name"],
                "description": fastp_graph.get("description", ""),
                "workflow_graph": fastp_graph,
                "editor_document": fastp_editor,
                "tool_specs": [tools[0]],
            },
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
        subflow_positions = subflow_graph.get("layout", {}).get("nodes", {})
        subflow_editor = {
            "nodes": [
                {"id": node_id, "position": position}
                for node_id, position in subflow_positions.items()
            ],
            "viewport": subflow_graph.get("layout", {}).get(
                "viewport", {"x": 0, "y": 0, "zoom": 1
            ),
        }
        subflow, _ = WorkflowDocument.objects.get_or_create(
            slug=subflow_graph["id"],
            defaults={
                "name": subflow_graph["name"],
                "description": subflow_graph["description"],
                "kind": WorkflowDocument.Kind.SUBWORKFLOW,
                "workflow_graph": subflow_graph,
                "editor_document": subflow_editor,
                "tool_specs": [tools[0]],
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
        validation, _ = validate_workflow_graph(subflow_graph, [tools[0]])
        WorkflowVersion.objects.get_or_create(
            workflow=subflow,
            version=1,
            defaults={
                "name": subflow.name,
                "description": subflow.description,
                "kind": WorkflowDocument.Kind.SUBWORKFLOW,
                "semantic_digest": validation["source"]["digest"],
                "workflow_graph": subflow_graph,
                "editor_document": subflow_editor,
                "tool_specs": [tools[0]],
                "interface_contract": contract,
                "subworkflow_references": [],
            },
        )
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
        self.stdout.write("Created Phase 1 demos." if created else "Phase 1 demos already exist.")

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
