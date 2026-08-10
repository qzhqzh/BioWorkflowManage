from django.conf import settings
import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone


def preserve_run_owners_and_close_legacy_active_runs(apps, schema_editor):
    AnalysisRun = apps.get_model("workflows", "AnalysisRun")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    username_field = getattr(User, "USERNAME_FIELD", "username")
    users = User.objects.values_list(username_field, "pk")
    for actor, user_id in users.iterator():
        AnalysisRun.objects.filter(actor=actor).update(submitted_by_id=user_id)
    AnalysisRun.objects.filter(status__in=["preparing", "running"]).update(
        status="failed",
        progress=100,
        current_step="升级后需手动重跑",
        error="运行服务已升级；旧 worker 租约无法确认，为避免重复计算请手动重跑。",
        finished_at=timezone.now(),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0011_analysis_run_sources_and_lease"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="workflowversion",
            name="compiled_bundle",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="workflowversion",
            name="compiled_digest",
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name="workflowversion",
            name="compiler_profile",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="analysisrun",
            name="submitted_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="submitted_analysis_runs",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(
            preserve_run_owners_and_close_legacy_active_runs,
            migrations.RunPython.noop,
        ),
    ]
