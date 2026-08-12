from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0017_wdl_graph_proposal"),
    ]

    operations = [
        migrations.AddField(
            model_name="tooldocument",
            name="draft_version",
            field=models.PositiveIntegerField(default=1),
        ),
    ]
