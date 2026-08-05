from __future__ import annotations

import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from workflows.analysis_runtime import claim_next_run, process_analysis_run


class Command(BaseCommand):
    help = "Poll queued analysis runs and execute them with miniwdl."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Process at most one queued run, then exit.",
        )
        parser.add_argument(
            "--poll-interval",
            type=float,
            default=settings.ANALYSIS_WORKER_POLL_SECONDS,
        )

    def handle(self, *args, **options):
        once = options["once"]
        interval = max(0.2, float(options["poll_interval"]))
        self.stdout.write("analysis-worker started")
        while True:
            close_old_connections()
            run = claim_next_run()
            if run is not None:
                self.stdout.write(f"processing analysis run {run.id}")
                process_analysis_run(run)
                self.stdout.write(f"finished analysis run {run.id}")
                if once:
                    return
                continue
            if once:
                self.stdout.write("no queued analysis run")
                return
            time.sleep(interval)
