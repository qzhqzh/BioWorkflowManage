from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from compiler_core import canonical_digest, validate_tool_spec
from workflows.annotation_tools import (
    ANNOVAR_ANNOTATION_VERSION,
    ANNOVAR_TOOL_ID,
    enhance_annosnv_spec,
)
from workflows.models import ToolDocument, ToolVersion


class Command(BaseCommand):
    help = "Enhance known annotation task drafts and optionally publish immutable versions."

    def add_arguments(self, parser):
        parser.add_argument("--publish", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        document = ToolDocument.objects.filter(tool_id=ANNOVAR_TOOL_ID).first()
        if document is None:
            raise CommandError(
                f"Tool draft not found: {ANNOVAR_TOOL_ID}. Extract the solid-tumor tool package first."
            )

        spec = enhance_annosnv_spec(document.draft_spec)
        validation = validate_tool_spec(spec)
        if validation["status"] != "valid":
            messages = "; ".join(
                item["message"]
                for item in validation["diagnostics"]
                if item["severity"] == "error"
            )
            raise CommandError(f"Enhanced annotation ToolSpec is invalid: {messages}")

        document.draft_spec = spec
        document.validation = validation
        document.save(update_fields=["draft_spec", "validation", "updated_at"])
        self.stdout.write(
            self.style.SUCCESS(
                f"READY {ANNOVAR_TOOL_ID}@{ANNOVAR_ANNOTATION_VERSION}"
            )
        )

        if not options["publish"]:
            return

        digest = canonical_digest(spec)
        existing = ToolVersion.objects.filter(
            tool_id=ANNOVAR_TOOL_ID,
            version=ANNOVAR_ANNOTATION_VERSION,
        ).first()
        if existing is not None:
            if existing.digest != digest:
                raise CommandError(
                    f"Published version has different content: {ANNOVAR_TOOL_ID}@{ANNOVAR_ANNOTATION_VERSION}"
                )
            self.stdout.write("UNCHANGED published version already matches")
            return

        ToolVersion.objects.create(
            tool_id=ANNOVAR_TOOL_ID,
            version=ANNOVAR_ANNOTATION_VERSION,
            name=spec["display_name"],
            digest=digest,
            tool_spec=spec,
        )
        self.stdout.write(self.style.SUCCESS(f"PUBLISHED sha256 {digest}"))
