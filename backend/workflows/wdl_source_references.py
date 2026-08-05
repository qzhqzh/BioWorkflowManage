from __future__ import annotations

import posixpath
from dataclasses import dataclass
from pathlib import PurePosixPath

from django.db.models import F, OuterRef, Subquery

from .models import (
    WDLSourcePackageReference,
    WDLSourceRevision,
    WDLToolPackage,
    WDLToolPackageVersion,
)
from .wdl_packages import WDLPackageError, normalize_package_path


@dataclass(frozen=True)
class PackageReferenceSpec:
    package_version: WDLToolPackageVersion
    mount_prefix: str
    digest: str


def normalize_mount_prefix(value: str) -> str:
    candidate = str(value or "").strip().replace("\\", "/").strip("/")
    if not candidate:
        return ""
    path = PurePosixPath(candidate)
    parts = [part for part in path.parts if part not in {"", "."}]
    if (
        path.is_absolute()
        or not parts
        or any(part == ".." for part in parts)
        or "\x00" in candidate
    ):
        raise WDLPackageError(
            "WDL_PACKAGE_MOUNT_INVALID",
            f"Invalid tool package mount path: {candidate}.",
        )
    normalized = "/".join(parts)
    if len(normalized) > 384:
        raise WDLPackageError(
            "WDL_PACKAGE_MOUNT_INVALID",
            "Tool package mount path exceeds 384 characters.",
        )
    return normalized


def reference_specs_for_revision(
    revision: WDLSourceRevision | None,
) -> list[PackageReferenceSpec]:
    if revision is None:
        return []
    references = revision.package_references.select_related(
        "package_version__package"
    ).prefetch_related("package_version__files")
    return [
        PackageReferenceSpec(
            package_version=reference.package_version,
            mount_prefix=reference.mount_prefix,
            digest=reference.digest,
        )
        for reference in references
    ]


def parse_reference_specs(
    value,
    *,
    fallback_revision: WDLSourceRevision | None = None,
) -> list[PackageReferenceSpec]:
    if value is None:
        return reference_specs_for_revision(fallback_revision)
    if not isinstance(value, list):
        raise WDLPackageError(
            "WDL_PACKAGE_REFERENCES_INVALID",
            "package_references must be an array.",
        )

    existing = {
        (
            reference.package_version.package.slug,
            reference.package_version.version,
            reference.mount_prefix,
        )
        for reference in (fallback_revision.package_references.select_related(
            "package_version__package"
        ) if fallback_revision else [])
    }
    specifications: list[PackageReferenceSpec] = []
    seen: set[tuple[int, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            raise WDLPackageError(
                "WDL_PACKAGE_REFERENCES_INVALID",
                "Each package reference must contain package, version, and mount_prefix.",
            )
        package_slug = str(item.get("package") or item.get("package_slug") or "").strip()
        version = str(item.get("version") or "").strip()
        mount_prefix = normalize_mount_prefix(item.get("mount_prefix") or "")
        package_version = (
            WDLToolPackageVersion.objects.select_related("package")
            .prefetch_related("files")
            .filter(package__slug=package_slug, version=version)
            .first()
        )
        if package_version is None:
            raise WDLPackageError(
                "WDL_TOOL_PACKAGE_VERSION_NOT_FOUND",
                f"Tool package version not found: {package_slug}@{version}.",
            )
        requested_digest = str(item.get("digest") or "").strip()
        if requested_digest and requested_digest != package_version.digest:
            raise WDLPackageError(
                "WDL_TOOL_PACKAGE_DIGEST_MISMATCH",
                f"Digest does not match {package_slug}@{version}.",
            )
        identity = (package_slug, version, mount_prefix)
        if (
            package_version.package.lifecycle == WDLToolPackage.Lifecycle.ARCHIVED
            and identity not in existing
        ):
            raise WDLPackageError(
                "WDL_TOOL_PACKAGE_ARCHIVED",
                f"Archived tool package cannot be added: {package_slug}.",
            )
        key = (package_version.id, mount_prefix)
        if key in seen:
            continue
        seen.add(key)
        specifications.append(
            PackageReferenceSpec(
                package_version=package_version,
                mount_prefix=mount_prefix,
                digest=package_version.digest,
            )
        )
    return specifications


def reference_spec_key(specification: PackageReferenceSpec) -> tuple[int, str, str]:
    return (
        specification.package_version.id,
        specification.mount_prefix,
        specification.digest,
    )


def effective_package_files(
    local_files: dict[str, str],
    specifications: list[PackageReferenceSpec],
) -> tuple[dict[str, str], dict[str, tuple[PackageReferenceSpec, object]]]:
    files = dict(local_files)
    origins: dict[str, tuple[PackageReferenceSpec, object]] = {}
    for specification in specifications:
        package_version = specification.package_version
        if specification.digest != package_version.digest:
            raise WDLPackageError(
                "WDL_TOOL_PACKAGE_DIGEST_MISMATCH",
                f"Stored digest does not match {package_version.package.slug}@{package_version.version}.",
            )
        for package_file in package_version.files.all():
            mounted_path = normalize_package_path(
                posixpath.join(specification.mount_prefix, package_file.path)
                if specification.mount_prefix
                else package_file.path
            )
            if mounted_path in files:
                raise WDLPackageError(
                    "WDL_PACKAGE_MOUNT_CONFLICT",
                    f"Tool package file conflicts with an existing WDL file: {mounted_path}.",
                    details={"path": mounted_path},
                )
            files[mounted_path] = package_file.content
            origins[mounted_path] = (specification, package_file)
    return files, origins


def persist_reference_specs(
    revision: WDLSourceRevision,
    specifications: list[PackageReferenceSpec],
) -> None:
    WDLSourcePackageReference.objects.bulk_create(
        [
            WDLSourcePackageReference(
                revision=revision,
                package_version=specification.package_version,
                mount_prefix=specification.mount_prefix,
                digest=specification.digest,
            )
            for specification in specifications
        ]
    )


def package_reference_payload(specification: PackageReferenceSpec) -> dict:
    package_version = specification.package_version
    return {
        "package_slug": package_version.package.slug,
        "package_name": package_version.package.name,
        "package_lifecycle": package_version.package.lifecycle,
        "version": package_version.version,
        "digest": specification.digest,
        "mount_prefix": specification.mount_prefix,
        "file_count": package_version.files.count(),
        "files": [
            {
                "path": item.path,
                "digest": item.digest,
                "mounted_path": (
                    posixpath.join(specification.mount_prefix, item.path)
                    if specification.mount_prefix
                    else item.path
                ),
            }
            for item in package_version.files.all()
        ],
    }


def current_source_references():
    latest_version = (
        WDLSourceRevision.objects.filter(asset_id=OuterRef("revision__asset_id"))
        .order_by("-version")
        .values("version")[:1]
    )
    return (
        WDLSourcePackageReference.objects.select_related(
            "revision__asset", "package_version__package"
        )
        .annotate(latest_asset_version=Subquery(latest_version))
        .filter(revision__version=F("latest_asset_version"))
    )
