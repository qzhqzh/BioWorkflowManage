from django.urls import path

from . import api_overrides, views, wdl_assets
from .request_ids import request_id, with_request_id


# Existing endpoint functions resolve these module globals at request time. Binding both
# modules here makes request tracing use one validated implementation everywhere.
views._request_id = request_id
views._with_request_id = with_request_id
wdl_assets._request_id = request_id
wdl_assets._with_request_id = with_request_id


urlpatterns = [
    path("health", views.health),
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
    path("editor/workflows", views.workflow_documents),
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
    # Compatibility alias retained for clients built against the phase-2 preview.
    path("editor/workflows/<slug:slug>/wdl-revisions", views.wdl_revisions),
    path(
        "editor/workflows/<slug:slug>/wdl-revisions/<int:version>",
        views.wdl_revision_detail,
    ),
]
