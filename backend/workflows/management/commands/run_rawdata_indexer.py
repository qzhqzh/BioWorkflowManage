from __future__ import annotations

import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from workflows.models import RawdataScan
from workflows.rawdata_index import (
    ensure_periodic_rawdata_scan,
    queue_rawdata_scan,
    rawdata_root_key,
    run_rawdata_scan_batch,
)


class Command(BaseCommand):
    help = "Build and refresh the persistent rawdata FASTQ index."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Queue one scan, finish it, print the result, and exit.",
        )
        parser.add_argument(
            "--poll-seconds",
            type=float,
            default=2.0,
            help="Worker polling interval (default: 2 seconds).",
        )

    def handle(self, *args, **options):
        if options["once"]:
            scan, _ = queue_rawdata_scan(
                actor="command",
                trigger="manual",
                root_value=settings.ANALYSIS_RAWDATA_ROOT,
            )
            for _ in range(100_000):
                scan = run_rawdata_scan_batch(settings.ANALYSIS_RAWDATA_ROOT) or scan
                scan.refresh_from_db()
                if scan.status not in {
                    RawdataScan.Status.QUEUED,
                    RawdataScan.Status.RUNNING,
                }:
                    if scan.status == RawdataScan.Status.FAILED:
                        raise CommandError(scan.error or "Rawdata scan failed.")
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"{scan.status} {scan.id}: "
                            f"{scan.scanned_entry_count} entries, "
                            f"{len(scan.catalog.get('datasets', []))} datasets"
                        )
                    )
                    return
            raise CommandError("Rawdata scan exceeded the maximum batch count.")

        poll_seconds = max(0.25, float(options["poll_seconds"]))
        self.stdout.write(
            f"Rawdata indexer watching {rawdata_root_key()} every "
            f"{settings.RAWDATA_INDEX_INTERVAL_SECONDS}s"
        )
        while True:
            ensure_periodic_rawdata_scan(settings.ANALYSIS_RAWDATA_ROOT)
            scan = run_rawdata_scan_batch(settings.ANALYSIS_RAWDATA_ROOT)
            if scan is None:
                time.sleep(poll_seconds)
