from __future__ import annotations

import copy


ANNOVAR_TOOL_ID = "solid_tumor_tools_task_snv_annosnv"
ANNOVAR_ANNOTATION_VERSION = "20180416-annotation.3"

ANNOVAR_OPTIONS = [
    {
        "id": "refgene",
        "label": "RefGene 转录本",
        "description": "基因、转录本、外显子及 HGVS 注释",
        "group": "基因与区域",
        "supported_builds": ["hg19", "hg38"],
    },
    {
        "id": "cytoband",
        "label": "染色体区带",
        "description": "变异所在的染色体区带",
        "group": "基因与区域",
        "supported_builds": ["hg19", "hg38"],
    },
    {
        "id": "clinvar",
        "label": "ClinVar",
        "description": "临床意义与致病性记录",
        "group": "临床与肿瘤",
        "supported_builds": ["hg19", "hg38"],
    },
    {
        "id": "cosmic",
        "label": "COSMIC",
        "description": "肿瘤体细胞变异记录",
        "group": "临床与肿瘤",
        "supported_builds": ["hg19", "hg38"],
    },
    {
        "id": "thousand_genomes",
        "label": "1000 Genomes",
        "description": "千人基因组人群频率",
        "group": "人群频率",
        "supported_builds": ["hg19", "hg38"],
    },
    {
        "id": "exac",
        "label": "ExAC",
        "description": "ExAC 外显子人群频率",
        "group": "人群频率",
        "supported_builds": ["hg19", "hg38"],
    },
    {
        "id": "gnomad_genome",
        "label": "gnomAD Genome",
        "description": "gnomAD 全基因组人群频率",
        "group": "人群频率",
        "supported_builds": ["hg19", "hg38"],
    },
    {
        "id": "dbsnp",
        "label": "dbSNP",
        "description": "已知变异 rs 标识",
        "group": "变异与预测",
        "supported_builds": ["hg19", "hg38"],
    },
    {
        "id": "dbnsfp",
        "label": "dbNSFP",
        "description": "非同义变异功能预测集合",
        "group": "变异与预测",
        "supported_builds": ["hg19", "hg38"],
    },
]

# Keep this order identical to the historical production command so the default
# selection produces the same ANNOVAR columns and remains cache-compatible.
ANNOVAR_CANONICAL_IDS = [
    "refgene",
    "cytoband",
    "clinvar",
    "thousand_genomes",
    "dbsnp",
    "cosmic",
    "dbnsfp",
    "exac",
    "gnomad_genome",
]

ANNOVAR_RESOURCE_PATTERNS = {
    "refgene": {"hg19": ["refGeneWithVer"], "hg38": ["refGeneWithVer"]},
    "cytoband": {"hg19": ["cytoBand"], "hg38": ["cytoBand"]},
    "clinvar": {"hg19": ["clinvar_20220320"], "hg38": ["clinvar_20221231"]},
    "thousand_genomes": {
        "hg19": ["1000g2015aug_all", "ALL.sites.2015_08"],
        "hg38": ["1000g2015aug_all", "ALL.sites.2015_08"],
    },
    "dbsnp": {"hg19": ["avsnp150"], "hg38": ["avsnp150"]},
    "cosmic": {"hg19": ["cosmic96"], "hg38": ["cosmic70"]},
    "dbnsfp": {"hg19": ["dbnsfp42a"], "hg38": ["dbnsfp42a"]},
    "exac": {"hg19": ["exac03"], "hg38": ["exac03"]},
    "gnomad_genome": {
        "hg19": ["gnomad211_genome"],
        "hg38": ["gnomad312_genome"],
    },
}

ANNOVAR_PRESETS = [
    {
        "id": "standard",
        "label": "标准",
        "items": ANNOVAR_CANONICAL_IDS,
    },
    {
        "id": "clinical",
        "label": "临床",
        "items": ["refgene", "clinvar", "dbsnp", "cosmic", "dbnsfp"],
    },
    {
        "id": "population",
        "label": "人群",
        "items": ["refgene", "thousand_genomes", "exac", "gnomad_genome"],
    },
]

ANNOVAR_COMMAND = r'''set -euo pipefail

ref_version="~{ref_version}"
case "$ref_version" in
    hg19|hg38) ;;
    *) echo "Unsupported reference build: $ref_version" >&2; exit 2 ;;
esac

IFS=',' read -r -a selected_items <<< "~{sep=',' annotation_items}"
protocols=()
operations=()
arguments=()

for item in "${selected_items[@]}"; do
    protocol=""
    operation=""
    argument=""
    case "$item" in
        refgene)
            protocol="refGeneWithVer"; operation="g"; argument="-hgvs" ;;
        cytoband)
            protocol="cytoBand"; operation="r" ;;
        clinvar)
            [[ "$ref_version" == "hg19" ]] && protocol="clinvar_20220320" || protocol="clinvar_20221231"
            operation="f" ;;
        thousand_genomes)
            protocol="1000g2015aug_all"; operation="f" ;;
        dbsnp)
            protocol="avsnp150"; operation="f" ;;
        cosmic)
            [[ "$ref_version" == "hg19" ]] && protocol="cosmic96" || protocol="cosmic70"
            operation="f" ;;
        dbnsfp)
            protocol="dbnsfp42a"; operation="f" ;;
        exac)
            protocol="exac03"; operation="f" ;;
        gnomad_genome)
            [[ "$ref_version" == "hg19" ]] && protocol="gnomad211_genome" || protocol="gnomad312_genome"
            operation="f" ;;
        *) echo "Unknown annotation item: $item" >&2; exit 2 ;;
    esac
    protocols+=("$protocol")
    operations+=("$operation")
    arguments+=("$argument")
done

if [[ ${#protocols[@]} -eq 0 ]]; then
    echo "At least one annotation item is required" >&2
    exit 2
fi

join_csv() {
    local IFS=,
    echo "$*"
}

protocol_csv="$(join_csv "${protocols[@]}")"
operation_csv="$(join_csv "${operations[@]}")"
argument_csv="$(join_csv "${arguments[@]}")"
selected_csv="$(join_csv "${selected_items[@]}")"

printf '{"schema_version":"1.0","engine":"annovar","build":"%s","selected_items_csv":"%s","protocols":"%s","operations":"%s"}\n' \
    "$ref_version" "$selected_csv" "$protocol_csv" "$operation_csv" > annotation_manifest.json

perl /home/TOOLS/tools/annovar/current/bin/table_annovar.pl \
    "~{vcf}" "~{humandb}" \
    -buildver "$ref_version" \
    -out "~{sample}.var" \
    -remove \
    -protocol "$protocol_csv" \
    -operation "$operation_csv" \
    -nastring . \
    -vcfinput \
    --polish \
    --argument "$argument_csv"
'''


def enhance_annosnv_spec(source: dict) -> dict:
    spec = copy.deepcopy(source)
    spec["tool_version"] = ANNOVAR_ANNOTATION_VERSION
    spec["display_name"] = "AnnoSNV 可选注释"
    spec["description"] = "ANNOVAR SNV 注释；注释项可按需选择，默认保持历史九项全选。"
    spec["category"] = "variant_annotation"
    spec["task_kind"] = "annotation"
    options_by_id = {item["id"]: item for item in ANNOVAR_OPTIONS}
    spec["annotation"] = {
        "selector_input": "annotation_items",
        "options": [options_by_id[item] for item in ANNOVAR_CANONICAL_IDS],
        "presets": ANNOVAR_PRESETS,
    }

    inputs = [
        item
        for item in spec.get("inputs", [])
        if item.get("name") not in {"annotation_items", "docker", "cpu", "memory"}
    ]
    for item in inputs:
        if item.get("name") == "humandb":
            item["label"] = "ANNOVAR 数据库目录"
            item["wdl_type"] = "Directory"
            item["semantic_type"] = "bio.annotation.database_dir"
        elif item.get("name") == "ref_version":
            item["label"] = "参考版本"
            item["semantic_type"] = "bio.reference.build"
            item["constraints"] = {"enum": ["hg19", "hg38"]}
        elif item.get("name") == "sample":
            item["label"] = "样本编号"
        elif item.get("name") == "vcf":
            item["label"] = "待注释 VCF"
            item["semantic_type"] = "bio.variant.vcf"
    selector = {
        "name": "annotation_items",
        "label": "注释项",
        "wdl_type": "Array[String]",
        "semantic_type": "bio.annotation.selection",
        "required": False,
        "default": ANNOVAR_CANONICAL_IDS,
        "constraints": {"enum": ANNOVAR_CANONICAL_IDS},
        "description": "选择本次需要写入 VCF 的注释数据库；默认全部启用。",
    }
    insert_at = next(
        (index + 1 for index, item in enumerate(inputs) if item.get("name") == "vcf"),
        len(inputs),
    )
    inputs.insert(insert_at, selector)
    spec["inputs"] = inputs
    spec["command"] = {
        "shell": "bash",
        "strict_mode": False,
        "template": ANNOVAR_COMMAND,
    }

    outputs = [item for item in spec.get("outputs", []) if item.get("name") != "annotation_manifest"]
    for item in outputs:
        if item.get("name") == "anno_vcf":
            item["label"] = "注释后 VCF"
            item["semantic_type"] = "bio.variant.vcf.annotated"
    outputs.append(
        {
            "name": "annotation_manifest",
            "label": "注释执行清单",
            "wdl_type": "File",
            "semantic_type": "bio.annotation.manifest",
            "optional": False,
            "capture": {"mode": "path", "value": "annotation_manifest.json"},
        }
    )
    spec["outputs"] = outputs

    metadata = spec.setdefault("metadata", {})
    metadata["tags"] = list(dict.fromkeys([*metadata.get("tags", []), "annotation"]))
    metadata["migration_warnings"] = [
        warning
        for warning in metadata.get("migration_warnings", [])
        if "Directory" not in warning
    ]
    return spec
