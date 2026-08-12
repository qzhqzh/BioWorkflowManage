from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from workflows.models import WDLAuditEvent, WDLAsset, WDLSourceRevision
from workflows.wdl_assets import (
    _bundle_diff,
    _digest,
    _revision_files,
    _save_source_files,
    _set_tags,
)
from workflows.wdl_packages import (
    analyze_wdl_bundle,
    normalize_bundle_files,
    package_digest,
)


ASSETS = (
    {
        "directory": "single",
        "entrypoint": "TumorBloodSingle.wdl",
        "name": "血液肿瘤单样本正式流程",
        "slug": "tumor-blood-single-production",
        "description": "血液肿瘤单样本正式 WDL；入口与 SolidTumorSingle 依赖按版本保存。",
        "tags": ["血液肿瘤", "单样本", "hg38", "正式流程"],
    },
    {
        "directory": "pair",
        "entrypoint": "TumorBloodPair.wdl",
        "name": "血液肿瘤配对样本正式流程",
        "slug": "tumor-blood-pair-production",
        "description": "血液肿瘤配对样本正式 WDL；入口与 SolidTumorSingle 依赖按版本保存。",
        "tags": ["血液肿瘤", "配对样本", "hg38", "正式流程"],
    },
)


class Command(BaseCommand):
    help = "Import the production blood-tumor single and pair WDL bundles."

    def add_arguments(self, parser):
        parser.add_argument("--source-dir", required=True)
        parser.add_argument(
            "--repository",
            default="github.com/qzhqzh/BioWorkflowManage:test/血液肿瘤最新流程",
        )
        parser.add_argument(
            "--revision",
            default="",
            help="Source revision label; defaults to the SHA-256 digest of each WDL bundle.",
        )
        parser.add_argument("--actor", default="zhuqin")

    def handle(self, *args, **options):
        source_dir = Path(options["source_dir"]).resolve()
        if not source_dir.is_dir():
            raise CommandError(f"WDL source directory does not exist: {source_dir}")

        results = []
        for definition in ASSETS:
            bundle_dir = source_dir / definition["directory"]
            if not bundle_dir.is_dir():
                raise CommandError(f"Missing WDL bundle directory: {bundle_dir}")
            files = {
                path.name: path.read_text(encoding="utf-8")
                for path in bundle_dir.glob("*.wdl")
                if path.is_file()
            }
            entrypoint = definition["entrypoint"]
            if entrypoint not in files:
                raise CommandError(f"Missing entrypoint: {bundle_dir / entrypoint}")
            files, _ = normalize_bundle_files(
                [{"path": path, "content": content} for path, content in files.items()],
                entrypoint,
            )
            analysis = analyze_wdl_bundle(files, entrypoint)
            reachable = {
                item["path"] for item in analysis.get("files", []) if item.get("reachable")
            }
            files = {path: files[path] for path in sorted(reachable)}
            results.append(self._import_asset(definition, files, options))

        self.stdout.write(self.style.SUCCESS("; ".join(results)))

    @transaction.atomic
    def _import_asset(self, definition, files, options):
        entrypoint = definition["entrypoint"]
        source_revision = options["revision"] or package_digest(files)
        asset = (
            WDLAsset.objects.select_for_update()
            .filter(slug=definition["slug"])
            .first()
        )
        created = asset is None
        if asset is None:
            asset = WDLAsset.objects.create(
                slug=definition["slug"],
                name=definition["name"],
                description=definition["description"],
                source_filename=entrypoint,
                source_repository=options["repository"],
                source_revision=source_revision,
                created_by=options["actor"],
            )

        latest = asset.source_revisions.prefetch_related("files").first()
        before_files, _ = _revision_files(latest)
        analysis = analyze_wdl_bundle(files, entrypoint)
        if before_files == files:
            version = latest.version
        else:
            version = (latest.version if latest else 0) + 1
            diff = _bundle_diff(before_files, files)
            revision = WDLSourceRevision.objects.create(
                asset=asset,
                version=version,
                operation=WDLSourceRevision.Operation.IMPORT,
                content=files[entrypoint],
                digest=_digest(files[entrypoint]),
                diff=diff,
                note=f"导入正式流程 {source_revision} 的完整 WDL 依赖。",
                actor=options["actor"],
                analysis=analysis,
            )
            _save_source_files(revision, files, entrypoint, analysis)
            WDLAuditEvent.objects.create(
                asset=asset,
                revision=revision,
                action="import",
                actor=options["actor"],
                note=revision.note,
                diff=diff,
                changes={
                    "repository": options["repository"],
                    "repository_revision": source_revision,
                    "package": {"entrypoint": entrypoint, "file_count": len(files)},
                },
            )

        if created:
            _set_tags(asset, definition["tags"])
        return f"{asset.slug}=v{version} ({len(files)} files, {'created' if created else 'updated'})"
