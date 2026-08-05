from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from workflows.models import WDLAuditEvent, WDLAsset, WDLSourceRevision
from workflows.wdl_assets import (
    _bundle_diff,
    _digest,
    _revision_files,
    _save_source_files,
    _set_tags,
)
from workflows.wdl_packages import analyze_wdl_bundle, normalize_bundle_files


ASSETS = (
    {
        "entrypoint": "SolidTumorWithMRD.wdl",
        "name": "SolidTumorWorkflow",
        "slug": "solid-tumor-workflow",
        "description": "实体瘤 MRD 历史生产 WDL；入口与全部本地依赖按版本保存。",
        "tags": ["实体瘤", "MRD", "hg19", "hg38"],
        "preserve_entry": True,
    },
    {
        "entrypoint": "BloodTumorWithMRD.wdl",
        "name": "BloodTumorWorkflow",
        "slug": "blood-tumor-workflow",
        "description": "血液肿瘤 MRD 历史生产 WDL；入口与全部本地依赖按版本保存。",
        "tags": ["血液肿瘤", "MRD", "hg19", "hg38"],
        "preserve_entry": False,
    },
)


class Command(BaseCommand):
    help = "Import the Solid/Blood tumor_wdl dependency closures as managed WDL assets."

    def add_arguments(self, parser):
        parser.add_argument("--source-dir", required=True)
        parser.add_argument("--repository", required=True)
        parser.add_argument("--revision", required=True)
        parser.add_argument("--actor", default="zhuqin")

    def handle(self, *args, **options):
        source_dir = Path(options["source_dir"]).resolve()
        if not source_dir.is_dir():
            raise CommandError(f"WDL source directory does not exist: {source_dir}")
        all_files = {
            str(path.relative_to(source_dir)).replace("\\", "/"): path.read_text(encoding="utf-8")
            for path in source_dir.rglob("*.wdl")
            if path.is_file()
        }
        if not all_files:
            raise CommandError("No WDL files were found in the source directory.")

        results = []
        for definition in ASSETS:
            entrypoint = definition["entrypoint"]
            if entrypoint not in all_files:
                raise CommandError(f"Missing entrypoint: {entrypoint}")
            full_files, _ = normalize_bundle_files(
                [{"path": path, "content": content} for path, content in all_files.items()],
                entrypoint,
            )
            full_analysis = analyze_wdl_bundle(full_files, entrypoint)
            reachable = {
                item["path"] for item in full_analysis.get("files", []) if item.get("reachable")
            }
            files = {path: full_files[path] for path in sorted(reachable)}
            result = self._import_asset(definition, files, options)
            results.append(result)

        self.stdout.write(self.style.SUCCESS("; ".join(results)))

    @transaction.atomic
    def _import_asset(self, definition, files, options):
        entrypoint = definition["entrypoint"]
        asset = (
            WDLAsset.objects.select_for_update()
            .filter(
                Q(name__iexact=definition["name"])
                | Q(source_filename__iexact=entrypoint)
                | Q(slug=definition["slug"])
            )
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
                source_revision=options["revision"],
                created_by=options["actor"],
            )

        latest = asset.source_revisions.prefetch_related("files").first()
        before_files, _ = _revision_files(latest)
        preserved_entry = False
        if latest is not None and definition["preserve_entry"]:
            files[entrypoint] = latest.content
            preserved_entry = True
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
                note=(
                    f"从 tumor_wdl {options['revision']} 导入完整依赖；"
                    + ("保留工作台现有入口源码。" if preserved_entry else "保留仓库入口源码。")
                ),
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
                    "repository_revision": options["revision"],
                    "package": {"entrypoint": entrypoint, "file_count": len(files)},
                },
            )

        asset.name = definition["name"]
        asset.description = asset.description or definition["description"]
        asset.source_filename = entrypoint
        asset.source_repository = options["repository"]
        asset.source_revision = options["revision"]
        asset.save()
        _set_tags(asset, list(dict.fromkeys([*asset.tags.values_list("name", flat=True), *definition["tags"]])))
        return f"{asset.slug}=v{version} ({len(files)} files, {'created' if created else 'updated'})"
