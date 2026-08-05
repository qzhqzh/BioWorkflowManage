from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


DEFAULT_USERNAMES = (
    "zhuqin",
    "zhangrusong",
    "hejingjing",
    "zhuying",
    "hangzhili",
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

        for username in DEFAULT_USERNAMES:
            user, created = user_model.objects.get_or_create(
                username=username,
                defaults={"is_active": True},
            )
            changed_fields = []
            if not user.is_active:
                user.is_active = True
                changed_fields.append("is_active")
            if created or reset_passwords or not user.has_usable_password():
                user.set_password(username)
                changed_fields.append("password")
            if changed_fields:
                user.save(update_fields=changed_fields)
                updated_count += 1
            if created:
                created_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Ensured {len(DEFAULT_USERNAMES)} default users "
                f"({created_count} created, {updated_count} updated)."
            )
        )
