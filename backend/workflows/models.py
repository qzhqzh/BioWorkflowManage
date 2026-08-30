import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone


def default_wdl_release_checks():
    return [
        "syntax",
        "imports",
        "package_pins",
        "approved_review",
        "resolved_threads",
        "small_data_run",
    ]


def default_webhook_event_types():
    return ["analysis.run.terminal"]


class ImmutableSnapshot(models.Model):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError(f"{type(self).__name__} snapshots are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(f"{type(self).__name__} snapshots cannot be deleted.")


class AnalysisProductQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if "code" in kwargs:
            raise ValidationError("AnalysisProduct code is immutable.")
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        if "code" in fields:
            raise ValidationError("AnalysisProduct code is immutable.")
        return super().bulk_update(objs, fields, batch_size=batch_size)


class AnalysisProductVersionQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("AnalysisProductVersion snapshots cannot be updated.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("AnalysisProductVersion snapshots cannot be updated.")

    def delete(self):
        raise ValidationError("AnalysisProductVersion snapshots cannot be deleted.")


class IntegrationOutboxEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("IntegrationOutboxEvent snapshots cannot be updated.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("IntegrationOutboxEvent snapshots cannot be updated.")

    def delete(self):
        raise ValidationError("IntegrationOutboxEvent snapshots cannot be deleted.")


class WorkflowDocument(models.Model):
    class Kind(models.TextChoices):
        WORKFLOW = "workflow", "Workflow"
        SUBWORKFLOW = "subworkflow", "Subworkflow"

    slug = models.SlugField(max_length=128, unique=True)
    name = models.CharField(max_length=256)
    description = models.TextField(blank=True)
    kind = models.CharField(
        max_length=24,
        choices=Kind.choices,
        default=Kind.WORKFLOW,
    )
    workflow_graph = models.JSONField(default=dict)
    editor_document = models.JSONField(default=dict)
    tool_specs = models.JSONField(default=list)
    subworkflow_references = models.JSONField(default=list)
    created_by = models.CharField(max_length=256, default="local-user")
    updated_by = models.CharField(max_length=256, default="local-user")
    document_version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["slug"]


class ToolVersion(ImmutableSnapshot):
    tool_id = models.CharField(max_length=256)
    version = models.CharField(max_length=64)
    name = models.CharField(max_length=256)
    digest = models.CharField(max_length=80)
    tool_spec = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["tool_id", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tool_id", "version"],
                name="unique_tool_version",
            )
        ]


class ToolDocument(models.Model):
    """Mutable authoring state; published ToolVersion rows remain immutable."""

    tool_id = models.CharField(max_length=256, unique=True)
    draft_spec = models.JSONField(default=dict)
    validation = models.JSONField(default=dict)
    draft_version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tool_id"]


class SoftwareAsset(models.Model):
    """Flexible knowledge record for software used by one or more tools."""

    class Lifecycle(models.TextChoices):
        ACTIVE = "active", "Active"
        ARCHIVED = "archived", "Archived"

    slug = models.SlugField(max_length=128, unique=True)
    name = models.CharField(max_length=256)
    summary = models.CharField(max_length=512, blank=True)
    description = models.TextField(blank=True)
    homepage = models.URLField(max_length=2048, blank=True)
    source_repository = models.CharField(max_length=512, blank=True)
    license = models.CharField(max_length=128, blank=True)
    notes = models.TextField(blank=True)
    tags = models.JSONField(default=list)
    metadata = models.JSONField(default=dict)
    lifecycle = models.CharField(
        max_length=16,
        choices=Lifecycle.choices,
        default=Lifecycle.ACTIVE,
    )
    metadata_version = models.PositiveIntegerField(default=1)
    created_by = models.CharField(max_length=256, default="local-user")
    updated_by = models.CharField(max_length=256, default="local-user")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "slug"]


class SoftwareRelease(models.Model):
    software = models.ForeignKey(
        SoftwareAsset,
        on_delete=models.PROTECT,
        related_name="releases",
    )
    version = models.CharField(max_length=128)
    description = models.TextField(blank=True)
    container_images = models.JSONField(default=list)
    metadata = models.JSONField(default=dict)
    metadata_version = models.PositiveIntegerField(default=1)
    created_by = models.CharField(max_length=256, default="local-user")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["software", "version"],
                name="unique_software_release",
            )
        ]


class ToolSoftwareLink(models.Model):
    class Role(models.TextChoices):
        PRIMARY = "primary", "Primary"
        DEPENDENCY = "dependency", "Dependency"
        RUNTIME = "runtime", "Runtime"

    tool_version = models.ForeignKey(
        ToolVersion,
        on_delete=models.PROTECT,
        related_name="software_links",
    )
    software = models.ForeignKey(
        SoftwareAsset,
        on_delete=models.PROTECT,
        related_name="tool_links",
    )
    release = models.ForeignKey(
        SoftwareRelease,
        on_delete=models.PROTECT,
        related_name="tool_links",
        null=True,
        blank=True,
    )
    role = models.CharField(
        max_length=16,
        choices=Role.choices,
        default=Role.PRIMARY,
    )
    note = models.TextField(blank=True)
    created_by = models.CharField(max_length=256, default="local-user")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["role", "software__name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tool_version", "software", "role"],
                name="unique_tool_software_role",
            )
        ]


class SoftwareAuditEvent(ImmutableSnapshot):
    software = models.ForeignKey(
        SoftwareAsset,
        on_delete=models.PROTECT,
        related_name="audit_events",
    )
    release = models.ForeignKey(
        SoftwareRelease,
        on_delete=models.PROTECT,
        related_name="audit_events",
        null=True,
        blank=True,
    )
    action = models.CharField(max_length=32)
    actor = models.CharField(max_length=256, default="local-user")
    changes = models.JSONField(default=dict)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class AnalysisResourceCatalog(models.Model):
    """Mutable pointer to the current portable analysis resource catalog."""

    key = models.SlugField(max_length=64, unique=True, default="default")
    document = models.JSONField(default=dict)
    version = models.PositiveIntegerField(default=1)
    digest = models.CharField(max_length=80)
    created_by = models.CharField(max_length=256, default="local-user")
    updated_by = models.CharField(max_length=256, default="local-user")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]


class AnalysisResourceCatalogRevision(ImmutableSnapshot):
    catalog = models.ForeignKey(
        AnalysisResourceCatalog,
        on_delete=models.PROTECT,
        related_name="revisions",
    )
    version = models.PositiveIntegerField()
    digest = models.CharField(max_length=80)
    document = models.JSONField(default=dict)
    actor = models.CharField(max_length=256, default="local-user")
    note = models.TextField(blank=True)
    changes = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["catalog", "version"],
                name="unique_analysis_resource_catalog_revision",
            )
        ]


class ServiceAccount(models.Model):
    """Machine identity used by external analysis clients and MCP agents."""

    client_id = models.SlugField(max_length=128, unique=True)
    name = models.CharField(max_length=256)
    scopes = models.JSONField(default=list)
    is_active = models.BooleanField(default=True)
    created_by = models.CharField(max_length=256, default="local-user")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["client_id"]


class ServiceToken(models.Model):
    """Revocable bearer credential; only its SHA-256 digest is persisted."""

    service_account = models.ForeignKey(
        ServiceAccount,
        on_delete=models.PROTECT,
        related_name="tokens",
    )
    name = models.CharField(max_length=128)
    prefix = models.CharField(max_length=16, unique=True)
    token_hash = models.CharField(max_length=64, unique=True)
    created_by = models.CharField(max_length=256, default="local-user")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class LoginRateLimitBucket(models.Model):
    """Shared fixed-window login throttle state for all web workers."""

    key = models.CharField(max_length=64, primary_key=True)
    window_started_at = models.DateTimeField()
    request_count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)


class WorkflowVersion(ImmutableSnapshot):
    workflow = models.ForeignKey(
        WorkflowDocument,
        on_delete=models.PROTECT,
        related_name="versions",
    )
    version = models.PositiveIntegerField()
    name = models.CharField(max_length=256)
    description = models.TextField(blank=True)
    kind = models.CharField(max_length=24, default=WorkflowDocument.Kind.WORKFLOW)
    semantic_digest = models.CharField(max_length=80)
    workflow_graph = models.JSONField()
    editor_document = models.JSONField(default=dict)
    tool_specs = models.JSONField(default=list)
    compiled_bundle = models.JSONField(default=dict)
    compiled_digest = models.CharField(max_length=80, blank=True)
    compiler_profile = models.CharField(max_length=64, blank=True)
    interface_contract = models.JSONField(default=dict)
    subworkflow_references = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["workflow", "version"],
                name="unique_workflow_version",
            )
        ]


class AnalysisProduct(models.Model):
    """Stable external analysis identity independent of internal Workflow IDs."""

    code = models.SlugField(max_length=128, unique=True)
    name = models.CharField(max_length=256)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.CharField(max_length=256, default="deployment")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = AnalysisProductQuerySet.as_manager()

    class Meta:
        ordering = ["code"]

    def save(self, *args, **kwargs):
        normalized_code = str(self.code or "").strip().lower()
        if self.pk:
            current_code = type(self).objects.only("code").get(pk=self.pk).code
            if current_code != normalized_code:
                raise ValidationError("AnalysisProduct code is immutable.")
        self.code = normalized_code
        return super().save(*args, **kwargs)


class AnalysisProductVersion(ImmutableSnapshot):
    """Immutable public contract bound to one executable WorkflowVersion."""

    product = models.ForeignKey(
        AnalysisProduct,
        on_delete=models.PROTECT,
        related_name="versions",
    )
    contract_version = models.CharField(max_length=64)
    workflow_version = models.ForeignKey(
        WorkflowVersion,
        on_delete=models.PROTECT,
        related_name="analysis_product_versions",
    )
    source_digest = models.CharField(max_length=80)
    interface_contract = models.JSONField(default=dict)
    contract_digest = models.CharField(max_length=80)
    created_by = models.CharField(max_length=256, default="deployment")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AnalysisProductVersionQuerySet.as_manager()

    class Meta:
        ordering = ["product_id", "contract_version"]
        constraints = [
            models.UniqueConstraint(
                fields=["product", "contract_version"],
                name="unique_analysis_product_contract_version",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(source_digest="") & ~models.Q(contract_digest="")
                ),
                name="analysis_product_version_has_digests",
            ),
        ]


class CompilationRecord(models.Model):
    workflow = models.ForeignKey(
        WorkflowDocument,
        on_delete=models.CASCADE,
        related_name="compilations",
        null=True,
        blank=True,
    )
    workflow_version = models.ForeignKey(
        WorkflowVersion,
        on_delete=models.SET_NULL,
        related_name="compilations",
        null=True,
        blank=True,
    )
    request_id = models.CharField(max_length=128, db_index=True)
    status = models.CharField(max_length=32)
    semantic_digest = models.CharField(max_length=80, blank=True)
    validation = models.JSONField(default=dict)
    artifacts = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class WDLRevision(ImmutableSnapshot):
    class Source(models.TextChoices):
        SYSTEM = "system", "System generated"
        MANUAL = "manual", "Manually authored"

    workflow = models.ForeignKey(
        WorkflowDocument,
        on_delete=models.PROTECT,
        related_name="wdl_revisions",
    )
    workflow_version = models.ForeignKey(
        WorkflowVersion,
        on_delete=models.PROTECT,
        related_name="wdl_revisions",
        null=True,
        blank=True,
    )
    base_revision = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="derived_revisions",
        null=True,
        blank=True,
    )
    version = models.PositiveIntegerField()
    source = models.CharField(max_length=16, choices=Source.choices)
    content = models.TextField()
    digest = models.CharField(max_length=80)
    validation = models.JSONField(default=dict)
    created_by = models.CharField(max_length=256, default="local-user")
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["workflow", "version"],
                name="unique_workflow_wdl_revision",
            )
        ]


class WDLGraphProposal(models.Model):
    class Status(models.TextChoices):
        READY = "ready", "Ready"
        BLOCKED = "blocked", "Blocked"
        APPLIED = "applied", "Applied"

    workflow = models.ForeignKey(
        WorkflowDocument,
        on_delete=models.PROTECT,
        related_name="wdl_graph_proposals",
    )
    source_revision = models.ForeignKey(
        WDLRevision,
        on_delete=models.PROTECT,
        related_name="graph_proposals",
    )
    base_document_version = models.PositiveIntegerField()
    base_document_digest = models.CharField(max_length=80)
    proposal_digest = models.CharField(max_length=80)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.READY,
    )
    proposal = models.JSONField(default=dict)
    created_by = models.CharField(max_length=256, default="local-user")
    applied_by = models.CharField(max_length=256, blank=True)
    applied_document_version = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "workflow",
                    "source_revision",
                    "base_document_version",
                    "proposal_digest",
                ],
                name="unique_wdl_graph_proposal",
            )
        ]


class WDLTag(models.Model):
    name = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                Lower("name"),
                name="unique_wdl_tag_name_ci",
            )
        ]


class WDLToolPackageTag(models.Model):
    name = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]


class WDLToolPackage(models.Model):
    class Lifecycle(models.TextChoices):
        ACTIVE = "active", "Active"
        ARCHIVED = "archived", "Archived"

    slug = models.SlugField(max_length=128, unique=True)
    name = models.CharField(max_length=256)
    description = models.TextField(blank=True)
    lifecycle = models.CharField(
        max_length=16,
        choices=Lifecycle.choices,
        default=Lifecycle.ACTIVE,
    )
    tags = models.ManyToManyField(
        WDLToolPackageTag,
        related_name="packages",
        blank=True,
    )
    created_by = models.CharField(max_length=256, default="local-user")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "slug"]


class WDLToolPackageVersion(ImmutableSnapshot):
    package = models.ForeignKey(
        WDLToolPackage,
        on_delete=models.PROTECT,
        related_name="versions",
    )
    version = models.CharField(max_length=64)
    digest = models.CharField(max_length=80)
    source_repository = models.CharField(max_length=512, blank=True)
    source_revision = models.CharField(max_length=128, blank=True)
    note = models.TextField(blank=True)
    actor = models.CharField(max_length=256, default="local-user")
    analysis = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["package", "version"],
                name="unique_wdl_tool_package_version",
            )
        ]


class WDLToolPackageFile(ImmutableSnapshot):
    package_version = models.ForeignKey(
        WDLToolPackageVersion,
        on_delete=models.PROTECT,
        related_name="files",
    )
    path = models.CharField(max_length=512)
    content = models.TextField()
    digest = models.CharField(max_length=80)
    analysis = models.JSONField(default=dict)

    class Meta:
        ordering = ["path"]
        constraints = [
            models.UniqueConstraint(
                fields=["package_version", "path"],
                name="unique_wdl_tool_package_file_path",
            )
        ]


class WDLToolPackageAuditEvent(ImmutableSnapshot):
    package = models.ForeignKey(
        WDLToolPackage,
        on_delete=models.PROTECT,
        related_name="audit_events",
    )
    package_version = models.ForeignKey(
        WDLToolPackageVersion,
        on_delete=models.PROTECT,
        related_name="audit_events",
        null=True,
        blank=True,
    )
    action = models.CharField(max_length=32)
    actor = models.CharField(max_length=256, default="local-user")
    note = models.TextField(blank=True)
    changes = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class WDLAsset(models.Model):
    class Lifecycle(models.TextChoices):
        ACTIVE = "active", "Active"
        FROZEN = "frozen", "Frozen"
        MIGRATING = "migrating", "Migrating"
        RETIRED = "retired", "Retired"

    slug = models.SlugField(max_length=128, unique=True)
    name = models.CharField(max_length=256)
    description = models.TextField(blank=True)
    source_filename = models.CharField(max_length=512)
    source_repository = models.CharField(max_length=512, blank=True)
    source_revision = models.CharField(max_length=128, blank=True)
    lifecycle = models.CharField(
        max_length=16,
        choices=Lifecycle.choices,
        default=Lifecycle.ACTIVE,
    )
    metadata_version = models.PositiveIntegerField(default=1)
    tags = models.ManyToManyField(WDLTag, related_name="wdl_assets", blank=True)
    created_by = models.CharField(max_length=256, default="local-user")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "slug"]


class WDLSourceRevision(ImmutableSnapshot):
    class Operation(models.TextChoices):
        IMPORT = "import", "Imported"
        EDIT = "edit", "Edited"
        FORMAT = "format", "Formatted"
        PACKAGE_LINK = "package_link", "Linked tool package"

    asset = models.ForeignKey(
        WDLAsset,
        on_delete=models.PROTECT,
        related_name="source_revisions",
    )
    version = models.PositiveIntegerField()
    operation = models.CharField(max_length=16, choices=Operation.choices)
    content = models.TextField()
    digest = models.CharField(max_length=80)
    diff = models.TextField(blank=True)
    note = models.TextField(blank=True)
    actor = models.CharField(max_length=256, default="local-user")
    analysis = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["asset", "version"],
                name="unique_wdl_asset_source_revision",
            )
        ]


class WDLSourceFile(ImmutableSnapshot):
    revision = models.ForeignKey(
        WDLSourceRevision,
        on_delete=models.PROTECT,
        related_name="files",
    )
    path = models.CharField(max_length=512)
    content = models.TextField()
    digest = models.CharField(max_length=80)
    is_entry = models.BooleanField(default=False)
    analysis = models.JSONField(default=dict)

    class Meta:
        ordering = ["path"]
        constraints = [
            models.UniqueConstraint(
                fields=["revision", "path"],
                name="unique_wdl_source_file_path",
            )
        ]


class WDLSourcePackageReference(ImmutableSnapshot):
    revision = models.ForeignKey(
        WDLSourceRevision,
        on_delete=models.PROTECT,
        related_name="package_references",
    )
    package_version = models.ForeignKey(
        WDLToolPackageVersion,
        on_delete=models.PROTECT,
        related_name="source_references",
    )
    mount_prefix = models.CharField(max_length=384, blank=True)
    digest = models.CharField(max_length=80)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["mount_prefix", "package_version__package__slug"]
        constraints = [
            models.UniqueConstraint(
                fields=["revision", "package_version", "mount_prefix"],
                name="unique_wdl_source_package_reference",
            )
        ]


class WDLAuditEvent(ImmutableSnapshot):
    asset = models.ForeignKey(
        WDLAsset,
        on_delete=models.PROTECT,
        related_name="audit_events",
    )
    revision = models.ForeignKey(
        WDLSourceRevision,
        on_delete=models.PROTECT,
        related_name="audit_events",
        null=True,
        blank=True,
    )
    action = models.CharField(max_length=32)
    actor = models.CharField(max_length=256, default="local-user")
    note = models.TextField(blank=True)
    changes = models.JSONField(default=dict)
    diff = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class WDLReviewRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        CHANGES_REQUESTED = "changes_requested", "Changes requested"
        CANCELLED = "cancelled", "Cancelled"

    asset = models.ForeignKey(
        WDLAsset,
        on_delete=models.PROTECT,
        related_name="review_requests",
    )
    revision = models.ForeignKey(
        WDLSourceRevision,
        on_delete=models.PROTECT,
        related_name="review_requests",
    )
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    version = models.PositiveIntegerField(default=1)
    requester = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="requested_wdl_reviews",
        null=True,
        blank=True,
    )
    requester_name = models.CharField(max_length=256)
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="assigned_wdl_reviews",
        null=True,
        blank=True,
    )
    assignee_name = models.CharField(max_length=256)
    request_note = models.TextField(blank=True)
    conclusion = models.TextField(blank=True)
    concluded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="concluded_wdl_reviews",
        null=True,
        blank=True,
    )
    concluded_by_name = models.CharField(max_length=256, blank=True)
    concluded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["revision"],
                condition=models.Q(status="pending"),
                name="unique_pending_wdl_revision_review",
            )
        ]


class WDLReviewThread(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        RESOLVED = "resolved", "Resolved"

    asset = models.ForeignKey(
        WDLAsset,
        on_delete=models.PROTECT,
        related_name="review_threads",
    )
    revision = models.ForeignKey(
        WDLSourceRevision,
        on_delete=models.PROTECT,
        related_name="review_threads",
    )
    file_path = models.CharField(max_length=512)
    line = models.PositiveIntegerField()
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.OPEN,
        db_index=True,
    )
    version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="created_wdl_review_threads",
        null=True,
        blank=True,
    )
    created_by_name = models.CharField(max_length=256)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="resolved_wdl_review_threads",
        null=True,
        blank=True,
    )
    resolved_by_name = models.CharField(max_length=256, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "file_path", "line", "created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(line__gte=1),
                name="wdl_review_thread_line_positive",
            )
        ]


class WDLReviewComment(ImmutableSnapshot):
    thread = models.ForeignKey(
        WDLReviewThread,
        on_delete=models.PROTECT,
        related_name="comments",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="wdl_review_comments",
        null=True,
        blank=True,
    )
    author_name = models.CharField(max_length=256)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class WDLSourceConflict(models.Model):
    asset = models.ForeignKey(
        WDLAsset,
        on_delete=models.PROTECT,
        related_name="source_conflicts",
    )
    current_revision = models.ForeignKey(
        WDLSourceRevision,
        on_delete=models.PROTECT,
        related_name="source_conflicts",
    )
    actor = models.CharField(max_length=256)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="wdl_source_conflicts",
        null=True,
        blank=True,
    )
    base_version = models.PositiveIntegerField()
    base_digest = models.CharField(max_length=80)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-updated_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["asset", "actor"],
                condition=models.Q(resolved_at__isnull=True),
                name="unique_open_wdl_source_conflict",
            ),
            models.UniqueConstraint(
                fields=["asset", "assigned_to"],
                condition=(
                    models.Q(resolved_at__isnull=True)
                    & models.Q(assigned_to__isnull=False)
                ),
                name="unique_assigned_open_wdl_source_conflict",
            ),
        ]


class WDLReleasePolicy(models.Model):
    key = models.SlugField(max_length=64, unique=True, default="default")
    version = models.PositiveIntegerField(default=1)
    enabled_checks = models.JSONField(default=default_wdl_release_checks)
    max_input_bytes = models.BigIntegerField(default=1_073_741_824)
    updated_by = models.CharField(max_length=256, default="system")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]


class WDLReleaseCheck(ImmutableSnapshot):
    class Status(models.TextChoices):
        PASSED = "passed", "Passed"
        FAILED = "failed", "Failed"

    asset = models.ForeignKey(
        WDLAsset,
        on_delete=models.PROTECT,
        related_name="release_checks",
    )
    revision = models.ForeignKey(
        WDLSourceRevision,
        on_delete=models.PROTECT,
        related_name="release_checks",
    )
    analysis_run = models.ForeignKey(
        "AnalysisRun",
        on_delete=models.PROTECT,
        related_name="wdl_release_checks",
        null=True,
        blank=True,
    )
    status = models.CharField(max_length=16, choices=Status.choices)
    policy_version = models.PositiveIntegerField()
    policy_snapshot = models.JSONField(default=dict)
    checks = models.JSONField(default=list)
    requested_by = models.CharField(max_length=256)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class WDLAssetRelease(ImmutableSnapshot):
    asset = models.ForeignKey(
        WDLAsset,
        on_delete=models.PROTECT,
        related_name="releases",
    )
    revision = models.ForeignKey(
        WDLSourceRevision,
        on_delete=models.PROTECT,
        related_name="releases",
    )
    release_check = models.OneToOneField(
        WDLReleaseCheck,
        on_delete=models.PROTECT,
        related_name="release",
    )
    version = models.CharField(max_length=64)
    note = models.TextField(blank=True)
    actor = models.CharField(max_length=256)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["asset", "version"],
                name="unique_wdl_asset_release_version",
            ),
            models.UniqueConstraint(
                fields=["asset", "revision"],
                name="unique_wdl_asset_release_revision",
            ),
        ]


class RawdataScan(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        LIMITED = "limited", "Limited"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    root_key = models.CharField(max_length=80, db_index=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.QUEUED,
        db_index=True,
    )
    trigger = models.CharField(max_length=24, default="scheduled")
    actor = models.CharField(max_length=256, default="system")
    progress = models.JSONField(default=dict)
    catalog = models.JSONField(default=dict)
    error = models.TextField(blank=True)
    scanned_entry_count = models.PositiveIntegerField(default=0)
    scan_limited = models.BooleanField(default=False)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    lease_token = models.UUIDField(null=True, blank=True, editable=False)
    lease_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["root_key"],
                condition=models.Q(status__in=["queued", "running"]),
                name="unique_active_rawdata_scan",
            )
        ]


class RawdataDatasetIndex(models.Model):
    root_key = models.CharField(max_length=80, db_index=True)
    dataset_id = models.CharField(max_length=64)
    pair_key = models.CharField(max_length=1024)
    name = models.CharField(max_length=256)
    directory = models.CharField(max_length=1024)
    status = models.CharField(max_length=32, db_index=True)
    issues = models.JSONField(default=list)
    files = models.JSONField(default=list)
    total_size = models.BigIntegerField(default=0)
    identity_digest = models.CharField(max_length=80)
    active = models.BooleanField(default=True, db_index=True)
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    last_changed_at = models.DateTimeField()
    last_scan = models.ForeignKey(
        RawdataScan,
        on_delete=models.PROTECT,
        related_name="datasets",
    )

    class Meta:
        ordering = ["directory", "name", "dataset_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["root_key", "dataset_id"],
                name="unique_rawdata_dataset_index",
            )
        ]


class RawdataDatasetEvent(ImmutableSnapshot):
    dataset = models.ForeignKey(
        RawdataDatasetIndex,
        on_delete=models.PROTECT,
        related_name="events",
    )
    scan = models.ForeignKey(
        RawdataScan,
        on_delete=models.PROTECT,
        related_name="dataset_events",
        null=True,
        blank=True,
    )
    run = models.ForeignKey(
        "AnalysisRun",
        on_delete=models.PROTECT,
        related_name="rawdata_events",
        null=True,
        blank=True,
    )
    action = models.CharField(max_length=24)
    actor = models.CharField(max_length=256, default="system")
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class RawdataRunReference(ImmutableSnapshot):
    dataset = models.ForeignKey(
        RawdataDatasetIndex,
        on_delete=models.PROTECT,
        related_name="run_references",
    )
    run = models.ForeignKey(
        "AnalysisRun",
        on_delete=models.PROTECT,
        related_name="rawdata_references",
    )
    identity = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["dataset", "run"],
                name="unique_rawdata_dataset_run_reference",
            )
        ]


class AnalysisRun(models.Model):
    class Kind(models.TextChoices):
        WORKFLOW = "workflow", "Workflow analysis"
        TOOL_TEST = "tool_test", "Tool test"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        PREPARING = "preparing", "Preparing"
        RUNNING = "running", "Running"
        CANCEL_REQUESTED = "cancel_requested", "Cancel requested"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELED = "canceled", "Canceled"

    class OutputStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        COMPLETE = "complete", "Complete"
        INCOMPLETE = "incomplete", "Incomplete"
        UNAVAILABLE = "unavailable", "Unavailable"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run_kind = models.CharField(
        max_length=16,
        choices=Kind.choices,
        default=Kind.WORKFLOW,
        db_index=True,
    )
    asset = models.ForeignKey(
        WDLAsset,
        on_delete=models.PROTECT,
        related_name="analysis_runs",
        null=True,
        blank=True,
    )
    revision = models.ForeignKey(
        WDLSourceRevision,
        on_delete=models.PROTECT,
        related_name="analysis_runs",
        null=True,
        blank=True,
    )
    workflow_version = models.ForeignKey(
        WorkflowVersion,
        on_delete=models.PROTECT,
        related_name="analysis_runs",
        null=True,
        blank=True,
    )
    analysis_product_version = models.ForeignKey(
        AnalysisProductVersion,
        on_delete=models.PROTECT,
        related_name="analysis_runs",
        null=True,
        blank=True,
    )
    tool_version = models.ForeignKey(
        ToolVersion,
        on_delete=models.PROTECT,
        related_name="test_runs",
        null=True,
        blank=True,
    )
    workflow_name = models.CharField(max_length=256)
    sample_id = models.CharField(max_length=256)
    sample_name = models.CharField(max_length=256, blank=True)
    actor = models.CharField(max_length=256, default="local-user")
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="submitted_analysis_runs",
        null=True,
        blank=True,
    )
    service_account = models.ForeignKey(
        ServiceAccount,
        on_delete=models.PROTECT,
        related_name="analysis_runs",
        null=True,
        blank=True,
    )
    external_run_id = models.CharField(max_length=128, blank=True, db_index=True)
    external_analysis_id = models.CharField(
        max_length=128,
        blank=True,
        db_index=True,
    )
    idempotency_key = models.CharField(max_length=128, blank=True)
    request_digest = models.CharField(max_length=80, blank=True)
    retry_of = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="retries",
        null=True,
        blank=True,
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.QUEUED,
        db_index=True,
    )
    status_version = models.PositiveIntegerField(default=1)
    progress = models.PositiveSmallIntegerField(default=0)
    current_step = models.CharField(max_length=256, default="等待执行")
    request_payload = models.JSONField(default=dict)
    input_values = models.JSONField(default=dict)
    source_bundle = models.JSONField(default=dict)
    source_digest = models.CharField(max_length=80, blank=True)
    outputs = models.JSONField(default=dict)
    output_status = models.CharField(
        max_length=16,
        choices=OutputStatus.choices,
        default=OutputStatus.PENDING,
    )
    output_manifest = models.JSONField(default=dict)
    error = models.TextField(blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_category = models.CharField(max_length=32, blank=True)
    error_retryable = models.BooleanField(default=False)
    error_details = models.JSONField(default=dict)
    work_directory = models.CharField(max_length=1024, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    lease_token = models.UUIDField(null=True, blank=True, editable=False)
    worker_heartbeat_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    (
                        models.Q(run_kind="workflow", tool_version__isnull=True)
                        & (
                            models.Q(
                                asset__isnull=False,
                                revision__isnull=False,
                                workflow_version__isnull=True,
                            )
                            | models.Q(
                                asset__isnull=True,
                                revision__isnull=True,
                                workflow_version__isnull=False,
                            )
                        )
                    )
                    | models.Q(
                        run_kind="tool_test",
                        asset__isnull=True,
                        revision__isnull=True,
                        workflow_version__isnull=True,
                        tool_version__isnull=False,
                    )
                ),
                name="analysis_run_has_one_source",
            ),
            models.UniqueConstraint(
                fields=["service_account", "external_run_id"],
                condition=(
                    models.Q(service_account__isnull=False)
                    & ~models.Q(external_run_id="")
                ),
                name="unique_service_external_run",
            ),
            models.UniqueConstraint(
                fields=["service_account", "idempotency_key"],
                condition=(
                    models.Q(service_account__isnull=False)
                    & ~models.Q(idempotency_key="")
                ),
                name="unique_service_idempotency_key",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(run_kind="workflow")
                    | models.Q(analysis_product_version__isnull=True)
                ),
                name="tool_test_has_no_analysis_product",
            ),
        ]


class AnalysisRunEvent(models.Model):
    run = models.ForeignKey(
        AnalysisRun,
        on_delete=models.CASCADE,
        related_name="events",
    )
    kind = models.CharField(max_length=32, default="status")
    level = models.CharField(max_length=16, default="info")
    message = models.CharField(max_length=1000)
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class InputStagingCoordinator(models.Model):
    """Singleton row used to serialize cross-worker staging reservations."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class InputStagingLease(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.OneToOneField(
        AnalysisRun,
        on_delete=models.CASCADE,
        related_name="input_staging_lease",
    )
    worker_lease_token = models.UUIDField(editable=False)
    reserved_bytes = models.PositiveBigIntegerField()
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(reserved_bytes__gt=0),
                name="input_staging_lease_positive_bytes",
            )
        ]


class AnalysisOutputRetention(models.Model):
    """Explicit, operator-driven lifecycle for one run's local output tree."""

    class State(models.TextChoices):
        PROTECTED = "protected", "Protected"
        CLEANING = "cleaning", "Cleaning"
        CLEANED = "cleaned", "Cleaned"
        FAILED = "failed", "Cleanup failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.OneToOneField(
        AnalysisRun,
        on_delete=models.CASCADE,
        related_name="output_retention",
    )
    retain_until = models.DateTimeField(db_index=True)
    state = models.CharField(
        max_length=16,
        choices=State.choices,
        default=State.PROTECTED,
        db_index=True,
    )
    cleanup_attempt_count = models.PositiveIntegerField(default=0)
    cleanup_token = models.UUIDField(null=True, blank=True, editable=False)
    cleanup_path_token = models.UUIDField(null=True, blank=True, editable=False)
    cleanup_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    quarantined_at = models.DateTimeField(null=True, blank=True)
    cleaned_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True)
    last_error = models.TextField(blank=True)
    created_by = models.CharField(max_length=256)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["retain_until", "created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state="cleaning",
                        cleanup_token__isnull=False,
                        cleanup_expires_at__isnull=False,
                    )
                    | (
                        ~models.Q(state="cleaning")
                        & models.Q(
                            cleanup_token__isnull=True,
                            cleanup_expires_at__isnull=True,
                        )
                    )
                ),
                name="output_retention_cleanup_lease_matches_state",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(state="cleaned")
                    | models.Q(cleaned_at__isnull=False)
                ),
                name="output_retention_cleaned_has_timestamp",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(state="cleaned")
                    | models.Q(quarantined_at__isnull=False)
                ),
                name="output_retention_cleaned_was_quarantined",
            ),
        ]


class ArtifactExport(models.Model):
    """Asynchronous, idempotent delivery of one immutable output manifest."""

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        EXPORTING = "exporting", "Exporting"
        SUCCEEDED = "succeeded", "Succeeded"
        DEAD_LETTER = "dead_letter", "Dead letter"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(
        AnalysisRun,
        on_delete=models.PROTECT,
        related_name="artifact_exports",
    )
    service_account = models.ForeignKey(
        ServiceAccount,
        on_delete=models.PROTECT,
        related_name="artifact_exports",
    )
    retention = models.ForeignKey(
        AnalysisOutputRetention,
        on_delete=models.PROTECT,
        related_name="artifact_exports",
    )
    idempotency_key = models.CharField(max_length=128)
    request_digest = models.CharField(max_length=80)
    source_manifest_digest = models.CharField(max_length=80)
    target_profile = models.CharField(max_length=64)
    target_snapshot = models.JSONField(default=dict)
    state = models.CharField(
        max_length=16,
        choices=State.choices,
        default=State.PENDING,
        db_index=True,
    )
    manifest = models.JSONField(default=dict)
    manifest_digest = models.CharField(max_length=80, blank=True)
    manifest_completed_at = models.DateTimeField(null=True, blank=True)
    manifest_location = models.JSONField(default=dict)
    requires_ack = models.BooleanField(default=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.CharField(max_length=256, blank=True)
    acknowledgement = models.JSONField(default=dict)
    attempt_count = models.PositiveIntegerField(default=0)
    replay_count = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    lease_token = models.UUIDField(null=True, blank=True, editable=False)
    lease_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True)
    last_error = models.TextField(blank=True)
    last_error_retryable = models.BooleanField(default=False)
    last_replayed_at = models.DateTimeField(null=True, blank=True)
    last_replayed_by = models.CharField(max_length=256, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["next_attempt_at", "created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["service_account", "idempotency_key"],
                name="unique_service_artifact_export_idempotency",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state="exporting",
                        lease_token__isnull=False,
                        lease_expires_at__isnull=False,
                    )
                    | (
                        ~models.Q(state="exporting")
                        & models.Q(
                            lease_token__isnull=True,
                            lease_expires_at__isnull=True,
                        )
                    )
                ),
                name="artifact_export_lease_matches_state",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(state="succeeded")
                    | models.Q(completed_at__isnull=False)
                ),
                name="artifact_export_success_has_completed_at",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(state="succeeded")
                    | models.Q(manifest_completed_at__isnull=False)
                ),
                name="artifact_export_success_has_manifest_time",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(state="succeeded")
                    | ~models.Q(manifest_digest="")
                ),
                name="artifact_export_success_has_manifest_digest",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(acknowledged_at__isnull=True)
                    | models.Q(state="succeeded")
                ),
                name="artifact_export_ack_requires_success",
            ),
        ]
        indexes = [
            models.Index(
                fields=["state", "next_attempt_at"],
                name="artifact_export_due_idx",
            ),
            models.Index(
                fields=["state", "lease_expires_at"],
                name="artifact_export_lease_idx",
            ),
        ]


class ArtifactExportAttempt(models.Model):
    class Outcome(models.TextChoices):
        STARTED = "started", "Started"
        SUCCEEDED = "succeeded", "Succeeded"
        RETRY = "retry", "Retry scheduled"
        DEAD_LETTER = "dead_letter", "Dead letter"
        LEASE_EXPIRED = "lease_expired", "Lease expired"

    export = models.ForeignKey(
        ArtifactExport,
        on_delete=models.PROTECT,
        related_name="attempts",
    )
    attempt_number = models.PositiveIntegerField()
    replay_number = models.PositiveIntegerField(default=0)
    outcome = models.CharField(
        max_length=16,
        choices=Outcome.choices,
        default=Outcome.STARTED,
    )
    files_total = models.PositiveIntegerField(default=0)
    files_exported = models.PositiveIntegerField(default=0)
    bytes_exported = models.PositiveBigIntegerField(default=0)
    error_code = models.CharField(max_length=64, blank=True)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["export_id", "attempt_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["export", "attempt_number"],
                name="unique_artifact_export_attempt",
            )
        ]


class WebhookEndpoint(models.Model):
    """Outbound terminal-event subscription owned by one Service Account."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service_account = models.ForeignKey(
        ServiceAccount,
        on_delete=models.PROTECT,
        related_name="webhook_endpoints",
    )
    name = models.SlugField(max_length=128)
    url = models.URLField(max_length=2048)
    event_types = models.JSONField(default=default_webhook_event_types)
    is_active = models.BooleanField(default=True)
    secret_salt = models.UUIDField(default=uuid.uuid4, editable=False)
    secret_version = models.PositiveIntegerField(default=1)
    created_by = models.CharField(max_length=256, default="deployment")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["service_account_id", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["service_account", "name"],
                name="unique_service_webhook_endpoint",
            )
        ]


class IntegrationOutboxEvent(ImmutableSnapshot):
    """Immutable integration event snapshot committed with its source state."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service_account = models.ForeignKey(
        ServiceAccount,
        on_delete=models.PROTECT,
        related_name="outbox_events",
    )
    run = models.ForeignKey(
        AnalysisRun,
        on_delete=models.PROTECT,
        related_name="outbox_events",
    )
    event_type = models.CharField(max_length=64)
    status_version = models.PositiveIntegerField()
    deduplication_key = models.CharField(max_length=128)
    payload = models.JSONField()
    occurred_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    objects = IntegrationOutboxEventQuerySet.as_manager()

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "event_type", "deduplication_key"],
                name="unique_run_outbox_deduplication_key",
            ),
            models.CheckConstraint(
                condition=~models.Q(deduplication_key=""),
                name="outbox_deduplication_key_not_empty",
            ),
        ]
        indexes = [
            models.Index(
                fields=["service_account", "created_at"],
                name="outbox_service_created_idx",
            )
        ]


class WebhookDelivery(models.Model):
    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        DELIVERING = "delivering", "Delivering"
        DELIVERED = "delivered", "Delivered"
        DEAD_LETTER = "dead_letter", "Dead letter"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(
        IntegrationOutboxEvent,
        on_delete=models.PROTECT,
        related_name="deliveries",
    )
    endpoint = models.ForeignKey(
        WebhookEndpoint,
        on_delete=models.PROTECT,
        related_name="deliveries",
    )
    target_url = models.URLField(max_length=2048)
    secret_salt = models.UUIDField(editable=False)
    secret_version = models.PositiveIntegerField()
    state = models.CharField(
        max_length=16,
        choices=State.choices,
        default=State.PENDING,
        db_index=True,
    )
    attempt_count = models.PositiveIntegerField(default=0)
    replay_count = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    lease_token = models.UUIDField(null=True, blank=True, editable=False)
    lease_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    last_status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True)
    last_error = models.TextField(blank=True)
    last_response_excerpt = models.TextField(blank=True)
    last_replayed_at = models.DateTimeField(null=True, blank=True)
    last_replayed_by = models.CharField(max_length=256, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["next_attempt_at", "created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["event", "endpoint"],
                name="unique_event_endpoint_delivery",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state="delivering",
                        lease_token__isnull=False,
                        lease_expires_at__isnull=False,
                    )
                    | (
                        ~models.Q(state="delivering")
                        & models.Q(
                            lease_token__isnull=True,
                            lease_expires_at__isnull=True,
                        )
                    )
                ),
                name="webhook_delivery_lease_matches_state",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(state="delivered")
                    | models.Q(delivered_at__isnull=False)
                ),
                name="webhook_delivery_has_delivered_at",
            ),
        ]
        indexes = [
            models.Index(
                fields=["state", "next_attempt_at"],
                name="webhook_delivery_due_idx",
            ),
            models.Index(
                fields=["state", "lease_expires_at"],
                name="webhook_delivery_lease_idx",
            ),
        ]


class WebhookDeliveryAttempt(models.Model):
    class Outcome(models.TextChoices):
        STARTED = "started", "Started"
        DELIVERED = "delivered", "Delivered"
        RETRY = "retry", "Retry scheduled"
        DEAD_LETTER = "dead_letter", "Dead letter"
        LEASE_EXPIRED = "lease_expired", "Lease expired"

    delivery = models.ForeignKey(
        WebhookDelivery,
        on_delete=models.PROTECT,
        related_name="attempts",
    )
    attempt_number = models.PositiveIntegerField()
    replay_number = models.PositiveIntegerField(default=0)
    outcome = models.CharField(
        max_length=16,
        choices=Outcome.choices,
        default=Outcome.STARTED,
    )
    request_timestamp = models.PositiveBigIntegerField(null=True, blank=True)
    resolved_address = models.GenericIPAddressField(null=True, blank=True)
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    error = models.TextField(blank=True)
    response_excerpt = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["delivery_id", "attempt_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["delivery", "attempt_number"],
                name="unique_webhook_delivery_attempt",
            )
        ]
