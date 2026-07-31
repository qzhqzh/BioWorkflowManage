from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("workflows", "0003_normalize_implicit_primary_keys")]

    operations = [
        migrations.AddField(
            model_name="workflowdocument",
            name="description",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="workflowdocument",
            name="kind",
            field=models.CharField(
                choices=[("workflow", "Workflow"), ("subworkflow", "Subworkflow")],
                default="workflow",
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name="workflowdocument",
            name="subworkflow_references",
            field=models.JSONField(default=list),
        ),
        migrations.AddField(
            model_name="workflowversion",
            name="description",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="workflowversion",
            name="interface_contract",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="workflowversion",
            name="kind",
            field=models.CharField(default="workflow", max_length=24),
        ),
        migrations.AddField(
            model_name="workflowversion",
            name="subworkflow_references",
            field=models.JSONField(default=list),
        ),
        migrations.CreateModel(
            name="WDLRevision",
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
                ("version", models.PositiveIntegerField()),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("system", "System generated"),
                            ("manual", "Manually authored"),
                        ],
                        max_length=16,
                    ),
                ),
                ("content", models.TextField()),
                ("digest", models.CharField(max_length=80)),
                ("validation", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "workflow",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="wdl_revisions",
                        to="workflows.workflowdocument",
                    ),
                ),
                (
                    "workflow_version",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="wdl_revisions",
                        to="workflows.workflowversion",
                    ),
                ),
            ],
            options={"ordering": ["-version"]},
        ),
        migrations.AddConstraint(
            model_name="wdlrevision",
            constraint=models.UniqueConstraint(
                fields=("workflow", "version"),
                name="unique_workflow_wdl_revision",
            ),
        ),
    ]
