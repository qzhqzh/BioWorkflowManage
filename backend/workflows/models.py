from django.core.exceptions import ValidationError
from django.db import models


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
    lifecycle = models.CharField(
        max_length=16,
        choices=Lifecycle.choices,
        default=Lifecycle.ACTIVE,
    )
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
