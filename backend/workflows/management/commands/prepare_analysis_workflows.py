from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from workflows.models import (
    WDLAuditEvent,
    WDLAsset,
    WDLSourceRevision,
    WDLToolPackage,
    WDLToolPackageAuditEvent,
    WDLToolPackageFile,
    WDLToolPackageVersion,
)
from workflows.wdl_assets import _digest, _revision_files, _save_source_files
from workflows.wdl_packages import (
    analyze_wdl_bundle,
    analyze_wdl_library,
    digest,
    package_digest,
)
from workflows.wdl_source_references import (
    PackageReferenceSpec,
    effective_package_files,
    persist_reference_specs,
    reference_specs_for_revision,
)


PACKAGE_SLUG = "solid-tumor-tools"
SOURCE_VERSION = "1.0.0"
TARGET_VERSION = "1.0.8"
ASSET_SLUGS = ("solidtumorsingle", "solidtumorpair")

INTEGRATE_XLS_DECLARATIONS = """        String fasta = "${reference}/${ref_version}.simp.fa"
        String key_site = "${resource}/combine.tsv"
        String gene_transcript_matchup = "${resource}/sorted.gene.tx.txt"
        String hotspot_gene = "${resource}/hotspot_gene-20230227.xls"
        String tumor_gene = "${resource}/tumor-gene-20241016.xlsx"
        String anno_db = "${localdb}/local_freq.zip"
        String ensembl_genbank = "${resource}/ensembltogenbank.xls"
        String cnv_tumor_gene = "${resource}/cnv_tumor_gene.2024-2.xlsx"
        String dosage = "${resource}/dosage_sensitivity_gene.xlsx"
        String fusion_gene_list = "${resource}/zy.DNA.fusion.gene.list"
        String gene_alias = "${resource}/gene_alias.xls"
        String cnv_filter_genelist = "${resource}/zy.cnv.gene.list"
        String sv_rec1 = '{if($3=="-" || $6=="-")print}'
        String sv_rec2 = '{if($3!="-" && $6!="-")print}'
"""

COLLECT_CLINVAR_COMPAT = """        python3 - ${anno_vcf} ${sample}.var.${ref_version}_multianno.vcf <<'PY'
import sys

required = ("ONCDN", "ONCDISDB", "ONCREVSTAT", "ONCSIG")
present = set()
with open(sys.argv[1], encoding="utf-8") as source, open(
    sys.argv[2], "w", encoding="utf-8"
) as target:
    for line in source:
        if line.startswith("##INFO=<ID="):
            present.add(line.split("=", 2)[2].split(",", 1)[0])
        if line.startswith("#CHROM"):
            for key in required:
                if key not in present:
                    target.write(
                        '##INFO=<ID=%s,Number=.,Type=String,Description="Unavailable in legacy ClinVar; compatibility placeholder">\\n'
                        % key
                    )
        if line.startswith("#"):
            target.write(line)
            continue
        columns = line.rstrip("\\n").split("\\t")
        info = columns[7]
        keys = set(item.split("=", 1)[0] for item in info.split(";"))
        missing = [key for key in required if key not in keys]
        if missing:
            prefix = "" if info in ("", ".") else info + ";"
            columns[7] = prefix + ";".join(key + "=." for key in missing)
        target.write("\\t".join(columns) + "\\n")
PY

        cp /scripts/pre_select_v2.py pre_select_v2.compat.py && \\
        python3 -c 'from pathlib import Path; p=Path("pre_select_v2.compat.py"); s=p.read_text(); old="            if val_tmp_transvar_input:\\n                try:\\n"; new="            if val_tmp_transvar_input:\\n                tsaa_list = None\\n                try:\\n"; assert s.count(old) == 1; p.write_text(s.replace(old, new, 1))' && \\

        python3 - ${cnv_filter_genelist} cnv_filter.compat.xlsx <<'PY'
import shutil
import sys
import zipfile

import pandas as pd

source, target = sys.argv[1:]
if zipfile.is_zipfile(source):
    shutil.copyfile(source, target)
else:
    with open(source, encoding="utf-8") as stream:
        genes = [line.strip() for line in stream if line.strip()]
    with pd.ExcelWriter(target) as writer:
        pd.DataFrame(genes, columns=["cnv_gene"]).to_excel(
            writer, sheet_name="cnvfilter_genelist", index=False
        )
        pd.DataFrame(columns=["cnv_keygene"]).to_excel(
            writer, sheet_name="cnvfilter_keygenelist", index=False
        )
PY

"""


def corrected_rule_source(content: str) -> str:
    prefix, separator, integrate = content.partition("task IntegrateXls {")
    if not separator:
        raise CommandError("task/rule.wdl does not contain IntegrateXls.")
    if "String key_site =" not in integrate.split("command {", 1)[0]:
        marker = '    String modify_varanno = "${resource}/modify_VARAnnovar.xls"\n'
        if marker not in integrate:
            raise CommandError("IntegrateXls declaration anchor changed; refusing repair.")
        integrate = integrate.replace(
            marker,
            marker + INTEGRATE_XLS_DECLARATIONS,
            1,
        )
        content = prefix + separator + integrate

    if "Unavailable in legacy ClinVar" not in content:
        command_marker = "        python3 /scripts/pre_select_v2.py \\\n"
        input_marker = "        -f ${anno_vcf} \\\n"
        if command_marker not in content or input_marker not in content:
            raise CommandError("Collect command anchor changed; refusing repair.")
        content = content.replace(
            command_marker,
            COLLECT_CLINVAR_COMPAT + "        python3 pre_select_v2.compat.py \\\n",
            1,
        ).replace(
            input_marker,
            "        -f ${sample}.var.${ref_version}_multianno.vcf \\\n",
            1,
        )

    collect, marker, remainder = content.partition("task IntegrateXls {")
    fasta_marker = '        String fasta = "${reference}/${ref_version}.simp.fa"\n'
    if "File fasta_fai" not in collect:
        if fasta_marker not in collect:
            raise CommandError("Collect FASTA declaration anchor changed; refusing repair.")
        collect = collect.replace(
            fasta_marker,
            '        File fasta = "${reference}/${ref_version}.simp.fa"\n'
            '        File fasta_fai = "${reference}/${ref_version}.simp.fa.fai"\n',
            1,
        )
        config_marker = "        transvar config -k reference -v ${fasta} --refversion ${if (is_hg19) then \"hg19\" else \"hg38\"} && \\\n"
        if config_marker not in collect:
            raise CommandError("Collect transvar config anchor changed; refusing repair.")
        collect = collect.replace(
            config_marker,
            "        test -r ${fasta} && test -r ${fasta_fai} && \\\n"
            + config_marker,
            1,
        )
    cnv_filter_marker = "            -cg ${cnv_filter_genelist} \\\n"
    if cnv_filter_marker not in collect:
        raise CommandError("Collect CNV filter argument anchor changed; refusing repair.")
    collect = collect.replace(
        cnv_filter_marker,
        "            -cg ../cnv_filter.compat.xlsx \\\n",
        1,
    )
    content = collect + marker + remainder
    return content


class Command(BaseCommand):
    help = (
        "Publish a statically valid solid-tumor-tools version for analysis runs "
        "and relink the two managed SolidTumor assets. Dry-run by default."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--actor", default="zhuqin")

    def handle(self, *args, **options):
        package = WDLToolPackage.objects.filter(slug=PACKAGE_SLUG).first()
        if package is None:
            raise CommandError(f"Tool package not found: {PACKAGE_SLUG}.")
        source = package.versions.prefetch_related("files").filter(version=SOURCE_VERSION).first()
        if source is None:
            raise CommandError(f"Tool package version not found: {PACKAGE_SLUG}@{SOURCE_VERSION}.")
        files = {item.path: item.content for item in source.files.all()}
        if "task/rule.wdl" not in files:
            raise CommandError("Tool package is missing task/rule.wdl.")
        files["task/rule.wdl"] = corrected_rule_source(files["task/rule.wdl"])
        analysis = analyze_wdl_library(files)
        error_count = analysis.get("summary", {}).get("error_count", 0)
        if error_count:
            messages = "; ".join(
                item.get("message", "unknown error")
                for item in analysis.get("diagnostics", [])[:5]
            )
            raise CommandError(f"Repaired tool package still has {error_count} errors: {messages}")
        content_digest = package_digest(files)
        self.stdout.write(
            f"READY {PACKAGE_SLUG}@{TARGET_VERSION}: {len(files)} files, "
            f"{analysis['summary']['task_count']} tasks, {content_digest}"
        )

        if not options["apply"]:
            for slug in ASSET_SLUGS:
                self.stdout.write(f"DRY-RUN would relink {slug} to {TARGET_VERSION}")
            return

        with transaction.atomic():
            target = self._publish_version(
                package,
                source,
                files,
                analysis,
                content_digest,
                options["actor"],
            )
            for slug in ASSET_SLUGS:
                version = self._relink_asset(slug, source, target, options["actor"])
                self.stdout.write(self.style.SUCCESS(f"MIGRATED {slug} -> v{version}"))

    def _publish_version(self, package, source, files, analysis, content_digest, actor):
        existing = package.versions.filter(version=TARGET_VERSION).first()
        if existing is not None:
            if existing.digest != content_digest:
                raise CommandError(f"{PACKAGE_SLUG}@{TARGET_VERSION} already has different content.")
            return existing
        version = WDLToolPackageVersion.objects.create(
            package=package,
            version=TARGET_VERSION,
            digest=content_digest,
            source_repository=source.source_repository,
            source_revision=source.source_revision,
            note="补齐旧版 ClinVar 字段，并兼容 transvar 回退与旧版 CNV 基因列表。",
            actor=actor,
            analysis=analysis,
        )
        file_analysis = {item["path"]: item for item in analysis.get("files", [])}
        WDLToolPackageFile.objects.bulk_create(
            [
                WDLToolPackageFile(
                    package_version=version,
                    path=path,
                    content=content,
                    digest=file_analysis.get(path, {}).get("digest") or digest(content),
                    analysis=file_analysis.get(path, {}),
                )
                for path, content in sorted(files.items())
            ]
        )
        package.updated_at = timezone.now()
        package.save(update_fields=["updated_at"])
        WDLToolPackageAuditEvent.objects.create(
            package=package,
            package_version=version,
            action="publish_version",
            actor=actor,
            note=version.note,
            changes={
                "version": TARGET_VERSION,
                "digest": content_digest,
                "file_count": len(files),
                "task_count": analysis["summary"]["task_count"],
                "reason": "legacy_clinvar_collect_compatibility",
            },
        )
        return version

    def _relink_asset(self, slug, source_version, target_version, actor):
        asset = WDLAsset.objects.select_for_update().filter(slug=slug).first()
        if asset is None:
            raise CommandError(f"WDL asset not found: {slug}.")
        latest = asset.source_revisions.prefetch_related(
            "files",
            "package_references__package_version__package",
            "package_references__package_version__files",
        ).first()
        if latest is None:
            raise CommandError(f"WDL asset has no revision: {slug}.")
        existing_specs = reference_specs_for_revision(latest)
        if any(item.package_version_id == target_version.id for item in latest.package_references.all()):
            return latest.version
        replaced = False
        specifications = []
        for specification in existing_specs:
            if (
                specification.package_version.package_id == target_version.package_id
                and specification.package_version.version
                in {
                    SOURCE_VERSION,
                    "1.0.1",
                    "1.0.2",
                    "1.0.3",
                    "1.0.4",
                    "1.0.5",
                    "1.0.6",
                    "1.0.7",
                }
            ):
                specifications.append(
                    PackageReferenceSpec(
                        package_version=target_version,
                        mount_prefix=specification.mount_prefix,
                        digest=target_version.digest,
                    )
                )
                replaced = True
            else:
                specifications.append(specification)
        if not replaced:
            raise CommandError(
                f"{slug} does not reference a migratable {PACKAGE_SLUG} version."
            )

        local_files, entrypoint = _revision_files(latest)
        effective_files, _ = effective_package_files(local_files, specifications)
        analysis = analyze_wdl_bundle(effective_files, entrypoint)
        error_count = analysis.get("summary", {}).get("error_count", 0)
        if error_count:
            raise CommandError(f"{slug} remains invalid after relink: {error_count} errors.")
        note = f"运行分析准备：工具包固定升级到 {PACKAGE_SLUG}@{TARGET_VERSION}。"
        revision = WDLSourceRevision.objects.create(
            asset=asset,
            version=latest.version + 1,
            operation=WDLSourceRevision.Operation.PACKAGE_LINK,
            content=local_files[entrypoint],
            digest=_digest(local_files[entrypoint]),
            diff="",
            note=note,
            actor=actor,
            analysis=analysis,
        )
        _save_source_files(revision, local_files, entrypoint, analysis)
        persist_reference_specs(revision, specifications)
        WDLAuditEvent.objects.create(
            asset=asset,
            revision=revision,
            action=WDLSourceRevision.Operation.PACKAGE_LINK,
            actor=actor,
            note=note,
            changes={
                "revision": {"before": latest.version, "after": revision.version},
                "tool_package_version": {"before": SOURCE_VERSION, "after": TARGET_VERSION},
            },
        )
        return revision.version
