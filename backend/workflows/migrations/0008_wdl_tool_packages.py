from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("workflows", "0007_wdl_bundles")]

    operations = [
        migrations.CreateModel(
            name="WDLToolPackage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slug", models.SlugField(max_length=128, unique=True)),
                ("name", models.CharField(max_length=256)),
                ("description", models.TextField(blank=True)),
                ("lifecycle", models.CharField(choices=[("active", "Active"), ("archived", "Archived")], default="active", max_length=16)),
                ("created_by", models.CharField(default="local-user", max_length=256)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["-updated_at", "slug"]},
        ),
        migrations.CreateModel(
            name="WDLToolPackageTag",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=64, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="WDLToolPackageVersion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("version", models.CharField(max_length=64)),
                ("digest", models.CharField(max_length=80)),
                ("source_repository", models.CharField(blank=True, max_length=512)),
                ("source_revision", models.CharField(blank=True, max_length=128)),
                ("note", models.TextField(blank=True)),
                ("actor", models.CharField(default="local-user", max_length=256)),
                ("analysis", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("package", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="versions", to="workflows.wdltoolpackage")),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.AddField(
            model_name="wdltoolpackage",
            name="tags",
            field=models.ManyToManyField(blank=True, related_name="packages", to="workflows.wdltoolpackagetag"),
        ),
        migrations.CreateModel(
            name="WDLToolPackageFile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("path", models.CharField(max_length=512)),
                ("content", models.TextField()),
                ("digest", models.CharField(max_length=80)),
                ("analysis", models.JSONField(default=dict)),
                ("package_version", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="files", to="workflows.wdltoolpackageversion")),
            ],
            options={"ordering": ["path"]},
        ),
        migrations.CreateModel(
            name="WDLToolPackageAuditEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action", models.CharField(max_length=32)),
                ("actor", models.CharField(default="local-user", max_length=256)),
                ("note", models.TextField(blank=True)),
                ("changes", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("package", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="audit_events", to="workflows.wdltoolpackage")),
                ("package_version", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="audit_events", to="workflows.wdltoolpackageversion")),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.AddConstraint(
            model_name="wdltoolpackageversion",
            constraint=models.UniqueConstraint(fields=("package", "version"), name="unique_wdl_tool_package_version"),
        ),
        migrations.AddConstraint(
            model_name="wdltoolpackagefile",
            constraint=models.UniqueConstraint(fields=("package_version", "path"), name="unique_wdl_tool_package_file_path"),
        ),
    ]
