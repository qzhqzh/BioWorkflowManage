from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0004_phase2_workflow_wdl"),
    ]

    operations = [
        migrations.CreateModel(
            name="ToolDocument",
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
                ("tool_id", models.CharField(max_length=256, unique=True)),
                ("draft_spec", models.JSONField(default=dict)),
                ("validation", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["tool_id"]},
        ),
    ]
