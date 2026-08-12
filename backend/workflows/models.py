import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower


class ImmutableSnapshot(models.Model):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError(f"{type(self).__name__} snapshots are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(f"{type(self).__name__} snapshots cannot be deleted.")


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
