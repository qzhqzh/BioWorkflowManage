from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from workflows.artifact_exports import (
    ArtifactExportError,
    claim_next_output_cleanup,
    clean_analysis_output,
    output_cleanup_candidates,
)


class Command(BaseCommand):
    help = "Dry-run or explicitly clean locally retained AnalysisRun output trees."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument(
            "--all-eligible",
            action="store_true",
            help="Required with --apply when no exact --run-id is supplied.",
        )
        parser.add_argument("--run-id")
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--actor", default="deployment")

    def handle(self, *args, **options):
        limit = max(1, min(1000, int(options["limit"])))
        run_id = options.get("run_id")
        if options["all_eligible"] and not options["apply"]:
            raise CommandError("--all-eligible 只能与 --apply 同时使用。")
        if options["all_eligible"] and run_id is not None:
            raise CommandError("--all-eligible 与 --run-id 不能同时使用。")
        if options["apply"] and run_id is None and not options["all_eligible"]:
            raise CommandError(
                "批量清理必须同时显式提供 --apply --all-eligible。"
            )
        if not options["apply"]:
            try:
                candidates = output_cleanup_candidates(
                    run_id=run_id,
                    limit=limit,
                )
            except ArtifactExportError as error:
                raise CommandError(f"{error.code}: {error}") from error
            self.stdout.write("DRY_RUN no files were deleted")
            for candidate in candidates:
                self.stdout.write(
                    json.dumps(candidate, ensure_ascii=False, sort_keys=True)
                )
            return

        processed = 0
        processed_run_ids = set()
        released = 0
        failures = 0
        while processed < limit:
            try:
                retention = claim_next_output_cleanup(
                    run_id=run_id,
                    exclude_run_ids=processed_run_ids,
                )
            except ArtifactExportError as error:
                raise CommandError(f"{error.code}: {error}") from error
            if retention is None:
                break
            processed_run_ids.add(retention.run_id)
            finalized, item_released, error = clean_analysis_output(
                retention,
                actor=str(options["actor"] or "deployment"),
            )
            if not finalized:
                failures += 1
                self.stderr.write(f"LEASE_LOST run_id={retention.run_id}")
            elif error is not None:
                failures += 1
                self.stderr.write(
                    f"FAILED run_id={retention.run_id} code={error.code}"
                )
            else:
                released += item_released
                self.stdout.write(
                    f"CLEANED run_id={retention.run_id} released_bytes={item_released}"
                )
            processed += 1
            if run_id is not None:
                break
        self.stdout.write(
            f"SUMMARY processed={processed} failures={failures} released_bytes={released}"
        )
        if failures:
            raise CommandError(f"输出清理有 {failures} 个失败项。")
