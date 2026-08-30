from copy import deepcopy
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from rest_framework.test import APIClient

from compiler_core import canonical_digest, compile_workflow, validate_tool_spec
from workflows.annotation_tools import (
    ANNOVAR_ANNOTATION_VERSION,
    ANNOVAR_CANONICAL_IDS,
    ANNOVAR_TOOL_ID,
    enhance_annosnv_spec,
)
from workflows.analysis_runs import _annotation_reference_entry
from workflows.models import ToolDocument, ToolVersion, WorkflowVersion


pytestmark = pytest.mark.usefixtures("auth_disabled")


def annosnv_source_spec():
    return {
        "schema_version": "1.0.0",
        "id": ANNOVAR_TOOL_ID,
        "name": "AnnoSNV",
        "display_name": "AnnoSNV",
        "tool_version": "20180416",
        "description": "Historical ANNOVAR task",
        "category": "historical_wdl",
        "container": {
            "engine": "docker",
            "image": "registry.cn-shanghai.aliyuncs.com/kszy-biosoft/annovar:v20180416",
        },
        "inputs": [
            {
                "name": "humandb",
                "label": "humandb",
                "wdl_type": "String",
                "semantic_type": "core.string",
                "required": True,
            },
            {
                "name": "ref_version",
                "label": "ref_version",
                "wdl_type": "String",
                "semantic_type": "core.string",
                "required": True,
            },
            {
                "name": "sample",
                "label": "sample",
                "wdl_type": "String",
                "semantic_type": "core.string",
                "required": True,
            },
            {
                "name": "vcf",
                "label": "vcf",
                "wdl_type": "File",
                "semantic_type": "core.file.any",
                "required": True,
            },
        ],
        "outputs": [
            {
                "name": "anno_vcf",
                "label": "anno_vcf",
                "wdl_type": "File",
                "semantic_type": "core.file.any",
                "optional": False,
                "capture": {
                    "mode": "expression",
                    "value": '"~{sample}.var.~{ref_version}_multianno.vcf"',
                },
            }
        ],
        "command": {
            "shell": "bash",
            "strict_mode": False,
            "template": "echo historical\n",
        },
        "runtime": {"cpu": 2, "memory_gb": 4},
        "metadata": {
            "tags": ["wdl-package"],
            "migration_warnings": [
                "原 WDL 类型 Directory 暂不受 ToolSpec 支持，已按 String 导入。"
            ],
        },
    }


def passthrough_spec():
    return {
        "schema_version": "1.0.0",
        "id": "normalize_vcf",
        "name": "normalize_vcf",
        "display_name": "Normalize VCF",
        "tool_version": "1.0.0",
        "container": {"engine": "docker", "image": "ubuntu:24.04"},
        "inputs": [
            {
                "name": "vcf",
                "wdl_type": "File",
                "semantic_type": "bio.variant.vcf",
                "required": True,
            }
        ],
        "outputs": [
            {
                "name": "normalized_vcf",
                "wdl_type": "File",
                "semantic_type": "bio.variant.vcf",
                "capture": {"mode": "path", "value": "normalized.vcf"},
            }
        ],
        "command": {
            "shell": "bash",
            "strict_mode": True,
            "template": 'cp "~{vcf}" normalized.vcf\n',
        },
    }


def annotation_workflow(annotation_spec, standard_spec):
    anno_digest = canonical_digest(annotation_spec)
    standard_digest = canonical_digest(standard_spec)
    return {
        "schema_version": "1.0.0",
        "id": "selectable_annotation_demo",
        "name": "Selectable annotation demo",
        "target": {
            "language": "wdl",
            "version": "1.0",
            "profile": "miniwdl-compatible",
        },
        "nodes": [
            {
                "id": "input_vcf",
                "type": "workflow_input",
                "port": {
                    "name": "value",
                    "wdl_type": "File",
                    "semantic_type": "bio.variant.vcf",
                    "required": True,
                },
            },
            {
                "id": "input_humandb",
                "type": "workflow_input",
                "port": {
                    "name": "value",
                    "wdl_type": "Directory",
                    "semantic_type": "bio.annotation.database_dir",
                    "required": True,
                },
            },
            {
                "id": "normalize",
                "type": "tool",
                "tool_ref": {
                    "id": standard_spec["id"],
                    "tool_version": standard_spec["tool_version"],
                    "spec_version": standard_spec["schema_version"],
                    "digest": standard_digest,
                },
            },
            {
                "id": "annotate",
                "type": "tool",
                "tool_ref": {
                    "id": annotation_spec["id"],
                    "tool_version": annotation_spec["tool_version"],
                    "spec_version": annotation_spec["schema_version"],
                    "digest": anno_digest,
                },
                "parameter_values": {
                    "sample": "demo",
                    "ref_version": "hg19",
                    "annotation_items": [
                        "refgene",
                        "clinvar",
                        "dbsnp",
                        "cosmic",
                        "dbnsfp",
                    ],
                },
            },
            {
                "id": "output_vcf",
                "type": "workflow_output",
                "port": {
                    "name": "value",
                    "wdl_type": "File",
                    "semantic_type": "bio.variant.vcf.annotated",
                },
            },
        ],
        "edges": [
            {
                "id": "input_to_normalize",
                "source": {"node_id": "input_vcf", "port": "value"},
                "target": {"node_id": "normalize", "port": "vcf"},
            },
            {
                "id": "normalize_to_annotate",
                "source": {"node_id": "normalize", "port": "normalized_vcf"},
                "target": {"node_id": "annotate", "port": "vcf"},
            },
            {
                "id": "humandb_to_annotate",
                "source": {"node_id": "input_humandb", "port": "value"},
                "target": {"node_id": "annotate", "port": "humandb"},
            },
            {
                "id": "annotate_to_output",
                "source": {"node_id": "annotate", "port": "anno_vcf"},
                "target": {"node_id": "output_vcf", "port": "value"},
            },
        ],
    }


def test_annosnv_enhancement_defaults_to_all_options_and_is_valid():
    spec = enhance_annosnv_spec(annosnv_source_spec())

    assert spec["task_kind"] == "annotation"
    selector = next(item for item in spec["inputs"] if item["name"] == "annotation_items")
    assert selector["default"] == ANNOVAR_CANONICAL_IDS
    assert next(item for item in spec["inputs"] if item["name"] == "humandb")[
        "wdl_type"
    ] == "Directory"
    assert validate_tool_spec(spec)["status"] == "valid"


def test_annotation_contract_rejects_partial_default():
    spec = enhance_annosnv_spec(annosnv_source_spec())
    selector = next(item for item in spec["inputs"] if item["name"] == "annotation_items")
    selector["default"] = ["refgene"]

    validation = validate_tool_spec(spec)

    assert validation["status"] == "invalid"
    assert "TA003" in {item["code"] for item in validation["diagnostics"]}


def test_annotation_run_requires_only_selected_database_resources():
    spec = enhance_annosnv_spec(annosnv_source_spec())
    version = SimpleNamespace(
        tool_specs=[spec],
        workflow_graph={
            "nodes": [
                {
                    "id": "annotate",
                    "type": "tool",
                    "tool_ref": {"id": spec["id"]},
                    "parameter_values": {
                        "ref_version": "hg19",
                        "annotation_items": ["refgene", "clinvar"],
                    },
                }
            ]
        },
    )
    reference = {
        "id": "hg19",
        "ref_version": "hg19",
        "required": [
            {"path": "hg19/humandb", "kind": "directory"},
            {"path": "hg19/humandb/hg19_refGeneWithVer.txt", "kind": "file"},
            {"path": "hg19/humandb/hg19_clinvar_20220320.txt", "kind": "file"},
            {"path": "hg19/humandb/hg19_dbnsfp42a.txt", "kind": "file"},
            {"path": "hg19/cnvdb/DGV.txt.gz", "kind": "file"},
        ],
    }

    scoped = _annotation_reference_entry(version, reference)

    assert [item["path"] for item in scoped["required"]] == [
        "hg19/humandb",
        "hg19/humandb/hg19_clinvar_20220320.txt",
        "hg19/humandb/hg19_refGeneWithVer.txt",
    ]


def test_standard_and_annotation_tasks_compile_to_stable_miniwdl_workflow():
    annotation_spec = enhance_annosnv_spec(annosnv_source_spec())
    standard_spec = passthrough_spec()
    graph = annotation_workflow(annotation_spec, standard_spec)

    validation, artifacts = compile_workflow(
        graph, [standard_spec, annotation_spec]
    )

    assert validation["status"] == "valid"
    wdl = next(item["content"] for item in artifacts if item["name"] == "workflow.wdl")
    manifest = next(
        item["content"]
        for item in artifacts
        if item["name"] == "compile-manifest.json"
    )
    assert wdl.startswith("version development")
    assert "Directory input_humandb" in wdl
    assert 'Array[String] annotation_items = ["refgene", "cytoband", "clinvar", "thousand_genomes", "dbsnp", "cosmic", "dbnsfp", "exac", "gnomad_genome"]' in wdl
    assert "~{sep=',' annotation_items}" in wdl
    assert "call normalize_vcf as normalize" in wdl
    assert f"call {ANNOVAR_TOOL_ID} as annotate" in wdl
    assert '"version": "development"' in manifest


@pytest.mark.django_db
def test_annotation_workflow_publishes_an_immutable_reusable_version():
    annotation_spec = enhance_annosnv_spec(annosnv_source_spec())
    standard_spec = passthrough_spec()
    graph = annotation_workflow(annotation_spec, standard_spec)
    client = APIClient()
    saved = client.put(
        "/api/v1/editor/workflows/selectable_annotation_demo",
        {
            "name": graph["name"],
            "workflow_graph": graph,
            "tool_specs": [standard_spec, annotation_spec],
            "editor_document": {"nodes": []},
        },
        format="json",
    )
    assert saved.status_code == 200

    first = client.post(
        "/api/v1/editor/workflows/selectable_annotation_demo/versions",
        {
            "reuse_unchanged": True,
            "base_document_version": saved.data["document_version"],
            "base_document_digest": saved.data["document_digest"],
        },
        format="json",
    )
    repeated = client.post(
        "/api/v1/editor/workflows/selectable_annotation_demo/versions",
        {
            "reuse_unchanged": True,
            "base_document_version": saved.data["document_version"],
            "base_document_digest": saved.data["document_digest"],
        },
        format="json",
    )

    assert first.status_code == 201
    assert repeated.status_code == 200
    assert repeated.data["reused"] is True
    assert WorkflowVersion.objects.count() == 1


@pytest.mark.django_db
def test_prepare_annotation_tools_publishes_without_mutating_older_versions():
    source = annosnv_source_spec()
    ToolDocument.objects.create(
        tool_id=ANNOVAR_TOOL_ID,
        draft_spec=source,
        validation=validate_tool_spec(source),
    )
    older = deepcopy(source)
    ToolVersion.objects.create(
        tool_id=ANNOVAR_TOOL_ID,
        version="20180416",
        name="AnnoSNV",
        digest=canonical_digest(older),
        tool_spec=older,
    )

    call_command("prepare_annotation_tools", publish=True)

    assert ToolVersion.objects.count() == 2
    assert ToolVersion.objects.get(version="20180416").tool_spec == older
    enhanced = ToolVersion.objects.get(version=ANNOVAR_ANNOTATION_VERSION)
    assert enhanced.tool_spec["task_kind"] == "annotation"
