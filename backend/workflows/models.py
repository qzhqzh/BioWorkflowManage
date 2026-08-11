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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tool_id"]


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
    version = models.PositiveIntegerField()
    source = models.CharField(max_length=16, choices=Source.choices)
    content = models.TextField()
    digest = models.CharField(max_length=80)
    validation = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["workflow", "version"],
                name="unique_workflow_wdl_revision",
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
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        PREPARING = "preparing", "Preparing"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
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
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.QUEUED,
        db_index=True,
    )
    progress = models.PositiveSmallIntegerField(default=0)
    current_step = models.CharField(max_length=256, default="等待执行")
    request_payload = models.JSONField(default=dict)
    input_values = models.JSONField(default=dict)
    source_bundle = models.JSONField(default=dict)
    source_digest = models.CharField(max_length=80, blank=True)
    outputs = models.JSONField(default=dict)
    error = models.TextField(blank=True)
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
                ),
                name="analysis_run_has_one_source",
            )
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
