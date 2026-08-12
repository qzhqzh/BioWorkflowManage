import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0015_workflow_document_concurrency"),
    ]

    operations = [
        migrations.AddField(
            model_name="wdlrevision",
            name="base_revision",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="derived_revisions",
                to="workflows.wdlrevision",
            ),
        ),
        migrations.AddField(
            model_name="wdlrevision",
            name="created_by",
            field=models.CharField(default="local-user", max_length=256),
        ),
        migrations.AddField(
            model_name="wdlrevision",
            name="note",
            field=models.TextField(blank=True),
        ),
    ]
