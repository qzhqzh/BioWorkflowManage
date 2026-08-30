from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import http.client
import ipaddress
import json
import math
import signal
import socket
import ssl
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Min, Q
from django.utils import timezone

from .models import (
    AnalysisRun,
    IntegrationOutboxEvent,
    WebhookDelivery,
    WebhookDeliveryAttempt,
    WebhookEndpoint,
)


TERMINAL_EVENT_TYPE = "analysis.run.terminal"
TERMINAL_STATUSES = {
    AnalysisRun.Status.SUCCEEDED,
    AnalysisRun.Status.FAILED,
    AnalysisRun.Status.CANCELED,
}


class WebhookError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WebhookTarget:
    scheme: str
    hostname: str
    port: int
    request_target: str
    address: str


@dataclass(frozen=True)
class WebhookHTTPResult:
    status_code: int
    response_excerpt: str


def webhook_delivery_deadline_supported() -> bool:
    return hasattr(signal, "setitimer") and threading.current_thread() is (
        threading.main_thread()
    )


@contextmanager
def _webhook_wall_clock_timeout(seconds: float):
    if not webhook_delivery_deadline_supported():
        raise WebhookError(
            "WEBHOOK_DELIVERY_DEADLINE_UNSUPPORTED",
            "Webhook dispatcher 必须在支持 POSIX wall-clock timer 的主线程运行。",
        )
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    if previous_timer[0] > 0 or previous_timer[1] > 0:
        raise WebhookError(
            "WEBHOOK_DELIVERY_TIMER_CONFLICT",
            "Webhook dispatcher 检测到已有 wall-clock timer，拒绝覆盖。",
        )
    previous_handler = signal.getsignal(signal.SIGALRM)

    def deadline_exceeded(_signum, _frame):
        raise WebhookError(
            "WEBHOOK_DELIVERY_TIMEOUT",
            "Webhook DNS 或网络请求超过总时限。",
        )

    signal.signal(signal.SIGALRM, deadline_exceeded)
    signal.setitimer(signal.ITIMER_REAL, max(0.001, float(seconds)))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def _remaining_delivery_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise WebhookError(
            "WEBHOOK_DELIVERY_TIMEOUT",
            "Webhook DNS 或网络请求超过总时限。",
        )
    return remaining


def _setting_hosts(name: str) -> set[str]:
    return {
        str(value).strip().casefold().rstrip(".")
        for value in getattr(settings, name, [])
        if str(value).strip()
    }


def _derive_webhook_secret(
    *,
    endpoint_id: uuid.UUID,
    secret_version: int,
    secret_salt: uuid.UUID,
) -> bytes:
    master_key = str(settings.WEBHOOK_SIGNING_KEY or "").encode("utf-8")
    if len(master_key) < 32:
        raise WebhookError(
            "WEBHOOK_SIGNING_KEY_INVALID",
            "WEBHOOK_SIGNING_KEY 至少需要 32 个 UTF-8 字节。",
        )
    context = (
        f"bioworkflow-webhook-v1:{endpoint_id}:"
        f"{secret_version}:{secret_salt}"
    ).encode("ascii")
    return hmac.new(master_key, context, hashlib.sha256).digest()


def derive_webhook_secret(endpoint: WebhookEndpoint) -> bytes:
    return _derive_webhook_secret(
        endpoint_id=endpoint.id,
        secret_version=endpoint.secret_version,
        secret_salt=endpoint.secret_salt,
    )


def derive_delivery_secret(delivery: WebhookDelivery) -> bytes:
    return _derive_webhook_secret(
        endpoint_id=delivery.endpoint_id,
        secret_version=delivery.secret_version,
        secret_salt=delivery.secret_salt,
    )


def webhook_secret_token(endpoint: WebhookEndpoint) -> str:
    return base64.urlsafe_b64encode(derive_webhook_secret(endpoint)).rstrip(b"=").decode(
        "ascii"
    )


def canonical_webhook_body(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def webhook_signature(
    delivery: WebhookDelivery,
    *,
    delivery_id: uuid.UUID,
    event_id: uuid.UUID,
    timestamp: int,
    body: bytes,
) -> str:
    signed = b".".join(
        [
            str(delivery_id).encode("ascii"),
            str(event_id).encode("ascii"),
            str(timestamp).encode("ascii"),
            body,
        ]
    )
    digest = hmac.new(derive_delivery_secret(delivery), signed, hashlib.sha256).hexdigest()
    return f"v1={digest}"


def webhook_headers(
    delivery: WebhookDelivery,
    *,
    body: bytes,
    timestamp: int,
) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "User-Agent": "BioWorkflowManage-Webhook/1.0",
        "X-BioWorkflow-Delivery-ID": str(delivery.id),
        "X-BioWorkflow-Event-ID": str(delivery.event_id),
        "X-BioWorkflow-Timestamp": str(timestamp),
        "X-BioWorkflow-Secret-Version": str(delivery.secret_version),
        "X-BioWorkflow-Signature": webhook_signature(
            delivery,
            delivery_id=delivery.id,
            event_id=delivery.event_id,
            timestamp=timestamp,
            body=body,
        ),
    }


def verify_webhook_signature(
    secret_token: str,
    headers: dict[str, str],
    body: bytes,
    *,
    now_timestamp: int | None = None,
    tolerance_seconds: int = 300,
) -> dict[str, Any]:
    normalized = {str(key).casefold(): str(value) for key, value in headers.items()}
    try:
        delivery_id = uuid.UUID(normalized["x-bioworkflow-delivery-id"])
        event_id = uuid.UUID(normalized["x-bioworkflow-event-id"])
        timestamp = int(normalized["x-bioworkflow-timestamp"])
        provided = normalized["x-bioworkflow-signature"]
        padding = "=" * (-len(secret_token) % 4)
        secret = base64.urlsafe_b64decode(secret_token + padding)
    except (binascii.Error, KeyError, ValueError, TypeError) as error:
        raise WebhookError(
            "WEBHOOK_SIGNATURE_INVALID",
            "Webhook 签名 header 或密钥格式无效。",
        ) from error
    if len(secret) != hashlib.sha256().digest_size:
        raise WebhookError(
            "WEBHOOK_SIGNATURE_INVALID",
            "Webhook 签名密钥长度无效。",
        )
    signed = b".".join(
        [
            str(delivery_id).encode("ascii"),
            str(event_id).encode("ascii"),
            str(timestamp).encode("ascii"),
            body,
        ]
    )
    expected = "v1=" + hmac.new(secret, signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(provided, expected):
        raise WebhookError(
            "WEBHOOK_SIGNATURE_INVALID",
            "Webhook HMAC-SHA256 签名不匹配。",
        )
    now_timestamp = (
        int(timezone.now().timestamp()) if now_timestamp is None else int(now_timestamp)
    )
    if abs(now_timestamp - timestamp) > max(0, int(tolerance_seconds)):
        raise WebhookError(
            "WEBHOOK_TIMESTAMP_OUT_OF_RANGE",
            "Webhook timestamp 超出允许窗口。",
        )
    return {
        "delivery_id": delivery_id,
        "event_id": event_id,
        "timestamp": timestamp,
    }


def resolve_webhook_target(url: str) -> WebhookTarget:
    try:
        parsed = urlsplit(str(url or "").strip())
        port = parsed.port
    except ValueError as error:
        raise WebhookError("WEBHOOK_URL_INVALID", "Webhook URL 端口无效。") from error
    scheme = parsed.scheme.casefold()
    hostname = str(parsed.hostname or "").casefold().rstrip(".")
    if scheme not in {"https", "http"} or not hostname:
        raise WebhookError(
            "WEBHOOK_URL_INVALID",
            "Webhook URL 必须包含 http(s) scheme 与 hostname。",
        )
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise WebhookError(
            "WEBHOOK_URL_INVALID",
            "Webhook URL 不允许 userinfo 或 fragment。",
        )
    if any(not 33 <= ord(character) <= 126 for character in hostname):
        raise WebhookError(
            "WEBHOOK_URL_INVALID",
            "Webhook hostname 必须使用 ASCII/IDNA 格式。",
        )
    path = parsed.path or "/"
    request_target = path + (f"?{parsed.query}" if parsed.query else "")
    if any(not 33 <= ord(character) <= 126 for character in request_target):
        raise WebhookError(
            "WEBHOOK_URL_INVALID",
            "Webhook path/query 必须先做 URI percent-encoding。",
        )
    if scheme == "http" and hostname not in _setting_hosts(
        "WEBHOOK_ALLOWED_HTTP_HOSTS"
    ):
        raise WebhookError(
            "WEBHOOK_HTTPS_REQUIRED",
            "Webhook 默认只允许 HTTPS；HTTP hostname 未在部署白名单中。",
        )
    port = port if port is not None else (443 if scheme == "https" else 80)
    if port < 1:
        raise WebhookError("WEBHOOK_URL_INVALID", "Webhook URL 端口无效。")
    try:
        records = socket.getaddrinfo(
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError as error:
        raise WebhookError(
            "WEBHOOK_TARGET_DNS_FAILED",
            "Webhook hostname 无法解析。",
        ) from error
    addresses = list(
        dict.fromkeys(str(record[4][0]) for record in records if record[4])
    )
    if not addresses:
        raise WebhookError(
            "WEBHOOK_TARGET_DNS_FAILED",
            "Webhook hostname 没有可用地址。",
        )
    private_allowlist = _setting_hosts("WEBHOOK_PRIVATE_HOST_ALLOWLIST")
    for address in addresses:
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as error:
            raise WebhookError(
                "WEBHOOK_TARGET_ADDRESS_INVALID",
                "Webhook hostname 解析出无效地址。",
            ) from error
        if (
            (
                not parsed_address.is_global
                or parsed_address.is_multicast
                or (
                    isinstance(parsed_address, ipaddress.IPv6Address)
                    and parsed_address.is_site_local
                )
            )
            and hostname not in private_allowlist
            and address.casefold() not in private_allowlist
        ):
            raise WebhookError(
                "WEBHOOK_TARGET_PRIVATE_ADDRESS",
                "Webhook 目标解析到私网、环回、site-local、组播或保留地址。",
            )
    return WebhookTarget(
        scheme=scheme,
        hostname=hostname,
        port=port,
        request_target=request_target,
        address=addresses[0],
    )


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, target: WebhookTarget, *, timeout: float):
        super().__init__(target.hostname, target.port, timeout=timeout)
        self._resolved_address = target.address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._resolved_address, self.port),
            self.timeout,
            self.source_address,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, target: WebhookTarget, *, timeout: float):
        super().__init__(
            target.hostname,
            target.port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._resolved_address = target.address

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._resolved_address, self.port),
            self.timeout,
            self.source_address,
        )
        try:
            self.sock = self._context.wrap_socket(
                raw_socket,
                server_hostname=self.host,
            )
        except Exception:
            raw_socket.close()
            raise


def _send_webhook_request(
    target: WebhookTarget,
    *,
    body: bytes,
    headers: dict[str, str],
    timeout_seconds: float | None = None,
) -> WebhookHTTPResult:
    timeout = max(
        0.001,
        float(
            settings.WEBHOOK_DELIVERY_TIMEOUT_SECONDS
            if timeout_seconds is None
            else timeout_seconds
        ),
    )
    connection: http.client.HTTPConnection
    if target.scheme == "https":
        connection = _PinnedHTTPSConnection(target, timeout=timeout)
    else:
        connection = _PinnedHTTPConnection(target, timeout=timeout)
    try:
        connection.request("POST", target.request_target, body=body, headers=headers)
        response = connection.getresponse()
        limit = max(0, int(settings.WEBHOOK_RESPONSE_MAX_BYTES))
        response_body = response.read(limit + 1) if limit else b""
        excerpt = response_body[:limit].decode("utf-8", errors="replace")
        return WebhookHTTPResult(
            status_code=int(response.status),
            response_excerpt=excerpt,
        )
    finally:
        connection.close()


def _terminal_event_payload(
    run: AnalysisRun,
    *,
    event_id: uuid.UUID,
    occurred_at,
) -> dict[str, Any]:
    request_payload = run.request_payload if isinstance(run.request_payload, dict) else {}
    external_ref = request_payload.get("external_ref")
    if not isinstance(external_ref, dict):
        external_ref = {
            "external_run_id": run.external_run_id or None,
            "external_analysis_id": run.external_analysis_id or None,
        }
    analysis_product = request_payload.get("analysis_product")
    if isinstance(analysis_product, dict):
        analysis_product = {
            key: analysis_product.get(key)
            for key in ("analysis_code", "contract_version", "contract_digest")
            if analysis_product.get(key) is not None
        }
    else:
        analysis_product = None
    error = None
    if run.error_code or run.status == AnalysisRun.Status.FAILED:
        error = {
            "code": run.error_code or "ANALYSIS_FAILED",
            "category": run.error_category or "application",
            "retryable": bool(run.error_retryable),
        }
    return {
        "schema_version": "1.0.0",
        "event_id": str(event_id),
        "event_type": TERMINAL_EVENT_TYPE,
        "occurred_at": occurred_at.isoformat(),
        "data": {
            "run_id": str(run.id),
            "run_kind": run.run_kind,
            "external_ref": external_ref,
            "analysis_product": analysis_product,
            "status": run.status,
            "status_version": int(run.status_version),
            "output_status": run.output_status,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "error": error,
            "links": {
                "run": f"/api/v1/integration/analysis-runs/{run.id}",
                "outputs": f"/api/v1/integration/analysis-runs/{run.id}/outputs",
            },
        },
    }


def enqueue_terminal_event(run: AnalysisRun) -> IntegrationOutboxEvent | None:
    if run.status not in TERMINAL_STATUSES or run.service_account_id is None:
        return None
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("terminal Outbox event must be created inside a transaction")
    occurred_at = timezone.now()
    event_id = uuid.uuid4()
    event, created = IntegrationOutboxEvent.objects.get_or_create(
        run=run,
        event_type=TERMINAL_EVENT_TYPE,
        status_version=run.status_version,
        defaults={
            "id": event_id,
            "service_account_id": run.service_account_id,
            "occurred_at": occurred_at,
            "payload": _terminal_event_payload(
                run,
                event_id=event_id,
                occurred_at=occurred_at,
            ),
        },
    )
    if not created:
        return event
    endpoints = list(
        WebhookEndpoint.objects.filter(
            service_account_id=run.service_account_id,
            is_active=True,
        )
    )
    deliveries = [
        WebhookDelivery(
            event=event,
            endpoint=endpoint,
            target_url=endpoint.url,
            secret_salt=endpoint.secret_salt,
            secret_version=endpoint.secret_version,
        )
        for endpoint in endpoints
        if isinstance(endpoint.event_types, list)
        and TERMINAL_EVENT_TYPE in endpoint.event_types
    ]
    if deliveries:
        WebhookDelivery.objects.bulk_create(deliveries)
    return event


def _retry_delay(attempt_count: int) -> timedelta:
    base = max(1.0, float(settings.WEBHOOK_BACKOFF_BASE_SECONDS))
    maximum = max(base, float(settings.WEBHOOK_BACKOFF_MAX_SECONDS))
    seconds = min(maximum, base * (2 ** max(0, attempt_count - 1)))
    return timedelta(seconds=seconds)


def _recover_expired_deliveries(now) -> None:
    expired = list(
        WebhookDelivery.objects.select_for_update(skip_locked=True, of=("self",))
        .filter(
            state=WebhookDelivery.State.DELIVERING,
            lease_expires_at__lt=now,
        )
        .order_by("lease_expires_at")[:50]
    )
    maximum = max(1, int(settings.WEBHOOK_MAX_ATTEMPTS))
    for delivery in expired:
        attempt = delivery.attempts.filter(finished_at__isnull=True).order_by(
            "-attempt_number"
        ).first()
        if attempt is not None:
            attempt.outcome = WebhookDeliveryAttempt.Outcome.LEASE_EXPIRED
            attempt.error_code = "WEBHOOK_DELIVERY_LEASE_EXPIRED"
            attempt.error = "Webhook dispatcher 租约过期；可能发生至少一次重复投递。"
            attempt.finished_at = now
            attempt.save(
                update_fields=[
                    "outcome",
                    "error_code",
                    "error",
                    "finished_at",
                ]
            )
        delivery.last_error_code = "WEBHOOK_DELIVERY_LEASE_EXPIRED"
        delivery.last_error = "Webhook dispatcher 租约过期。"
        delivery.lease_token = None
        delivery.lease_expires_at = None
        if delivery.attempt_count >= maximum:
            delivery.state = WebhookDelivery.State.DEAD_LETTER
        else:
            delivery.state = WebhookDelivery.State.PENDING
            delivery.next_attempt_at = now
        delivery.save()


def _dead_letter_inactive_deliveries(now) -> None:
    inactive = list(
        WebhookDelivery.objects.select_for_update(skip_locked=True, of=("self",))
        .select_related("endpoint", "endpoint__service_account")
        .filter(state=WebhookDelivery.State.PENDING, next_attempt_at__lte=now)
        .filter(Q(endpoint__is_active=False) | Q(endpoint__service_account__is_active=False))
        .order_by("next_attempt_at")[:50]
    )
    for delivery in inactive:
        delivery.state = WebhookDelivery.State.DEAD_LETTER
        delivery.last_error_code = "WEBHOOK_ENDPOINT_INACTIVE"
        delivery.last_error = "Webhook endpoint 或 Service Account 已停用。"
        delivery.save(
            update_fields=[
                "state",
                "last_error_code",
                "last_error",
                "updated_at",
            ]
        )


def claim_next_delivery() -> WebhookDelivery | None:
    with transaction.atomic():
        now = timezone.now()
        _recover_expired_deliveries(now)
        _dead_letter_inactive_deliveries(now)
        delivery = (
            WebhookDelivery.objects.select_for_update(
                skip_locked=True,
                of=("self",),
            )
            .select_related(
                "event",
                "endpoint",
                "endpoint__service_account",
            )
            .filter(
                state=WebhookDelivery.State.PENDING,
                next_attempt_at__lte=now,
                endpoint__is_active=True,
                endpoint__service_account__is_active=True,
            )
            .order_by("next_attempt_at", "created_at")
            .first()
        )
        if delivery is None:
            return None
        delivery.state = WebhookDelivery.State.DELIVERING
        delivery.attempt_count += 1
        delivery.lease_token = uuid.uuid4()
        delivery.lease_expires_at = now + timedelta(
            seconds=max(
                5,
                int(settings.WEBHOOK_DELIVERY_LEASE_SECONDS),
                math.ceil(float(settings.WEBHOOK_DELIVERY_TIMEOUT_SECONDS)) + 5,
            )
        )
        delivery.save(
            update_fields=[
                "state",
                "attempt_count",
                "lease_token",
                "lease_expires_at",
                "updated_at",
            ]
        )
        WebhookDeliveryAttempt.objects.create(
            delivery=delivery,
            attempt_number=delivery.attempt_count,
            replay_number=delivery.replay_count,
        )
        return delivery


def _record_attempt_request(
    delivery: WebhookDelivery,
    *,
    timestamp: int,
    resolved_address: str,
) -> bool:
    with transaction.atomic():
        current = (
            WebhookDelivery.objects.select_for_update(of=("self",))
            .filter(
                pk=delivery.pk,
                state=WebhookDelivery.State.DELIVERING,
                lease_token=delivery.lease_token,
            )
            .first()
        )
        if current is None:
            return False
        attempt = WebhookDeliveryAttempt.objects.select_for_update().get(
            delivery=current,
            attempt_number=current.attempt_count,
        )
        attempt.request_timestamp = timestamp
        attempt.resolved_address = resolved_address
        attempt.save(update_fields=["request_timestamp", "resolved_address"])
        return True


def _finish_delivery(
    delivery: WebhookDelivery,
    *,
    timestamp: int | None,
    resolved_address: str | None,
    status_code: int | None,
    response_excerpt: str,
    error_code: str,
    error: str,
) -> bool:
    with transaction.atomic():
        current = (
            WebhookDelivery.objects.select_for_update(of=("self",))
            .filter(
                pk=delivery.pk,
                state=WebhookDelivery.State.DELIVERING,
                lease_token=delivery.lease_token,
            )
            .first()
        )
        if current is None:
            return False
        now = timezone.now()
        succeeded = status_code is not None and 200 <= status_code < 300
        maximum = max(1, int(settings.WEBHOOK_MAX_ATTEMPTS))
        if succeeded:
            current.state = WebhookDelivery.State.DELIVERED
            current.delivered_at = now
            outcome = WebhookDeliveryAttempt.Outcome.DELIVERED
        elif current.attempt_count >= maximum:
            current.state = WebhookDelivery.State.DEAD_LETTER
            outcome = WebhookDeliveryAttempt.Outcome.DEAD_LETTER
        else:
            current.state = WebhookDelivery.State.PENDING
            current.next_attempt_at = now + _retry_delay(current.attempt_count)
            outcome = WebhookDeliveryAttempt.Outcome.RETRY
        current.last_status_code = status_code
        current.last_error_code = error_code
        current.last_error = str(error or "")[:2000]
        current.last_response_excerpt = str(response_excerpt or "")[
            : int(settings.WEBHOOK_RESPONSE_MAX_BYTES)
        ]
        current.lease_token = None
        current.lease_expires_at = None
        current.save()

        attempt = WebhookDeliveryAttempt.objects.select_for_update().get(
            delivery=current,
            attempt_number=current.attempt_count,
        )
        attempt.outcome = outcome
        attempt.request_timestamp = timestamp
        attempt.resolved_address = resolved_address
        attempt.status_code = status_code
        attempt.error_code = error_code
        attempt.error = str(error or "")[:2000]
        attempt.response_excerpt = str(response_excerpt or "")[
            : int(settings.WEBHOOK_RESPONSE_MAX_BYTES)
        ]
        attempt.finished_at = now
        attempt.save()
        return True


def deliver_webhook(delivery: WebhookDelivery) -> bool:
    body = canonical_webhook_body(delivery.event.payload)
    timestamp = None
    target = None
    result = None
    error_code = ""
    error = ""
    timeout = max(0.001, float(settings.WEBHOOK_DELIVERY_TIMEOUT_SECONDS))
    deadline = time.monotonic() + timeout
    try:
        with _webhook_wall_clock_timeout(
            _remaining_delivery_seconds(deadline)
        ):
            target = resolve_webhook_target(delivery.target_url)
        timestamp = int(timezone.now().timestamp())
        headers = webhook_headers(delivery, body=body, timestamp=timestamp)
        if not _record_attempt_request(
            delivery,
            timestamp=timestamp,
            resolved_address=target.address,
        ):
            return False
        remaining = _remaining_delivery_seconds(deadline)
        with _webhook_wall_clock_timeout(remaining):
            result = _send_webhook_request(
                target,
                body=body,
                headers=headers,
                timeout_seconds=remaining,
            )
        if not 200 <= result.status_code < 300:
            error_code = "WEBHOOK_HTTP_STATUS"
            error = f"Webhook endpoint 返回 HTTP {result.status_code}。"
    except WebhookError as caught:
        error_code = caught.code
        error = str(caught)
    except (
        OSError,
        TimeoutError,
        ValueError,
        ssl.SSLError,
        http.client.HTTPException,
    ) as caught:
        error_code = "WEBHOOK_NETWORK_ERROR"
        error = str(caught) or type(caught).__name__
    return _finish_delivery(
        delivery,
        timestamp=timestamp,
        resolved_address=target.address if target is not None else None,
        status_code=result.status_code if result is not None else None,
        response_excerpt=result.response_excerpt if result is not None else "",
        error_code=error_code,
        error=error,
    )


def replay_webhook_delivery(
    delivery_id: uuid.UUID | str,
    *,
    actor: str,
) -> WebhookDelivery:
    try:
        normalized_delivery_id = uuid.UUID(str(delivery_id))
    except (TypeError, ValueError) as error:
        raise WebhookError(
            "WEBHOOK_DELIVERY_ID_INVALID",
            "Webhook delivery ID 无效。",
        ) from error
    with transaction.atomic():
        delivery = (
            WebhookDelivery.objects.select_for_update(of=("self",))
            .select_related("endpoint", "endpoint__service_account")
            .filter(pk=normalized_delivery_id)
            .first()
        )
        if delivery is None:
            raise WebhookError(
                "WEBHOOK_DELIVERY_NOT_FOUND",
                "Webhook delivery 不存在。",
            )
        if delivery.state == WebhookDelivery.State.DELIVERING:
            raise WebhookError(
                "WEBHOOK_DELIVERY_IN_PROGRESS",
                "Webhook delivery 正在投递，不能重放。",
            )
        if not delivery.endpoint.is_active or not delivery.endpoint.service_account.is_active:
            raise WebhookError(
                "WEBHOOK_ENDPOINT_INACTIVE",
                "Webhook endpoint 或 Service Account 已停用。",
            )
        now = timezone.now()
        delivery.state = WebhookDelivery.State.PENDING
        delivery.replay_count += 1
        delivery.next_attempt_at = now
        delivery.lease_token = None
        delivery.lease_expires_at = None
        delivery.delivered_at = None
        delivery.last_replayed_at = now
        delivery.last_replayed_by = str(actor or "deployment")[:256]
        delivery.save()
        return delivery


def webhook_delivery_metrics() -> dict[str, Any]:
    counts = {
        item["state"]: item["count"]
        for item in WebhookDelivery.objects.values("state").annotate(count=Count("id"))
    }
    now = timezone.now()
    pending = WebhookDelivery.objects.filter(state=WebhookDelivery.State.PENDING)
    oldest = pending.aggregate(value=Min("created_at"))["value"]
    return {
        "states": {
            state: int(counts.get(state, 0)) for state, _ in WebhookDelivery.State.choices
        },
        "due": pending.filter(next_attempt_at__lte=now).count(),
        "oldest_pending_seconds": (
            max(0, int((now - oldest).total_seconds())) if oldest is not None else None
        ),
    }
