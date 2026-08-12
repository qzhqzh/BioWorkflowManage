from __future__ import annotations

from compiler_core import canonical_digest

from .models import WorkflowDocument


def workflow_document_digest(document: WorkflowDocument) -> str:
    return canonical_digest(
        {
            "name": document.name,
            "description": document.description,
            "kind": document.kind,
            "workflow_graph": document.workflow_graph,
            "editor_document": document.editor_document,
            "tool_specs": document.tool_specs,
            "subworkflow_references": document.subworkflow_references,
        }
    )
