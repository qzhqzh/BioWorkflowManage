from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from workflows.artifact_exports import artifact_export_metrics


class Command(BaseCommand):
    help = "Show durable ArtifactExport queue and dead-letter metrics."

    def handle(self, *args, **options):
        del args, options
        self.stdout.write(
            json.dumps(
                artifact_export_metrics(),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
