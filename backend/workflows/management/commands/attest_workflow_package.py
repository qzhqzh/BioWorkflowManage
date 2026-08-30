from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from workflows.analysis_products import (
    AnalysisProductError,
    attest_workflow_package,
)
from workflows.models import (
    WorkflowDocument,
    WorkflowPackageAttestation,
    WorkflowVersion,
)


class Command(BaseCommand):
    help = "Record immutable evidence for a host-verified Sigstore workflow package."

    def add_arguments(self, parser):
        parser.add_argument("--workflow-version-id", required=True, type=int)
        parser.add_argument("--source-digest", required=True)
        parser.add_argument("--statement-digest", required=True)
        parser.add_argument("--signature-bundle-digest", required=True)
        parser.add_argument("--signer-identity", required=True)
        parser.add_argument("--actor", default="analysis-node-installer")

    def handle(self, *args, **options):
        version = (
            WorkflowVersion.objects.select_related("workflow")
            .filter(
                pk=options["workflow_version_id"],
                kind=WorkflowDocument.Kind.WORKFLOW,
                workflow__kind=WorkflowDocument.Kind.WORKFLOW,
            )
            .first()
        )
        if version is None:
            raise CommandError("未找到可证明的 WorkflowVersion。")
        try:
            _, created = attest_workflow_package(
                version,
                verification_method=(
                    WorkflowPackageAttestation.VerificationMethod.SIGSTORE
                ),
                source_digest=str(options["source_digest"]),
                statement_digest=str(options["statement_digest"]),
                signature_bundle_digest=str(options["signature_bundle_digest"]),
                signer_identity=str(options["signer_identity"]),
                actor=str(options["actor"]),
            )
        except AnalysisProductError as error:
            raise CommandError(f"{error.code}: {error}") from error
        action = "ATTESTED" if created else "REUSED"
        self.stdout.write(
            self.style.SUCCESS(
                f"{action} workflow_version={version.pk} "
                f"source_digest={version.compiled_digest}"
            )
        )
