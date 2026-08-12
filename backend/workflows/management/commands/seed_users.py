from __future__ import annotations

import os

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError

from workflows.auth_roles import ANALYSIS_OPERATOR_GROUP, WORKFLOW_MAINTAINER_GROUP


DEFAULT_USERNAMES = (
    "zhuqin",
    "zhangrusong",
    "hejingjing",
    "zhuying",
    "hangzhili",
    "chaohuaiyu",
)

ADMIN_USERNAME = "zhuqin"
OPERATOR_USERNAME = "chaohuaiyu"
MAINTAINER_USERNAMES = tuple(
    username
    for username in DEFAULT_USERNAMES
    if username not in {ADMIN_USERNAME, OPERATOR_USERNAME}
)


class Command(BaseCommand):
    help = "Create the default BioWorkflowManage users idempotently."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset-passwords",
            action="store_true",
            help="Reset existing default-user passwords to their username.",
        )

    def handle(self, *args, **options):
        user_model = get_user_model()
        reset_passwords = options["reset_passwords"]
        created_count = 0
        updated_count = 0
        operator_group, _ = Group.objects.get_or_create(name=ANALYSIS_OPERATOR_GROUP)
        maintainer_group, _ = Group.objects.get_or_create(
            name=WORKFLOW_MAINTAINER_GROUP
        )
        allow_default_passwords = (
            os.environ.get("DJANGO_SEED_ALLOW_DEFAULT_PASSWORDS", "0") == "1"
        )

        for username in DEFAULT_USERNAMES:
            user, created = user_model.objects.get_or_create(
                username=username,
                defaults={"is_active": True},
            )
            changed_fields = []
            if username == ADMIN_USERNAME:
                for field in ("is_staff", "is_superuser"):
                    if not getattr(user, field):
                        setattr(user, field, True)
                        changed_fields.append(field)
            if created or reset_passwords or not user.has_usable_password():
                password = os.environ.get(
                    f"DJANGO_SEED_PASSWORD_{username.upper()}", ""
                )
                if not password and allow_default_passwords:
                    password = username
                if not password:
                    if created:
                        user.delete()
                    raise CommandError(
                        f"Set DJANGO_SEED_PASSWORD_{username.upper()} or explicitly "
                        "enable DJANGO_SEED_ALLOW_DEFAULT_PASSWORDS=1 for test data."
                    )
                user.set_password(password)
                changed_fields.append("password")
            if changed_fields:
                user.save(update_fields=changed_fields)
                updated_count += 1
            if created:
                created_count += 1
            if username == ADMIN_USERNAME:
                user.groups.remove(operator_group)
                user.groups.remove(maintainer_group)
            elif username == OPERATOR_USERNAME:
                user.groups.add(operator_group)
                user.groups.remove(maintainer_group)
            elif username in MAINTAINER_USERNAMES:
                user.groups.add(maintainer_group)
                user.groups.remove(operator_group)

        self.stdout.write(
            self.style.SUCCESS(
                f"Ensured {len(DEFAULT_USERNAMES)} default users "
                f"({created_count} created, {updated_count} updated)."
            )
        )
