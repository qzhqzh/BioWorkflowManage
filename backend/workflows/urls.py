from django.urls import path
from rest_framework.permissions import AllowAny

from . import (
    analysis_runs,
    api_overrides,
    auth_views,
    integration_api,
    rawdata_catalog,
    resource_catalog_api,
    software_library,
    tool_runs,
    views,
    wdl_assets,
    wdl_graph_proposals,
    wdl_tool_packages,
)
from .request_ids import request_id, with_request_id


# Existing endpoint functions resolve these module globals at request time. Binding both
# modules here makes request tracing use one validated implementation everywhere.
views._request_id = request_id
views._with_request_id = with_request_id
wdl_assets._request_id = request_id
wdl_assets._with_request_id = with_request_id
# Keep the container liveness probe public while the rest of the API uses the
# runtime authentication permission.
views.health.view_class.permission_classes = [AllowAny]

for analysis_view in (
    rawdata_catalog.rawdata_catalog,
    analysis_runs.analysis_catalog,
    analysis_runs.analysis_runs,
    analysis_runs.analysis_run_detail,
    analysis_runs.analysis_run_output,
):
    analysis_view.view_class.analysis_operator_allowed = True


urlpatterns = [
    path("health", views.health),
    path("auth/csrf", auth_views.csrf_token),
    path("auth/login", auth_views.login_view),
    path("auth/me", auth_views.me),
    path("auth/logout", auth_views.logout_view),
    path("integration/openapi", integration_api.integration_openapi),
    path(
        "integration/workflow-versions",
        integration_api.integration_workflow_versions,
    ),
    path(
        "integration/workflow-versions/<int:version_id>",
        integration_api.integration_workflow_version_detail,
    ),
    path(
        "integration/analysis-runs/preflight",
        integration_api.integration_preflight,
    ),
    path(
        "integration/analysis-runs/by-external-ref",
        integration_api.integration_analysis_run_by_external_ref,
    ),
    path(
        "integration/analysis-runs",
        integration_api.integration_analysis_runs,
    ),
    path(
        "integration/analysis-runs/batch-status",
        integration_api.integration_analysis_runs_batch_status,
    ),
    path(
        "integration/analysis-runs/<uuid:run_id>",
        integration_api.integration_analysis_run_detail,
    ),
    path(
        "integration/analysis-runs/<uuid:run_id>/events",
        integration_api.integration_analysis_run_events,
    ),
    path(
        "integration/analysis-runs/<uuid:run_id>/cancel",
        integration_api.integration_analysis_run_cancel,
    ),
    path(
        "integration/analysis-runs/<uuid:run_id>/retry",
        integration_api.integration_analysis_run_retry,
    ),
    path(
        "integration/analysis-runs/<uuid:run_id>/outputs",
        integration_api.integration_analysis_run_outputs,
    ),
    path(
        "integration/analysis-runs/<uuid:run_id>/outputs/download",
        integration_api.integration_analysis_run_output_download,
    ),
    path(
        "integration/tool-test-runs/preflight",
        integration_api.integration_tool_test_preflight,
    ),
    path(
        "integration/tool-test-runs",
        integration_api.integration_tool_test_runs,
    ),
    path("integration/tools", integration_api.integration_tools),
    path("integration/software", integration_api.integration_software),
    path("analysis/catalog", analysis_runs.analysis_catalog),
    path("rawdata/catalog", rawdata_catalog.rawdata_catalog),
    path("resource-catalog", resource_catalog_api.resource_catalog),
    path("analysis-runs", analysis_runs.analysis_runs),
    path("analysis-runs/<uuid:run_id>", analysis_runs.analysis_run_detail),
    path(
        "analysis-runs/<uuid:run_id>/outputs",
        analysis_runs.analysis_run_output,
    ),
    path("tool-test-resources", tool_runs.tool_test_resources),
    path("tool-test-runs", tool_runs.tool_test_runs),
    path("tool-test-runs/<uuid:run_id>", tool_runs.tool_test_run_detail),
    path(
        "tool-test-runs/<uuid:run_id>/outputs",
        tool_runs.tool_test_run_output,
    ),
    path("software", software_library.software_assets),
    path("software/<slug:slug>", software_library.software_asset_detail),
    path(
        "software/<slug:slug>/releases",
        software_library.software_releases,
    ),
    path(
        "software/<slug:slug>/releases/<int:release_id>",
        software_library.software_release_detail,
    ),
    path(
        "software/<slug:slug>/tool-links",
        software_library.software_tool_links,
    ),
    path(
        "software/<slug:slug>/tool-links/<int:link_id>",
        software_library.software_tool_link_detail,
    ),
    path("contracts", views.contracts),
    path("contracts/<str:contract_name>", views.contracts),
    path("validations/tool-spec", views.validate_tool),
    path("validations/workflow-graph", views.validate_graph),
    path("compilations", views.compile_graph),
    path("tools", api_overrides.tools),
    path("tools/<str:tool_id>/drafts", views.tool_document),
    path("tools/<path:tool_id>/publish", views.publish_tool_document),
    path("tools/<path:tool_id>/versions", views.tool_versions),
    path(
        "tools/<path:tool_id>/versions/<str:version>",
        views.tool_version_detail,
    ),
    path("tools/<str:tool_id>", views.tool_document),
    path("wdl-packages", wdl_tool_packages.wdl_tool_packages),
    path("wdl-packages/preview", wdl_tool_packages.preview_wdl_tool_package),
    path("wdl-packages/tags", wdl_tool_packages.wdl_tool_package_tags),
    path(
        "wdl-packages/<slug:slug>/versions",
        wdl_tool_packages.wdl_tool_package_versions,
    ),
    path(
        "wdl-packages/<slug:slug>/versions/<str:version>",
        wdl_tool_packages.wdl_tool_package_version_detail,
    ),
    path(
        "wdl-packages/<slug:slug>/export",
        wdl_tool_packages.export_wdl_tool_package,
    ),
    path(
        "wdl-packages/<slug:slug>/tasks/extract",
        wdl_tool_packages.extract_wdl_tool_package_tasks,
    ),
    path("wdl-packages/<slug:slug>", wdl_tool_packages.wdl_tool_package_detail),
    path("wdl-assets", wdl_assets.wdl_assets),
    path("wdl-assets/tags", wdl_assets.wdl_tags),
    path("wdl-assets/tags/<int:tag_id>", wdl_assets.wdl_tag_detail),
    path("wdl-assets/<slug:slug>", wdl_assets.wdl_asset_detail),
    path(
        "wdl-assets/<slug:slug>/revisions",
        wdl_assets.wdl_asset_revisions,
    ),
    path(
        "wdl-assets/<slug:slug>/revisions/<int:version>",
        wdl_assets.wdl_asset_revision_detail,
    ),
    path(
        "wdl-assets/<slug:slug>/format",
        wdl_assets.format_wdl_asset,
    ),
    path("wdl-assets/<slug:slug>/export", wdl_assets.export_wdl_asset),
    path("wdl-assets/<slug:slug>/tasks/import", wdl_assets.import_wdl_task),
    path("editor/workflows", views.workflow_documents),
    path(
        "editor/workflows/<slug:slug>/tool-package-source",
        wdl_tool_packages.workflow_tool_package_source,
    ),
    path("editor/workflows/<slug:slug>", views.workflow_document),
    path("editor/workflows/<slug:slug>/versions", views.workflow_versions),
    path(
        "editor/workflows/<slug:slug>/versions/<int:version>",
        views.workflow_version_detail,
    ),
    path("editor/workflows/<slug:slug>/compilations", views.compilation_history),
    path("editor/workflows/<slug:slug>/wdl-versions", views.wdl_revisions),
    path(
        "editor/workflows/<slug:slug>/wdl-versions/<int:version>",
        views.wdl_revision_detail,
    ),
    path(
        "editor/workflows/<slug:slug>/wdl-versions/<int:version>/graph-proposals",
        wdl_graph_proposals.wdl_graph_proposals,
    ),
    path(
        "editor/workflows/<slug:slug>/wdl-graph-proposals/<int:proposal_id>/apply",
        wdl_graph_proposals.apply_wdl_graph_proposal,
    ),
    # Compatibility alias retained for clients built against the phase-2 preview.
    path("editor/workflows/<slug:slug>/wdl-revisions", views.wdl_revisions),
    path(
        "editor/workflows/<slug:slug>/wdl-revisions/<int:version>",
        views.wdl_revision_detail,
    ),
]
