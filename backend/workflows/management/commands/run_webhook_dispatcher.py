from __future__ import annotations

import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from workflows.webhooks import claim_next_delivery, deliver_webhook


class Command(BaseCommand):
    help = "Deliver durable Integration Outbox events without blocking analysis workers."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Process at most one due delivery, then exit.",
        )
        parser.add_argument(
            "--poll-interval",
            type=float,
            default=settings.WEBHOOK_DELIVERY_POLL_SECONDS,
        )

    def handle(self, *args, **options):
        signing_key = str(settings.WEBHOOK_SIGNING_KEY or "").encode("utf-8")
        if len(signing_key) < 32:
            raise CommandError("WEBHOOK_SIGNING_KEY 至少需要 32 个 UTF-8 字节。")
        once = bool(options["once"])
        interval = max(0.2, float(options["poll_interval"]))
        self.stdout.write("webhook-dispatcher started")
        while True:
            close_old_connections()
            delivery = claim_next_delivery()
            if delivery is None:
                if once:
                    self.stdout.write("no due webhook delivery")
                    return
                time.sleep(interval)
                continue
            self.stdout.write(
                f"delivering webhook {delivery.id} attempt={delivery.attempt_count}"
            )
            finalized = deliver_webhook(delivery)
            if finalized:
                delivery.refresh_from_db()
                self.stdout.write(f"webhook {delivery.id} state={delivery.state}")
            else:
                self.stderr.write(f"webhook {delivery.id} lease was lost")
            if once:
                return
