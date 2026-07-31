import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("workflows", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="ToolVersion",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("tool_id", models.CharField(max_length=256)),
                ("version", models.CharField(max_length=64)),
                ("name", models.CharField(max_length=256)),
                ("digest", models.CharField(max_length=80)),
                ("tool_spec", models.JSONField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["tool_id", "-created_at"]},
        ),
        migrations.CreateModel(
            name="WorkflowVersion",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("version", models.PositiveIntegerField()),
                ("name", models.CharField(max_length=256)),
                ("semantic_digest", models.CharField(max_length=80)),
                ("workflow_graph", models.JSONField()),
                ("editor_document", models.JSONField(default=dict)),
                ("tool_specs", models.JSONField(default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "workflow",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="versions",
                        to="workflows.workflowdocument",
                    ),
                ),
            ],
            options={"ordering": ["-version"]},
        ),
        migrations.AddField(
            model_name="compilationrecord",
            name="workflow_version",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="compilations",
                to="workflows.workflowversion",
            ),
        ),
        migrations.AddConstraint(
            model_name="toolversion",
            constraint=models.UniqueConstraint(
                fields=("tool_id", "version"),
                name="unique_tool_version",
            ),
        ),
        migrations.AddConstraint(
            model_name="workflowversion",
            constraint=models.UniqueConstraint(
                fields=("workflow", "version"),
                name="unique_workflow_version",
            ),
        ),
    ]
