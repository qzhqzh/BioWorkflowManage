from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from workflows.webhooks import WebhookError, replay_webhook_delivery


class Command(BaseCommand):
    help = "Replay one completed or dead-letter WebhookDelivery with the same delivery ID."

    def add_arguments(self, parser):
        parser.add_argument("--delivery-id", required=True)
        parser.add_argument("--actor", default="deployment")

    def handle(self, *args, **options):
        try:
            delivery = replay_webhook_delivery(
                options["delivery_id"],
                actor=options["actor"],
            )
        except (ValueError, WebhookError) as error:
            code = getattr(error, "code", "WEBHOOK_DELIVERY_ID_INVALID")
            raise CommandError(f"{code}: {error}") from error
        self.stdout.write(
            f"REPLAY_QUEUED {delivery.id} replay_count={delivery.replay_count}"
        )
