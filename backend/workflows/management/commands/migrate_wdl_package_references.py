from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from workflows.models import (
    WDLAuditEvent,
    WDLAsset,
    WDLSourceRevision,
    WDLToolPackage,
    WDLToolPackageVersion,
)
from workflows.wdl_assets import _digest, _revision_files, _save_source_files
from workflows.wdl_packages import analyze_wdl_bundle, digest
from workflows.wdl_source_references import (
    PackageReferenceSpec,
    effective_package_files,
    persist_reference_specs,
)


class Command(BaseCommand):
    help = (
        "Replace duplicated WDL task modules in the latest asset revision with "
        "immutable tool-package version references. Dry-run by default."
    )

    def add_arguments(self, parser):
        parser.add_argument("--package", dest="package_slug")
        parser.add_argument("--package-version", dest="package_version")
        parser.add_argument("--actor", default="zhuqin")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        if options["package_version"] and not options["package_slug"]:
            raise CommandError("--package-version requires --package.")

        package_versions = self._package_versions(options)
        if not package_versions:
            raise CommandError("No active WDL tool-package versions matched the filters.")

        mode = "APPLY" if options["apply"] else "DRY-RUN"
        migrated = 0
        candidates = 0
        skipped = 0
        self.stdout.write(f"[{mode}] checking {len(package_versions)} package version(s)")

        for asset in WDLAsset.objects.order_by("slug"):
            result = self._inspect_asset(asset, package_versions)
            if result["status"] == "skip":
                skipped += 1
                self.stdout.write(f"SKIP {asset.slug}: {result['reason']}")
                continue
            candidates += 1
            packages = ", ".join(
                f"{spec.package_version.package.slug}@{spec.package_version.version}"
                for spec in result["specifications"]
            )
            removed = ", ".join(result["matched_paths"])
            self.stdout.write(
                f"CANDIDATE {asset.slug} v{result['revision'].version}: "
                f"{packages}; remove [{removed}]"
            )
            if options["apply"]:
                version = self._migrate_asset(
                    asset.pk,
                    result["revision"].pk,
                    result["specifications"],
                    options["actor"],
                )
                migrated += 1
                self.stdout.write(self.style.SUCCESS(f"MIGRATED {asset.slug} -> v{version}"))

        summary = (
            f"{mode}: {candidates} candidate(s), {migrated} migrated, "
            f"{skipped} skipped"
        )
        self.stdout.write(self.style.SUCCESS(summary) if options["apply"] else summary)

    def _package_versions(self, options) -> list[WDLToolPackageVersion]:
        queryset = (
            WDLToolPackageVersion.objects.select_related("package")
            .prefetch_related("files")
            .filter(package__lifecycle=WDLToolPackage.Lifecycle.ACTIVE)
        )
        if options["package_slug"]:
            queryset = queryset.filter(package__slug=options["package_slug"])
        if options["package_version"]:
            queryset = queryset.filter(version=options["package_version"])
        versions = list(queryset)
        if options["package_slug"] or options["package_version"]:
            return versions

        latest_by_package = {}
        for item in versions:
            latest_by_package.setdefault(item.package_id, item)
        return list(latest_by_package.values())

    def _inspect_asset(self, asset, package_versions):
        revision = (
            asset.source_revisions.prefetch_related(
                "files",
                "package_references__package_version__package",
                "package_references__package_version__files",
            )
            .first()
        )
        if revision is None:
            return {"status": "skip", "reason": "no source revision"}
        if revision.package_references.exists():
            return {"status": "skip", "reason": "latest revision already has package references"}

        files, entrypoint = _revision_files(revision)
        file_digests = {path: digest(content) for path, content in files.items()}
        matches = []
        for package_version in package_versions:
            package_files = list(package_version.files.all())
            paths = {item.path for item in package_files}
            if entrypoint in paths or not paths:
                continue
            if all(
                file_digests.get(item.path) == (item.digest or digest(item.content))
                for item in package_files
            ):
                matches.append(
                    (
                        PackageReferenceSpec(
                            package_version=package_version,
                            mount_prefix="",
                            digest=package_version.digest,
                        ),
                        paths,
                    )
                )
        if not matches:
            return {"status": "skip", "reason": "no exact package file match"}

        occupied = set()
        for _, paths in matches:
            overlap = occupied & paths
            if overlap:
                return {
                    "status": "skip",
                    "reason": "ambiguous overlapping package matches: "
                    + ", ".join(sorted(overlap)),
                }
            occupied.update(paths)
        return {
            "status": "candidate",
            "revision": revision,
            "entrypoint": entrypoint,
            "specifications": [item[0] for item in matches],
            "matched_paths": sorted(occupied),
        }

    @transaction.atomic
    def _migrate_asset(self, asset_id, expected_revision_id, specifications, actor):
        asset = WDLAsset.objects.select_for_update().get(pk=asset_id)
        latest = (
            asset.source_revisions.prefetch_related(
                "files",
                "package_references__package_version__package",
                "package_references__package_version__files",
            )
            .first()
        )
        if latest is None or latest.pk != expected_revision_id:
            raise CommandError(f"{asset.slug} changed after dry-run inspection; retry.")
        if latest.package_references.exists():
            return latest.version

        files, entrypoint = _revision_files(latest)
        matched_paths = {
            item.path
            for specification in specifications
            for item in specification.package_version.files.all()
        }
        for specification in specifications:
            for package_file in specification.package_version.files.all():
                if digest(files.get(package_file.path, "")) != (
                    package_file.digest or digest(package_file.content)
                ):
                    raise CommandError(
                        f"{asset.slug} no longer exactly matches "
                        f"{specification.package_version.package.slug}@"
                        f"{specification.package_version.version}."
                    )

        local_files = {
            path: content for path, content in files.items() if path not in matched_paths
        }
        if entrypoint not in local_files:
            raise CommandError(f"Refusing to remove entrypoint for {asset.slug}.")
        effective_files, _ = effective_package_files(local_files, specifications)
        analysis = analyze_wdl_bundle(effective_files, entrypoint)
        package_names = ", ".join(
            f"{item.package_version.package.slug}@{item.package_version.version}"
            for item in specifications
        )
        note = f"将重复 Task 固定引用到 {package_names}。"
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
        asset.source_filename = entrypoint
        asset.save(update_fields=["source_filename", "updated_at"])
        WDLAuditEvent.objects.create(
            asset=asset,
            revision=revision,
            action=WDLSourceRevision.Operation.PACKAGE_LINK,
            actor=actor,
            note=note,
            changes={
                "revision": {"before": latest.version, "after": revision.version},
                "package_references": [
                    {
                        "package": item.package_version.package.slug,
                        "version": item.package_version.version,
                        "digest": item.digest,
                        "mount_prefix": item.mount_prefix,
                    }
                    for item in specifications
                ],
                "local_file_count": {
                    "before": len(files),
                    "after": len(local_files),
                },
                "matched_files": sorted(matched_paths),
            },
        )
        return revision.version
