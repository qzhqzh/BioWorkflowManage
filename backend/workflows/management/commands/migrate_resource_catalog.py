from __future__ import annotations

import copy

from django.core.management.base import BaseCommand

from workflows.analysis_runs import (
    BLOOD_TUMOR_PANEL_REQUIREMENT_PATHS,
    _blood_tumor_hg38_reference,
    _merge_reference_requirements,
)
from workflows.resource_catalog import load_catalog_state, save_catalog


BLOOD_WORKFLOWS = [
    "tumor-blood-single-production",
    "tumor-blood-pair-production",
]


def _required(path: str, label: str, kind: str = "file") -> dict[str, str]:
    return {"path": path, "label": label, "kind": kind}


BLOOD_TUMOR_PANELS = (
    {
        "id": "blood-84",
        "name": "血液肿瘤 84 Panel",
        "description": "84 基因血液肿瘤检测方案；BED 与 gene BED 需按本地生产版本配置。",
        "resource_version": "",
        "reference": "hg38",
        "workflow_ids": BLOOD_WORKFLOWS,
        "bindings": {
            "bed": "",
            "gene_bed": "",
            "gene_list": "hg38/blood_tumor/bed/kszy_84panel.hg38.gene.list",
            "cnvkit_db": "hg38/blood_tumor/cnvkit/84panel",
        },
        "required_bindings": [
            {
                "key": "bed",
                "label": "84 Panel 捕获区域 BED",
                "kind": "file",
                "basename_includes": ["84panel"],
            },
            {"key": "gene_bed", "label": "84 Panel 基因区域 BED", "kind": "file"},
            {"key": "gene_list", "label": "84 Panel 基因列表", "kind": "file"},
            {
                "key": "cnvkit_db",
                "label": "84 Panel CNVKit 基线",
                "kind": "directory",
            },
        ],
        "required": [
            _required(
                "hg38/blood_tumor/resource/84panel_filter.xls",
                "84 Panel 注释过滤库",
            ),
            _required(
                "hg38/blood_tumor/resource/local_freq_blood/dna_fusion/84fusion.mutation_frequency.txt",
                "84 Panel 融合频率库",
            ),
            _required(
                "hg38/blood_tumor/resource/local_freq_blood/dna_fusion/84fusion.mutation_frequency.txt.idx",
                "84 Panel 融合频率库索引",
            ),
            _required(
                "hg38/blood_tumor/cnvdb/84panel/baseline_zm_kz",
                "84 Panel 备用 CNV 基线",
                "directory",
            ),
        ],
    },
    {
        "id": "blood-396",
        "name": "血液肿瘤 396 Panel",
        "description": "396 基因血液肿瘤检测方案；沿用 624 Panel 结果过滤规则。",
        "resource_version": "",
        "reference": "hg38",
        "workflow_ids": BLOOD_WORKFLOWS,
        "bindings": {
            "bed": "",
            "gene_bed": "",
            "gene_list": "hg38/blood_tumor/bed/kszy_BloodTumor_DNA_panel.624.gene.list",
            "cnvkit_db": "hg38/blood_tumor/cnvkit/396",
        },
        "required_bindings": [
            {
                "key": "bed",
                "label": "396 Panel 捕获区域 BED",
                "kind": "file",
                "basename_includes": ["396"],
            },
            {"key": "gene_bed", "label": "396 Panel 基因区域 BED", "kind": "file"},
            {"key": "gene_list", "label": "396/624 Panel 基因列表", "kind": "file"},
            {
                "key": "cnvkit_db",
                "label": "396 Panel CNVKit 基线",
                "kind": "directory",
            },
        ],
        "required": [
            _required(
                "hg38/blood_tumor/resource/624panel_anno_filter.xls",
                "396/624 Panel 注释过滤库",
            ),
            _required(
                "hg38/blood_tumor/resource/local_freq_blood/dna_fusion/624fusion.mutation_frequency.txt",
                "396/624 Panel 融合频率库",
            ),
            _required(
                "hg38/blood_tumor/resource/local_freq_blood/dna_fusion/624fusion.mutation_frequency.txt.idx",
                "396/624 Panel 融合频率库索引",
            ),
            _required(
                "hg38/blood_tumor/cnvdb/624panel_backbone/v2/baseline_624panel_zm_kz",
                "396 Panel 备用 CNV 基线",
                "directory",
            ),
        ],
    },
    {
        "id": "blood-624",
        "name": "血液肿瘤 624 Panel",
        "description": "624 基因血液肿瘤检测方案；BED 与 gene BED 需按本地生产版本配置。",
        "resource_version": "",
        "reference": "hg38",
        "workflow_ids": BLOOD_WORKFLOWS,
        "bindings": {
            "bed": "",
            "gene_bed": "",
            "gene_list": "hg38/blood_tumor/bed/kszy_BloodTumor_DNA_panel.624.gene.list",
            "cnvkit_db": "hg38/blood_tumor/cnvkit_new0919/624panel",
        },
        "required_bindings": [
            {
                "key": "bed",
                "label": "624 Panel 捕获区域 BED",
                "kind": "file",
                "basename_includes": ["624"],
            },
            {"key": "gene_bed", "label": "624 Panel 基因区域 BED", "kind": "file"},
            {"key": "gene_list", "label": "624 Panel 基因列表", "kind": "file"},
            {
                "key": "cnvkit_db",
                "label": "624 Panel CNVKit 基线",
                "kind": "directory",
            },
        ],
        "required": [
            _required(
                "hg38/blood_tumor/resource/624panel_anno_filter.xls",
                "624 Panel 注释过滤库",
            ),
            _required(
                "hg38/blood_tumor/resource/local_freq_blood/dna_fusion/624fusion.mutation_frequency.txt",
                "624 Panel 融合频率库",
            ),
            _required(
                "hg38/blood_tumor/resource/local_freq_blood/dna_fusion/624fusion.mutation_frequency.txt.idx",
                "624 Panel 融合频率库索引",
            ),
            _required(
                "hg38/blood_tumor/cnvdb/624panel_backbone/v2/baseline_624panel_zm_kz",
                "624 Panel 备用 CNV 基线",
                "directory",
            ),
        ],
    },
)


def with_blood_tumor_resources(document):
    result = copy.deepcopy(document)
    references = {item["id"]: item for item in result["references"]}
    blood_reference = _blood_tumor_hg38_reference()
    if "hg38" in references:
        existing_reference = copy.deepcopy(references["hg38"])
        existing_reference["required"] = [
            item
            for item in existing_reference.get("required", [])
            if item.get("path") not in BLOOD_TUMOR_PANEL_REQUIREMENT_PATHS
        ]
        merged = _merge_reference_requirements(existing_reference, blood_reference)
        result["references"] = [
            merged if item["id"] == "hg38" else item for item in result["references"]
        ]
    else:
        result["references"].append(blood_reference)
    panels = {item["id"]: item for item in result["panels"]}
    for template in BLOOD_TUMOR_PANELS:
        current = panels.get(template["id"])
        if current is None:
            result["panels"].append(copy.deepcopy(template))
            continue
        migrated = copy.deepcopy(current)
        migrated["workflow_ids"] = list(current.get("workflow_ids", []))
        for workflow_id in template["workflow_ids"]:
            if workflow_id not in migrated["workflow_ids"]:
                migrated["workflow_ids"].append(workflow_id)
        migrated_bindings = copy.deepcopy(template["bindings"])
        migrated_bindings.update(current.get("bindings", {}))
        migrated["bindings"] = migrated_bindings
        current_binding_definitions = {
            item["key"]: item for item in current.get("required_bindings", [])
        }
        migrated["required_bindings"] = []
        template_binding_keys = set()
        for item in template["required_bindings"]:
            template_binding_keys.add(item["key"])
            merged_binding = copy.deepcopy(item)
            merged_binding.update(current_binding_definitions.get(item["key"], {}))
            migrated["required_bindings"].append(merged_binding)
        migrated["required_bindings"].extend(
            copy.deepcopy(item)
            for item in current.get("required_bindings", [])
            if item.get("key") not in template_binding_keys
        )
        current_required = {
            item.get("path"): item for item in current.get("required", [])
        }
        template_required_paths = {item["path"] for item in template["required"]}
        migrated["required"] = [
            copy.deepcopy(current_required.get(item["path"], item))
            for item in template["required"]
        ]
        migrated["required"].extend(
            copy.deepcopy(item)
            for item in current.get("required", [])
            if item.get("path") not in template_required_paths
            and item.get("path") not in BLOOD_TUMOR_PANEL_REQUIREMENT_PATHS
        )
        result["panels"] = [
            migrated if item["id"] == template["id"] else item
            for item in result["panels"]
        ]
    return result


class Command(BaseCommand):
    help = "Manage catalog.json in the database and add blood-tumor resource templates."

    def add_arguments(self, parser):
        parser.add_argument("--actor", default="system:migrate-resource-catalog")
        parser.add_argument(
            "--without-blood-tumor",
            action="store_true",
            help="Only migrate catalog.json without adding the built-in blood-tumor templates.",
        )

    def handle(self, *args, **options):
        state = load_catalog_state()
        document = state["document"]
        if not options["without_blood_tumor"]:
            document = with_blood_tumor_resources(document)
        saved = save_catalog(
            document,
            base_version=state["version"],
            base_digest=state["digest"],
            actor=options["actor"],
            note="纳管 catalog.json 并加入血液肿瘤 hg38/Panel 资源模板。",
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"resource-catalog v{saved.version} {saved.digest}; "
                f"{len(saved.document['references'])} references, "
                f"{len(saved.document['panels'])} panels"
            )
        )
