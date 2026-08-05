from __future__ import annotations

from django.core.management import BaseCommand, call_command


class Command(BaseCommand):
    help = (
        "Initialize deterministic test users and workflow data. "
        "Optionally import the historical tumor WDL bundle from a local source."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-users",
            action="store_true",
            help="Do not create the default local test users.",
        )
        parser.add_argument(
            "--wdl-source-dir",
            help="Optional directory containing SolidTumorWithMRD.wdl and BloodTumorWithMRD.wdl.",
        )
        parser.add_argument(
            "--repository",
            default="test-fixture",
            help="Repository label recorded for an optional historical WDL import.",
        )
        parser.add_argument(
            "--revision",
            default="test-fixture",
            help="Revision label recorded for an optional historical WDL import.",
        )
        parser.add_argument(
            "--actor",
            default="test-fixture",
            help="Actor recorded for an optional historical WDL import.",
        )

    def handle(self, *args, **options):
        if not options["skip_users"]:
            call_command("seed_users")

        call_command("seed_demo")

        source_dir = options.get("wdl_source_dir")
        if source_dir:
            call_command(
                "import_tumor_wdl",
                source_dir=source_dir,
                repository=options["repository"],
                revision=options["revision"],
                actor=options["actor"],
            )

        self.stdout.write(
            self.style.SUCCESS(
                "Test data ready: users and Phase 1 workflows initialized"
                + ("; historical WDL bundle imported." if source_dir else ".")
            )
        )
