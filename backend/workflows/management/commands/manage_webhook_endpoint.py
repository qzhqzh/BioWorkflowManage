from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_slug
from django.db import transaction

from workflows.models import ServiceAccount, WebhookEndpoint
from workflows.webhooks import (
    TERMINAL_EVENT_TYPE,
    WebhookError,
    resolve_webhook_target,
    webhook_secret_token,
)


class Command(BaseCommand):
    help = "Create/update a Service Account webhook endpoint and rotate its signing secret."

    def add_arguments(self, parser):
        parser.add_argument("--client-id", required=True)
        parser.add_argument("--name", required=True)
        parser.add_argument("--url")
        parser.add_argument("--event", action="append", default=[])
        parser.add_argument("--actor", default="deployment")
        parser.add_argument("--rotate-secret", action="store_true")
        active = parser.add_mutually_exclusive_group()
        active.add_argument("--activate", action="store_true")
        active.add_argument("--deactivate", action="store_true")

    def handle(self, *args, **options):
        client_id = str(options["client_id"] or "").strip().lower()
        name = str(options["name"] or "").strip().lower()
        try:
            validate_slug(name)
        except ValidationError as error:
            raise CommandError("name 必须是有效 slug。") from error
        if not client_id or not name or len(name) > 128:
            raise CommandError("client-id 与 1-128 字符的 name 不能为空。")
        account = ServiceAccount.objects.filter(client_id=client_id).first()
        if account is None:
            raise CommandError("Service Account 不存在。")
        current = WebhookEndpoint.objects.filter(
            service_account=account,
            name=name,
        ).first()
        url_option = options.get("url")
        explicit_url = str(url_option).strip() if url_option is not None else None
        candidate_url = explicit_url if explicit_url is not None else (
            current.url if current else ""
        )
        if not candidate_url:
            raise CommandError("新建 endpoint 必须提供 --url。")
        requested_active = (
            True
            if options["activate"]
            else False
            if options["deactivate"]
            else None
        )
        should_validate_url = current is None or url_option is not None or requested_active is True
        if should_validate_url:
            try:
                target = resolve_webhook_target(candidate_url)
            except WebhookError as error:
                raise CommandError(f"{error.code}: {error}") from error
        else:
            target = None
        requested_events = list(
            dict.fromkeys(str(value).strip() for value in options["event"])
        )
        event_types = requested_events or (
            current.event_types if current else [TERMINAL_EVENT_TYPE]
        )
        if (
            not isinstance(event_types, list)
            or not event_types
            or set(event_types) != {TERMINAL_EVENT_TYPE}
        ):
            raise CommandError(f"当前只支持 --event {TERMINAL_EVENT_TYPE}。")
        may_reveal_secret = current is None or bool(options["rotate_secret"])
        signing_key = str(settings.WEBHOOK_SIGNING_KEY or "").encode("utf-8")
        if may_reveal_secret and len(signing_key) < 32:
            raise CommandError("WEBHOOK_SIGNING_KEY 至少需要 32 个 UTF-8 字节。")

        actor = str(options.get("actor") or "deployment")[:256]
        with transaction.atomic():
            locked_account = ServiceAccount.objects.select_for_update().get(pk=account.pk)
            endpoint, created = WebhookEndpoint.objects.select_for_update().get_or_create(
                service_account=locked_account,
                name=name,
                defaults={
                    "url": candidate_url,
                    "event_types": event_types,
                    "created_by": actor,
                },
            )
            changed = []
            if explicit_url is not None and endpoint.url != candidate_url:
                endpoint.url = candidate_url
                changed.append("url")
            if requested_events and endpoint.event_types != event_types:
                endpoint.event_types = event_types
                changed.append("event_types")
            if requested_active is not None and endpoint.is_active != requested_active:
                endpoint.is_active = requested_active
                changed.append("is_active")
            if options["rotate_secret"]:
                endpoint.secret_salt = uuid.uuid4()
                endpoint.secret_version += 1
                changed.extend(["secret_salt", "secret_version"])
            if changed:
                endpoint.save(update_fields=[*changed, "updated_at"])
        reveal_secret = created or bool(options["rotate_secret"])

        self.stdout.write(
            f"{'CREATED' if created else 'UPDATED'} {client_id}/{name} "
            f"active={endpoint.is_active} secret_version={endpoint.secret_version}"
        )
        if target is not None:
            self.stdout.write(f"TARGET_ADDRESS={target.address}")
        if reveal_secret:
            self.stdout.write(f"SIGNING_SECRET={webhook_secret_token(endpoint)}")
            self.stdout.write("签名密钥只显示本次，请立即保存到接收方密钥管理系统。")
