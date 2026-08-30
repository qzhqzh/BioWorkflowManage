from __future__ import annotations

import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from workflows.artifact_exports import (
    artifact_export_deadline_supported,
    claim_next_artifact_export,
    deliver_artifact_export,
)


class Command(BaseCommand):
    help = "Deliver immutable analysis artifacts without blocking API or analysis workers."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Process at most one due ArtifactExport, then exit.",
        )
        parser.add_argument(
            "--poll-interval",
            type=float,
            default=settings.ANALYSIS_ARTIFACT_EXPORT_POLL_SECONDS,
        )

    def handle(self, *args, **options):
        if not artifact_export_deadline_supported():
            raise CommandError(
                "artifact-exporter 必须在支持 POSIX wall-clock timer 的主线程运行。"
            )
        once = bool(options["once"])
        interval = max(0.2, float(options["poll_interval"]))
        self.stdout.write("artifact-exporter started")
        while True:
            close_old_connections()
            export = claim_next_artifact_export()
            if export is None:
                if once:
                    self.stdout.write("no due artifact export")
                    return
                time.sleep(interval)
                continue
            self.stdout.write(
                f"exporting artifact {export.id} attempt={export.attempt_count}"
            )
            finalized = deliver_artifact_export(export)
            if finalized:
                export.refresh_from_db()
                self.stdout.write(f"artifact export {export.id} state={export.state}")
            else:
                self.stderr.write(f"artifact export {export.id} lease was lost")
            if once:
                return
