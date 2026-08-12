from django.core.management import call_command

import pytest

from workflows.models import (
    CompilationRecord,
    ToolDocument,
    WDLRevision,
    WorkflowDocument,
)
from workflows.management.commands import seed_demo


@pytest.mark.django_db
def test_seed_test_data_is_idempotent(monkeypatch, capsys):
    monkeypatch.setenv("DJANGO_SEED_ALLOW_DEFAULT_PASSWORDS", "1")
    def fake_compile_workflow(graph, tools):
        return (
            {
                "status": "valid",
                "source": {"digest": f"sha256:{graph['id']}"},
            },
            [{"name": "workflow.wdl", "content": f"workflow {graph['id']} {{}}\n"}],
        )

    monkeypatch.setattr(seed_demo, "compile_workflow", fake_compile_workflow)
    call_command("seed_test_data")
    call_command("seed_test_data")

    assert WorkflowDocument.objects.count() == 3
    assert set(WorkflowDocument.objects.values_list("created_by", flat=True)) == {"zhuqin"}
    assert ToolDocument.objects.count() == 2
    assert WDLRevision.objects.count() == 3
    assert CompilationRecord.objects.count() == 3
    assert "Test data ready" in capsys.readouterr().out
