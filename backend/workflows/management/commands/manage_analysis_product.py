from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_slug
from django.db import transaction

from workflows.analysis_products import (
    AnalysisProductError,
    publish_analysis_product_version,
)
from workflows.models import AnalysisProduct, WorkflowDocument, WorkflowVersion


class Command(BaseCommand):
    help = "Create/update an Analysis Product and publish immutable contract versions."

    def add_arguments(self, parser):
        parser.add_argument("--code", required=True)
        parser.add_argument("--name")
        parser.add_argument("--description")
        parser.add_argument("--contract-version")
        parser.add_argument("--workflow-version-id", type=int)
        parser.add_argument("--actor", default="deployment")
        active = parser.add_mutually_exclusive_group()
        active.add_argument("--activate", action="store_true")
        active.add_argument("--deactivate", action="store_true")

    def handle(self, *args, **options):
        code = str(options["code"] or "").strip().lower()
        try:
            validate_slug(code)
        except ValidationError as error:
            raise CommandError("code 必须是有效 slug。") from error
        if not code or len(code) > 128:
            raise CommandError("code 必须是 1-128 字符的有效 slug。")

        contract_version = options.get("contract_version")
        workflow_version_id = options.get("workflow_version_id")
        if bool(contract_version) != bool(workflow_version_id):
            raise CommandError(
                "--contract-version 与 --workflow-version-id 必须同时提供。"
            )

        workflow_version = None
        if contract_version and workflow_version_id:
            workflow_version = (
                WorkflowVersion.objects.select_related("workflow")
                .filter(
                    pk=workflow_version_id,
                    kind=WorkflowDocument.Kind.WORKFLOW,
                    workflow__kind=WorkflowDocument.Kind.WORKFLOW,
                )
                .first()
            )
            if workflow_version is None:
                raise CommandError("未找到可发布的 WorkflowVersion。")

        actor = str(options.get("actor") or "deployment")[:256]
        item = None
        version_created = False
        try:
            with transaction.atomic():
                product, created = (
                    AnalysisProduct.objects.select_for_update().get_or_create(
                        code=code,
                        defaults={
                            "name": str(options.get("name") or code).strip(),
                            "description": str(options.get("description") or ""),
                            "created_by": actor,
                        },
                    )
                )
                changed = []
                if options.get("name") is not None:
                    name = str(options["name"]).strip()
                    if not name:
                        raise CommandError("name 不能为空。")
                    if product.name != name:
                        product.name = name
                        changed.append("name")
                if options.get("description") is not None:
                    description = str(options["description"])
                    if product.description != description:
                        product.description = description
                        changed.append("description")
                requested_active = (
                    True
                    if options["activate"]
                    else False
                    if options["deactivate"]
                    else None
                )
                if (
                    requested_active is not None
                    and product.is_active != requested_active
                ):
                    product.is_active = requested_active
                    changed.append("is_active")
                if changed:
                    product.save(update_fields=[*changed, "updated_at"])
                if workflow_version is not None:
                    item, version_created = publish_analysis_product_version(
                        product,
                        contract_version=contract_version,
                        workflow_version=workflow_version,
                        actor=actor,
                    )
        except AnalysisProductError as error:
            raise CommandError(f"{error.code}: {error}") from error

        self.stdout.write(
            f"{'CREATED' if created else 'UPDATED'} {product.code} "
            f"active={product.is_active}"
        )
        if item is not None:
            self.stdout.write(
                f"{'PUBLISHED' if version_created else 'REUSED'} "
                f"{product.code}@{item.contract_version} "
                f"workflow_version={item.workflow_version_id}"
            )
