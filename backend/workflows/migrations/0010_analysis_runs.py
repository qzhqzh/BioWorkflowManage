import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0009_wdl_source_package_references"),
    ]

    operations = [
        migrations.CreateModel(
            name="AnalysisRun",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("workflow_name", models.CharField(max_length=256)),
                ("sample_id", models.CharField(max_length=256)),
                ("sample_name", models.CharField(blank=True, max_length=256)),
                ("actor", models.CharField(default="local-user", max_length=256)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("preparing", "Preparing"),
                            ("running", "Running"),
                            ("succeeded", "Succeeded"),
                            ("failed", "Failed"),
                        ],
                        db_index=True,
                        default="queued",
                        max_length=16,
                    ),
                ),
                ("progress", models.PositiveSmallIntegerField(default=0)),
                ("current_step", models.CharField(default="等待执行", max_length=256)),
                ("request_payload", models.JSONField(default=dict)),
                ("input_values", models.JSONField(default=dict)),
                ("outputs", models.JSONField(default=dict)),
                ("error", models.TextField(blank=True)),
                ("work_directory", models.CharField(blank=True, max_length=1024)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "asset",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="analysis_runs",
                        to="workflows.wdlasset",
                    ),
                ),
                (
                    "revision",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="analysis_runs",
                        to="workflows.wdlsourcerevision",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="AnalysisRunEvent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("kind", models.CharField(default="status", max_length=32)),
                ("level", models.CharField(default="info", max_length=16)),
                ("message", models.CharField(max_length=1000)),
                ("details", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="events",
                        to="workflows.analysisrun",
                    ),
                ),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
    ]
