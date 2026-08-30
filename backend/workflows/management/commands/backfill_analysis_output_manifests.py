from __future__ import annotations

import uuid

from django.core.management.base import BaseCommand, CommandError

from workflows.integration_outputs import (
    assert_output_snapshot_storage_writable,
    backfill_output_manifest,
    output_manifest_is_current,
)
from workflows.models import AnalysisRun


class Command(BaseCommand):
    help = "为升级前成功运行补建输出完整性清单（幂等）。"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--limit", type=int)
        parser.add_argument("--after-id")
        parser.add_argument("--actor", default="operator")

    def handle(self, *args, **options) -> None:
        limit = options["limit"]
        if limit is not None and limit < 1:
            raise CommandError("--limit 必须大于 0。")

        after_id = options["after_id"]
        try:
            cursor = uuid.UUID(after_id) if after_id else None
        except ValueError as error:
            raise CommandError("--after-id 必须是有效的运行 UUID。") from error
        candidates = AnalysisRun.objects.filter(
            status=AnalysisRun.Status.SUCCEEDED,
            work_directory__gt="",
        ).order_by("id")
        if cursor is not None:
            candidates = candidates.filter(id__gt=cursor)
        eligible = 0
        backfilled = 0
        failed = 0
        failed_ids: list[str] = []
        last_id = ""
        if not options["dry_run"]:
            try:
                assert_output_snapshot_storage_writable()
            except ValueError as error:
                raise CommandError(str(error)) from error
        for run in candidates.iterator():
            if (
                output_manifest_is_current(run.output_manifest)
                and run.output_status == AnalysisRun.OutputStatus.COMPLETE
            ):
                continue
            if limit is not None and eligible >= limit:
                break
            eligible += 1
            last_id = str(run.id)
            if options["dry_run"]:
                continue
            try:
                if backfill_output_manifest(
                    run,
                    source=f"management-command:{options['actor']}",
                ):
                    backfilled += 1
                else:
                    failed += 1
                    failed_ids.append(str(run.id))
            except (AnalysisRun.DoesNotExist, KeyError, OSError, TypeError, ValueError) as error:
                failed += 1
                failed_ids.append(str(run.id))
                self.stderr.write(f"{run.id}: {error}")

        if options["dry_run"]:
            suffix = f"最后 ID {last_id}。" if last_id else ""
            self.stdout.write(
                f"待补建 {eligible} 条历史成功运行（仅计数，未读取或校验输出）。"
                f"{suffix}"
            )
            return
        if failed:
            raise CommandError(
                f"输出清单补建未完成：成功 {backfilled}，失败 {failed}；"
                f"失败 ID: {', '.join(failed_ids[:20])}；"
                f"最后 ID: {last_id}。"
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"输出清单补建完成：成功 {backfilled}，失败 {failed}；"
                f"最后 ID: {last_id or '-'}。"
            )
        )
