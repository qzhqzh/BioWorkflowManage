from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("workflows", "0008_wdl_tool_packages")]

    operations = [
        migrations.AlterField(
            model_name="wdlsourcerevision",
            name="operation",
            field=models.CharField(
                choices=[
                    ("import", "Imported"),
                    ("edit", "Edited"),
                    ("format", "Formatted"),
                    ("package_link", "Linked tool package"),
                ],
                max_length=16,
            ),
        ),
        migrations.CreateModel(
            name="WDLSourcePackageReference",
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
                ("mount_prefix", models.CharField(blank=True, max_length=384)),
                ("digest", models.CharField(max_length=80)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "package_version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="source_references",
                        to="workflows.wdltoolpackageversion",
                    ),
                ),
                (
                    "revision",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="package_references",
                        to="workflows.wdlsourcerevision",
                    ),
                ),
            ],
            options={
                "ordering": ["mount_prefix", "package_version__package__slug"],
            },
        ),
        migrations.AddConstraint(
            model_name="wdlsourcepackagereference",
            constraint=models.UniqueConstraint(
                fields=("revision", "package_version", "mount_prefix"),
                name="unique_wdl_source_package_reference",
            ),
        ),
    ]
