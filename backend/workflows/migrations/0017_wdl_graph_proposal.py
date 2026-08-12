import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0016_wdl_revision_provenance"),
    ]

    operations = [
        migrations.CreateModel(
            name="WDLGraphProposal",
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
                ("base_document_version", models.PositiveIntegerField()),
                ("base_document_digest", models.CharField(max_length=80)),
                ("proposal_digest", models.CharField(max_length=80)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("ready", "Ready"),
                            ("blocked", "Blocked"),
                            ("applied", "Applied"),
                        ],
                        default="ready",
                        max_length=16,
                    ),
                ),
                ("proposal", models.JSONField(default=dict)),
                ("created_by", models.CharField(default="local-user", max_length=256)),
                ("applied_by", models.CharField(blank=True, max_length=256)),
                ("applied_document_version", models.PositiveIntegerField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("applied_at", models.DateTimeField(blank=True, null=True)),
                (
                    "source_revision",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="graph_proposals",
                        to="workflows.wdlrevision",
                    ),
                ),
                (
                    "workflow",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="wdl_graph_proposals",
                        to="workflows.workflowdocument",
                    ),
                ),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.AddConstraint(
            model_name="wdlgraphproposal",
            constraint=models.UniqueConstraint(
                fields=(
                    "workflow",
                    "source_revision",
                    "base_document_version",
                    "proposal_digest",
                ),
                name="unique_wdl_graph_proposal",
            ),
        ),
    ]
