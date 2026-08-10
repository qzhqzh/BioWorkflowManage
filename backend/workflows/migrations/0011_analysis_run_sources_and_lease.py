import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0010_analysis_runs"),
    ]

    operations = [
        migrations.AlterField(
            model_name="analysisrun",
            name="asset",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="analysis_runs",
                to="workflows.wdlasset",
            ),
        ),
        migrations.AlterField(
            model_name="analysisrun",
            name="revision",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="analysis_runs",
                to="workflows.wdlsourcerevision",
            ),
        ),
        migrations.AddField(
            model_name="analysisrun",
            name="workflow_version",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="analysis_runs",
                to="workflows.workflowversion",
            ),
        ),
        migrations.AddField(
            model_name="analysisrun",
            name="source_bundle",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="analysisrun",
            name="source_digest",
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name="analysisrun",
            name="attempt_count",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="analysisrun",
            name="lease_token",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="analysisrun",
            name="worker_heartbeat_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="analysisrun",
            name="lease_expires_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="analysisrun",
            constraint=models.CheckConstraint(
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
            ),
        ),
    ]
