from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from workflows.artifact_exports import ArtifactExportError, replay_artifact_export


class Command(BaseCommand):
    help = "Replay one failed ArtifactExport with the same ID and destination keys."

    def add_arguments(self, parser):
        parser.add_argument("--export-id", required=True)
        parser.add_argument("--actor", default="deployment")

    def handle(self, *args, **options):
        try:
            export = replay_artifact_export(
                options["export_id"],
                actor=options["actor"],
            )
        except ArtifactExportError as error:
            raise CommandError(f"{error.code}: {error}") from error
        self.stdout.write(
            f"REPLAY_QUEUED {export.id} replay_count={export.replay_count}"
        )
