import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0027_loginratelimitbucket"),
    ]

    operations = [
        migrations.CreateModel(
            name="AnalysisProduct",
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
                ("code", models.SlugField(max_length=128, unique=True)),
                ("name", models.CharField(max_length=256)),
                ("description", models.TextField(blank=True)),
                ("is_active", models.BooleanField(default=True)),
                (
                    "created_by",
                    models.CharField(default="deployment", max_length=256),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["code"]},
        ),
        migrations.CreateModel(
            name="AnalysisProductVersion",
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
                ("contract_version", models.CharField(max_length=64)),
                ("source_digest", models.CharField(max_length=80)),
                ("interface_contract", models.JSONField(default=dict)),
                ("contract_digest", models.CharField(max_length=80)),
                (
                    "created_by",
                    models.CharField(default="deployment", max_length=256),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "product",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="versions",
                        to="workflows.analysisproduct",
                    ),
                ),
                (
                    "workflow_version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="analysis_product_versions",
                        to="workflows.workflowversion",
                    ),
                ),
            ],
            options={"ordering": ["product_id", "contract_version"]},
        ),
        migrations.AddField(
            model_name="analysisrun",
            name="analysis_product_version",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="analysis_runs",
                to="workflows.analysisproductversion",
            ),
        ),
        migrations.AddConstraint(
            model_name="analysisproductversion",
            constraint=models.UniqueConstraint(
                fields=("product", "contract_version"),
                name="unique_analysis_product_contract_version",
            ),
        ),
        migrations.AddConstraint(
            model_name="analysisproductversion",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("source_digest", ""), _negated=True),
                    models.Q(("contract_digest", ""), _negated=True),
                ),
                name="analysis_product_version_has_digests",
            ),
        ),
        migrations.AddConstraint(
            model_name="analysisrun",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("run_kind", "workflow"),
                    ("analysis_product_version__isnull", True),
                    _connector="OR",
                ),
                name="tool_test_has_no_analysis_product",
            ),
        ),
    ]
