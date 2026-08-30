from __future__ import annotations

import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from workflows.object_input_gc import (
    ObjectInputCacheGCError,
    garbage_collect_object_input_cache,
)


class Command(BaseCommand):
    help = "Plan or explicitly apply safe retention GC to object input cache files."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument(
            "--all-eligible",
            action="store_true",
            help="Ignore watermarks and select every retention-eligible file.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=settings.ANALYSIS_OBJECT_STAGE_GC_MAX_FILES,
        )
        parser.add_argument("--actor", default="deployment")

    def handle(self, *args, **options):
        limit = int(options["limit"])
        if limit < 1:
            raise CommandError("--limit 必须大于 0。")
        try:
            result = garbage_collect_object_input_cache(
                apply=bool(options["apply"]),
                all_eligible=bool(options["all_eligible"]),
                limit=limit,
                actor=str(options["actor"] or "deployment"),
            )
        except ObjectInputCacheGCError as error:
            details = (
                " " + json.dumps(error.details, ensure_ascii=False, sort_keys=True)
                if error.details
                else ""
            )
            raise CommandError(f"{error.code}: {error}{details}") from error
        self.stdout.write(
            "APPLY object input cache files may be deleted"
            if options["apply"]
            else "DRY_RUN no object input cache files were deleted"
        )
        for candidate in result.pop("candidates"):
            self.stdout.write(
                json.dumps(
                    {"action": "delete", **candidate},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        self.stdout.write(
            "SUMMARY "
            + json.dumps(result, ensure_ascii=False, sort_keys=True)
        )
