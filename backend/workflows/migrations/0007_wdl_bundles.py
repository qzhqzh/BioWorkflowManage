from django.db import migrations, models
import django.db.models.deletion


def populate_legacy_source_files(apps, schema_editor):
    WDLSourceFile = apps.get_model("workflows", "WDLSourceFile")
    WDLSourceRevision = apps.get_model("workflows", "WDLSourceRevision")
    for revision in WDLSourceRevision.objects.select_related("asset").iterator():
        WDLSourceFile.objects.create(
            revision=revision,
            path=revision.asset.source_filename,
            content=revision.content,
            digest=revision.digest,
            is_entry=True,
            analysis=revision.analysis,
        )


class Migration(migrations.Migration):
    dependencies = [("workflows", "0006_wdlasset_wdltag_wdlsourcerevision_wdlauditevent_and_more")]

    operations = [
        migrations.AddField(
            model_name="wdlasset",
            name="source_repository",
            field=models.CharField(blank=True, max_length=512),
        ),
        migrations.AddField(
            model_name="wdlasset",
            name="source_revision",
            field=models.CharField(blank=True, max_length=128),
        ),
        migrations.CreateModel(
            name="WDLSourceFile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("path", models.CharField(max_length=512)),
                ("content", models.TextField()),
                ("digest", models.CharField(max_length=80)),
                ("is_entry", models.BooleanField(default=False)),
                ("analysis", models.JSONField(default=dict)),
                (
                    "revision",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="files",
                        to="workflows.wdlsourcerevision",
                    ),
                ),
            ],
            options={"ordering": ["path"]},
        ),
        migrations.AddConstraint(
            model_name="wdlsourcefile",
            constraint=models.UniqueConstraint(
                fields=("revision", "path"),
                name="unique_wdl_source_file_path",
            ),
        ),
        migrations.RunPython(populate_legacy_source_files, migrations.RunPython.noop),
    ]
