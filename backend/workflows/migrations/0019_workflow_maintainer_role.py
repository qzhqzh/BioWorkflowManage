from django.db import migrations


MAINTAINER_USERNAMES = (
    "zhangrusong",
    "hejingjing",
    "zhuying",
    "hangzhili",
)


def assign_existing_maintainers(apps, schema_editor):
    group_model = apps.get_model("auth", "Group")
    user_model = apps.get_model("auth", "User")
    maintainer_group, _ = group_model.objects.get_or_create(name="workflow-maintainers")
    operator_group, _ = group_model.objects.get_or_create(name="analysis-operators")
    for user in user_model.objects.filter(username__in=MAINTAINER_USERNAMES):
        user.groups.add(maintainer_group)
        user.groups.remove(operator_group)


def unassign_existing_maintainers(apps, schema_editor):
    group_model = apps.get_model("auth", "Group")
    user_model = apps.get_model("auth", "User")
    maintainer_group = group_model.objects.filter(name="workflow-maintainers").first()
    operator_group, _ = group_model.objects.get_or_create(name="analysis-operators")
    if maintainer_group is None:
        return
    for user in user_model.objects.filter(username__in=MAINTAINER_USERNAMES):
        user.groups.remove(maintainer_group)
        user.groups.add(operator_group)


class Migration(migrations.Migration):
    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("workflows", "0018_tool_document_concurrency"),
    ]

    operations = [
        migrations.RunPython(
            assign_existing_maintainers,
            unassign_existing_maintainers,
        ),
    ]
