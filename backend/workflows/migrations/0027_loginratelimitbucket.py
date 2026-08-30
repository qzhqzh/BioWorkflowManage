from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0026_wdlsourceconflict_unique_assigned_open_wdl_source_conflict"),
    ]

    operations = [
        migrations.CreateModel(
            name="LoginRateLimitBucket",
            fields=[
                ("key", models.CharField(max_length=64, primary_key=True, serialize=False)),
                ("window_started_at", models.DateTimeField()),
                ("request_count", models.PositiveIntegerField(default=0)),
                ("updated_at", models.DateTimeField(auto_now=True, db_index=True)),
            ],
        ),
    ]
