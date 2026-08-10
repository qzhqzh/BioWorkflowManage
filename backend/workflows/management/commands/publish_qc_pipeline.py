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
SOURCE_VERSION = "1.0.8"
TARGET_VERSION = "1.0.9"
ASSET_SLUGS = ("solidtumorsingle", "solidtumorpair")

QC_PIPELINE = r'''version development

task QC_GeneFuse {
    input {
        String sample
        Pair[File, File] fastq
        String mgi = " --fix_mgi_id "
        String docker = "registry.cn-shanghai.aliyuncs.com/kszy-biosoft/fastp:v0.23.1"
        Int cpu = 8
        String memory = "12G"
    }
    command {
        set -vex
        fastp \
            -i ${fastq.left} \
            -I ${fastq.right} \
            -o ${sample}.cleaned.genefuse.r1.fq.gz \
            -O ${sample}.cleaned.genefuse.r2.fq.gz \
            ${mgi} \
            -f 7 \
            -F 7 \
            --cut_right \
            --correction \
            -w ${cpu} \
            -j ${sample}_genefuse.fastp.json \
            -h ${sample}_genefuse.fastp.html
    }
    output {
        Pair[File, File] cleaned_fastq = ("${sample}.cleaned.genefuse.r1.fq.gz", "${sample}.cleaned.genefuse.r2.fq.gz")
        File fastp_json = "${sample}_genefuse.fastp.json"
        File fastp_html = "${sample}_genefuse.fastp.html"
    }
    runtime {
        docker: docker
        cpu: cpu
        memory: memory
    }
}

task QC_Step1 {
    input {
        String sample
        Pair[File, File] fastq
        String mgi = " --fix_mgi_id "
        String fastp_opts = " --cut_tail --correction "
        String fastp_parma = " -f 1 "
        String docker = "registry.cn-shanghai.aliyuncs.com/kszy-biosoft/fastp:v0.23.1"
        Int cpu = 8
        String memory = "12G"
    }
    command {
        set -vex
        fastp \
            -i ${fastq.left} \
            -I ${fastq.right} \
            -o ${sample}.cleaned.step1.r1.fq.gz \
            -O ${sample}.cleaned.step1.r2.fq.gz \
            ${mgi} \
            ${fastp_parma} \
            ${fastp_opts} \
            -w ${cpu} \
            -j ${sample}.step1.fastp.json \
            -h ${sample}.step1.fastp.html
    }
    output {
        Pair[File, File] cleaned_fastq = ("${sample}.cleaned.step1.r1.fq.gz", "${sample}.cleaned.step1.r2.fq.gz")
        File fastp_json = "${sample}.step1.fastp.json"
        File fastp_html = "${sample}.step1.fastp.html"
    }
    runtime {
        docker: docker
        cpu: cpu
        memory: memory
    }
}

task QC_UMI {
    input {
        String sample
        Pair[File, File] fastq
        String fastp_opts = " --cut_tail --correction "
        String fastp_parma = " -U --umi_loc per_read --umi_len 3 --umi_prefix UMI --umi_skip 3 "
        String docker = "registry.cn-shanghai.aliyuncs.com/kszy-biosoft/fastp:v0.23.1"
        Int cpu = 8
        String memory = "12G"
    }
    command {
        set -vex
        fastp \
            -i ${fastq.left} \
            -I ${fastq.right} \
            -o ${sample}.cleaned.r1.fq.gz \
            -O ${sample}.cleaned.r2.fq.gz \
            ${fastp_parma} \
            ${fastp_opts} \
            -w ${cpu} \
            -j ${sample}.fastp.json \
            -h ${sample}.fastp.html
    }
    output {
        Pair[File, File] cleaned_fastq = ("${sample}.cleaned.r1.fq.gz", "${sample}.cleaned.r2.fq.gz")
        File fastp_json = "${sample}.fastp.json"
        File fastp_html = "${sample}.fastp.html"
    }
    runtime {
        docker: docker
        cpu: cpu
        memory: memory
    }
}

workflow QC {
    input {
        String sample
        String sample_type
        File gene_list
        Pair[File, File] fastq
        String docker = "registry.cn-shanghai.aliyuncs.com/kszy-biosoft/fastp:v0.23.1"
        Int cpu = 8
        String memory = "12G"
    }

    String gene_list_name = basename(gene_list)
    Boolean is_brca = sub(gene_list_name, "brca", "") != gene_list_name
    Boolean is_lynch = sub(gene_list_name, "lynch", "") != gene_list_name
    Boolean is_blood = sub(sample_type, "blood", "") != sample_type
    Boolean use_genefuse_cleaned = (is_brca && is_blood) || is_lynch

    call QC_GeneFuse {
        input:
        sample = sample,
        fastq = fastq,
        docker = docker,
        cpu = cpu,
        memory = memory,
    }

    if (!use_genefuse_cleaned) {
        call QC_Step1 {
            input:
            sample = sample,
            fastq = fastq,
            docker = docker,
            cpu = cpu,
            memory = memory,
        }

        call QC_UMI {
            input:
            sample = sample,
            fastq = QC_Step1.cleaned_fastq,
            docker = docker,
            cpu = cpu,
            memory = memory,
        }
    }

    output {
        Pair[File, File] cleaned_fastq = if use_genefuse_cleaned then QC_GeneFuse.cleaned_fastq else select_first([QC_UMI.cleaned_fastq])
        Pair[File, File] cleaned_fastq_genefuse = QC_GeneFuse.cleaned_fastq
        File fastp_json = if use_genefuse_cleaned then QC_GeneFuse.fastp_json else select_first([QC_UMI.fastp_json])
        File fastp_html = if use_genefuse_cleaned then QC_GeneFuse.fastp_html else select_first([QC_UMI.fastp_html])
    }
}

'''


def optimized_qc_source(content: str) -> str:
    _, marker, control = content.partition("task QC_control {")
    if not marker:
        raise CommandError("task/qc.wdl does not contain QC_control.")
    if "task QC {" not in content:
        raise CommandError("task/qc.wdl does not contain the legacy QC task.")
    control = marker + control
    old_resources = '        Int cpu = 12\n        String memory = "24G"'
    if old_resources not in control:
        raise CommandError("QC_control resource defaults changed; refusing migration.")
    control = control.replace(
        old_resources,
        '        Int cpu = 8\n        String memory = "12G"',
        1,
    )
    return QC_PIPELINE + control


class Command(BaseCommand):
    help = (
        "Publish the cacheable QC pipeline and relink the managed solid-tumor assets. "
        "Dry-run by default."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--actor", default="zhuqin")

    def handle(self, *args, **options):
        package = WDLToolPackage.objects.filter(slug=PACKAGE_SLUG).first()
        if package is None:
            raise CommandError(f"Tool package not found: {PACKAGE_SLUG}.")
        source = package.versions.prefetch_related("files").filter(
            version=SOURCE_VERSION
        ).first()
        if source is None:
            raise CommandError(
                f"Tool package version not found: {PACKAGE_SLUG}@{SOURCE_VERSION}."
            )
        files = {item.path: item.content for item in source.files.all()}
        if "task/qc.wdl" not in files:
            raise CommandError("Tool package is missing task/qc.wdl.")
        files["task/qc.wdl"] = optimized_qc_source(files["task/qc.wdl"])
        analysis = analyze_wdl_library(files)
        error_count = analysis.get("summary", {}).get("error_count", 0)
        if error_count:
            messages = "; ".join(
                item.get("message", "unknown error")
                for item in analysis.get("diagnostics", [])[:5]
            )
            raise CommandError(
                f"Optimized tool package still has {error_count} errors: {messages}"
            )
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
                package, source, files, analysis, content_digest, options["actor"]
            )
            for slug in ASSET_SLUGS:
                version = self._relink_asset(slug, target, options["actor"])
                self.stdout.write(self.style.SUCCESS(f"MIGRATED {slug} -> v{version}"))

    def _publish_version(self, package, source, files, analysis, content_digest, actor):
        existing = package.versions.filter(version=TARGET_VERSION).first()
        if existing is not None:
            if existing.digest != content_digest:
                raise CommandError(
                    f"{PACKAGE_SLUG}@{TARGET_VERSION} already has different content."
                )
            return existing
        version = WDLToolPackageVersion.objects.create(
            package=package,
            version=TARGET_VERSION,
            digest=content_digest,
            source_repository=source.source_repository,
            source_revision=source.source_revision,
            note="QC 拆分为可缓存步骤，资源默认值调整为 8 CPU / 12G。",
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
                "reason": "cacheable_qc_pipeline",
                "resources": {"cpu": 8, "memory": "12G"},
            },
        )
        return version

    def _relink_asset(self, slug, target_version, actor):
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
        if any(
            item.package_version_id == target_version.id
            for item in latest.package_references.all()
        ):
            return latest.version

        specifications = []
        replaced = False
        for specification in reference_specs_for_revision(latest):
            if (
                specification.package_version.package_id == target_version.package_id
                and specification.package_version.version == SOURCE_VERSION
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
                f"{slug} does not reference {PACKAGE_SLUG}@{SOURCE_VERSION}."
            )

        local_files, entrypoint = _revision_files(latest)
        effective_files, _ = effective_package_files(local_files, specifications)
        analysis = analyze_wdl_bundle(effective_files, entrypoint)
        error_count = analysis.get("summary", {}).get("error_count", 0)
        if error_count:
            raise CommandError(f"{slug} remains invalid: {error_count} errors.")
        note = f"运行稳定性：QC 拆分并升级到 {PACKAGE_SLUG}@{TARGET_VERSION}。"
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
                "tool_package_version": {
                    "before": SOURCE_VERSION,
                    "after": TARGET_VERSION,
                },
            },
        )
        return revision.version
