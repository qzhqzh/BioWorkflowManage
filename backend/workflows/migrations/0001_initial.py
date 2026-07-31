from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="WorkflowDocument",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("slug", models.SlugField(max_length=128, unique=True)),
                ("name", models.CharField(max_length=256)),
                ("workflow_graph", models.JSONField(default=dict)),
                ("editor_document", models.JSONField(default=dict)),
                ("tool_specs", models.JSONField(default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["slug"]},
        ),
        migrations.CreateModel(
            name="CompilationRecord",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("request_id", models.CharField(db_index=True, max_length=128)),
                ("status", models.CharField(max_length=32)),
                ("semantic_digest", models.CharField(blank=True, max_length=80)),
                ("validation", models.JSONField(default=dict)),
                ("artifacts", models.JSONField(default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "workflow",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="compilations",
                        to="workflows.workflowdocument",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]

