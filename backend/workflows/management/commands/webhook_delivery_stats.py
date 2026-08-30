from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from workflows.models import WebhookDelivery
from workflows.webhooks import webhook_delivery_metrics


class Command(BaseCommand):
    help = "Print machine-readable WebhookDelivery queue and dead-letter metrics."

    def add_arguments(self, parser):
        parser.add_argument("--state", choices=WebhookDelivery.State.values)
        parser.add_argument("--client-id")
        parser.add_argument("--limit", type=int, default=50)

    def handle(self, *args, **options):
        payload = webhook_delivery_metrics()
        selected_state = options.get("state")
        if selected_state:
            deliveries = (
                WebhookDelivery.objects.select_related(
                    "event",
                    "event__run",
                    "endpoint",
                    "endpoint__service_account",
                )
                .filter(state=selected_state)
                .order_by("-updated_at", "-created_at")
            )
            client_id = str(options.get("client_id") or "").strip().lower()
            if client_id:
                deliveries = deliveries.filter(
                    endpoint__service_account__client_id=client_id
                )
            limit = min(200, max(1, int(options["limit"])))
            payload["deliveries"] = [
                {
                    "delivery_id": str(delivery.id),
                    "event_id": str(delivery.event_id),
                    "run_id": str(delivery.event.run_id),
                    "client_id": delivery.endpoint.service_account.client_id,
                    "endpoint": delivery.endpoint.name,
                    "state": delivery.state,
                    "attempt_count": delivery.attempt_count,
                    "replay_count": delivery.replay_count,
                    "next_attempt_at": delivery.next_attempt_at.isoformat(),
                    "last_error_code": delivery.last_error_code,
                    "updated_at": delivery.updated_at.isoformat(),
                }
                for delivery in deliveries[:limit]
            ]
        self.stdout.write(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
