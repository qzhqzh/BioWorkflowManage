"""Optional data service transport. Never reads the data service's database."""

import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings
from rest_framework.exceptions import APIException


class DataServiceUnavailable(APIException):
    code = "DATA_SERVICE_UNAVAILABLE"
    category = "infrastructure"
    retryable = True
    status_code = 503
    default_detail = {
        "error": {
            "code": "DATA_SERVICE_UNAVAILABLE",
            "category": "infrastructure",
            "message": "数据管理服务暂时不可用，请检查连接及服务凭据。",
            "retryable": True,
        }
    }

    def __str__(self):
        return str(self.detail["error"]["message"])


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def enabled():
    return bool(settings.DATA_SERVICE_URL)


def request(method, path, payload=None):
    base = settings.DATA_SERVICE_URL.rstrip("/")
    parsed = urlsplit(base)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise DataServiceUnavailable()
    try:
        token_path = Path(settings.DATA_SERVICE_TOKEN_FILE)
        if not token_path.is_file() or token_path.stat().st_size > 8192:
            raise OSError("Invalid token file")
        token = token_path.read_text().strip()
        if not token or any(c.isspace() for c in token):
            raise OSError("Invalid token")
        body = json.dumps(payload).encode() if payload is not None else None
        req = Request(
            base + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        with build_opener(NoRedirect()).open(
            req, timeout=settings.DATA_SERVICE_TIMEOUT_SECONDS
        ) as response:
            content = response.read(32 * 1024 * 1024 + 1)
            if len(content) > 32 * 1024 * 1024:
                raise ValueError("Response too large")
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError("Invalid response")
            return result
    except HTTPError as error:
        # Only a validation failure of a snapshot may become an input failure.
        # Auth, redirects and outages always fail closed as infrastructure errors.
        if error.code == 400:
            try:
                content = error.read(65537)
                body = json.loads(content) if len(content) <= 65536 else {}
                code = (
                    body.get("error", {}).get("code")
                    if isinstance(body, dict)
                    else None
                )
            except (ValueError, TypeError, AttributeError, UnicodeError, OSError):
                code = None
            if code == "RAWDATA_NOT_READY" and path == "/api/v1/verify":
                raise ValueError(
                    "RAWDATA_NOT_READY: 数据或完成标记已变化，请重新核验。"
                ) from None
            if code == "RAWDATA_REQUEST_INVALID" and path == "/api/v1/readiness":
                raise ValueError(
                    "RAWDATA_REQUEST_INVALID: 数据就绪请求无效。"
                ) from None
        raise DataServiceUnavailable() from None
    except (OSError, URLError, ValueError, UnicodeError):
        raise DataServiceUnavailable() from None


def datasets(*, include_unready):
    result = request(
        "GET", "/api/v1/datasets" + ("?include_unready=true" if include_unready else "")
    )
    if (
        not isinstance(result.get("results"), list)
        or type(result.get("count")) is not int
        or type(result.get("truncated")) is not bool
    ):
        raise DataServiceUnavailable()
    # Preserve the closed legacy Integration API representation.
    for item in result["results"]:
        if not isinstance(item, dict):
            raise DataServiceUnavailable()
        item.pop("version", None)
    return result


def verify_snapshot(snapshot, manifest, *, require_fresh_index=True):
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        raise ValueError("RAWDATA_SNAPSHOT_INVALID: 必须提供有效的数据就绪快照。")
    result = request(
        "POST",
        "/api/v1/verify",
        {
            "snapshot": snapshot,
            "manifest": manifest,
            "require_fresh_index": require_fresh_index,
        },
    )
    if not isinstance(snapshot, dict) or result.get("snapshot") != snapshot:
        raise DataServiceUnavailable()
    return result["snapshot"]


def catalog():
    from django.db.models import Count, F, Window
    from django.db.models.functions import RowNumber
    from .models import AnalysisRun

    result = request("GET", "/api/v1/catalog")
    if not isinstance(result.get("datasets"), list):
        raise DataServiceUnavailable()
    by_id = {item["id"]: item for item in result["datasets"]}
    for item in by_id.values():
        item.pop("version", None)
        item.pop("identity_digest", None)
        item.update(run_count=0, recent_runs=[])
    # Analysis history is owned by BWM; dataset identifiers are plain references.
    # Existing runs already freeze these identifiers in request_payload.
    for key in ("dataset", "control_dataset"):
        field = "request_payload__" + key
        runs = AnalysisRun.objects.filter(**{field + "__in": list(by_id)})
        for row in runs.order_by().values(field).annotate(total=Count("id")):
            by_id[row[field]]["run_count"] += row["total"]
        recent = (
            runs.annotate(
                position=Window(
                    expression=RowNumber(),
                    partition_by=[F(field)],
                    order_by=[F("created_at").desc(), F("id").desc()],
                )
            )
            .filter(position__lte=3)
            .values(field, "id", "status", "created_at")
        )
        for row in recent:
            by_id[row[field]]["recent_runs"].append(
                {
                    "id": str(row["id"]),
                    "status": row["status"],
                    "created_at": row["created_at"].isoformat(),
                }
            )
    for item in by_id.values():
        item["recent_runs"] = sorted(
            item["recent_runs"],
            key=lambda row: (row["created_at"], row["id"]),
            reverse=True,
        )[:3]
    return result
