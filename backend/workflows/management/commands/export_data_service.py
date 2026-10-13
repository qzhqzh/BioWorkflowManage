"""Read-only export of BWM's data-owned state; never includes service credentials."""

import hashlib
import json
import os

from django.core import serializers
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from workflows.models import (
    RawdataBatchReadiness,
    RawdataDatasetEvent,
    RawdataDatasetIndex,
    RawdataScan,
)
from workflows.rawdata_index import rawdata_root_key


class Command(BaseCommand):
    help = "Export quiescent rawdata state to a NEW mode-0600 file for the independent service."

    def add_arguments(self, parser):
        parser.add_argument("--output", required=True)

    def handle(self, *args, **options):
        root_key = rawdata_root_key()
        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                    )
            if RawdataScan.objects.filter(
                root_key=root_key, status__in=["queued", "running"]
            ).exists():
                raise CommandError(
                    "Finish active scans and stop the legacy indexer before exporting."
                )
            records = []
            for model in (
                RawdataScan,
                RawdataBatchReadiness,
                RawdataDatasetIndex,
                RawdataDatasetEvent,
            ):
                query = (
                    {"dataset__root_key": root_key}
                    if model is RawdataDatasetEvent
                    else {"root_key": root_key}
                )
                rows = json.loads(
                    serializers.serialize(
                        "json", model.objects.filter(**query).order_by("pk")
                    )
                )
                for row in rows:
                    row["model"] = row["model"].replace(
                        "workflows.", "data_service.", 1
                    )
                    if model is RawdataDatasetEvent:
                        run_id = row["fields"].pop("run", None)
                        row["fields"]["external_reference"] = (
                            {"service": "bwm", "run_id": str(run_id)} if run_id else {}
                        )
                records.extend(rows)
        digest = hashlib.sha256(
            json.dumps(records, sort_keys=True).encode()
        ).hexdigest()
        payload = {
            "schema_version": 1,
            "root_key": root_key,
            "records_sha256": digest,
            "records": records,
        }
        try:
            with os.fdopen(
                os.open(options["output"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                "w",
            ) as output:
                json.dump(payload, output, ensure_ascii=False)
        except OSError:
            raise CommandError(
                "Choose a new writable output file; existing files are never overwritten."
            ) from None
        self.stdout.write(
            f"Exported {len(records)} records. Preserve root identifier {root_key}."
        )
