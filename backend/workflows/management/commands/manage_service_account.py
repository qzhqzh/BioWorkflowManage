from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from workflows.integration_tokens import issue_service_token, validate_service_scopes
from workflows.models import ServiceAccount, ServiceToken


class Command(BaseCommand):
    help = "Create/update a Service Account and issue or revoke bearer tokens."

    def add_arguments(self, parser):
        parser.add_argument("--client-id", required=True)
        parser.add_argument("--name")
        parser.add_argument("--scope", action="append", default=[])
        parser.add_argument("--actor", default="deployment")
        parser.add_argument("--token-name", default="default")
        parser.add_argument("--expires-days", type=int)
        parser.add_argument("--issue-token", action="store_true")
        parser.add_argument("--revoke-prefix")
        active = parser.add_mutually_exclusive_group()
        active.add_argument("--activate", action="store_true")
        active.add_argument("--deactivate", action="store_true")

    def handle(self, *args, **options):
        client_id = str(options["client_id"]).strip().lower()
        name = str(options.get("name") or client_id).strip()
        if not client_id or not name:
            raise CommandError("client-id 和 name 不能为空。")
        try:
            scopes = validate_service_scopes(options["scope"])
        except ValueError as error:
            raise CommandError(str(error)) from error
        if not scopes and not ServiceAccount.objects.filter(client_id=client_id).exists():
            raise CommandError("新建 Service Account 至少需要一个 --scope。")

        with transaction.atomic():
            account, created = ServiceAccount.objects.select_for_update().get_or_create(
                client_id=client_id,
                defaults={
                    "name": name,
                    "scopes": scopes,
                    "created_by": options["actor"],
                },
            )
            changed = []
            if account.name != name:
                account.name = name
                changed.append("name")
            if scopes and account.scopes != scopes:
                account.scopes = scopes
                changed.append("scopes")
            requested_active = (
                True
                if options["activate"]
                else False
                if options["deactivate"]
                else None
            )
            if requested_active is not None and account.is_active != requested_active:
                account.is_active = requested_active
                changed.append("is_active")
            if changed:
                account.save(update_fields=[*changed, "updated_at"])

            revoke_prefix = str(options.get("revoke_prefix") or "").strip()
            if revoke_prefix:
                updated = ServiceToken.objects.filter(
                    service_account=account,
                    prefix=revoke_prefix,
                    revoked_at__isnull=True,
                ).update(revoked_at=timezone.now())
                if updated != 1:
                    raise CommandError("未找到可吊销的 Token prefix。")

        self.stdout.write(
            f"{'CREATED' if created else 'UPDATED'} {account.client_id} "
            f"active={account.is_active} scopes={','.join(account.scopes)}"
        )
        if options["issue_token"]:
            expires_at = None
            if options["expires_days"] is not None:
                if options["expires_days"] < 1:
                    raise CommandError("expires-days 必须大于 0。")
                expires_at = timezone.now() + timedelta(days=options["expires_days"])
            token, raw_token = issue_service_token(
                account,
                name=str(options["token_name"]).strip() or "default",
                actor=options["actor"],
                expires_at=expires_at,
            )
            self.stdout.write(f"TOKEN_PREFIX={token.prefix}")
            self.stdout.write(f"TOKEN={raw_token}")
            self.stdout.write("Token 只显示本次，请立即保存到安全的密钥管理系统。")
