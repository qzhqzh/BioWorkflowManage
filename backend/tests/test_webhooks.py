from __future__ import annotations

import base64
import hashlib
import hmac
import json
import socket
import uuid
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.utils import timezone
from rest_framework.test import APIClient
from jsonschema import Draft202012Validator

from workflows.analysis_runtime import (
    _finalize_cancelled_run,
    _recover_stale_runs,
    _update_run,
)
from workflows.integration_tokens import issue_service_token
from workflows.models import (
    AnalysisRun,
    IntegrationOutboxEvent,
    ServiceAccount,
    ToolVersion,
    WebhookDelivery,
    WebhookDeliveryAttempt,
    WebhookEndpoint,
)
from workflows.webhooks import (
    TERMINAL_EVENT_TYPE,
    WebhookError,
    WebhookHTTPResult,
    WebhookTarget,
    canonical_webhook_body,
    claim_next_delivery,
    deliver_webhook,
    derive_delivery_secret,
    enqueue_terminal_event,
    resolve_webhook_target,
    verify_webhook_signature,
    webhook_delivery_metrics,
)


pytestmark = pytest.mark.usefixtures("auth_disabled")


def _settings(settings):
    settings.WEBHOOK_SIGNING_KEY = "pytest-webhook-signing-key-" + "x" * 40
    settings.WEBHOOK_DELIVERY_TIMEOUT_SECONDS = 1
    settings.WEBHOOK_DELIVERY_LEASE_SECONDS = 30
    settings.WEBHOOK_MAX_ATTEMPTS = 3
    settings.WEBHOOK_BACKOFF_BASE_SECONDS = 10
    settings.WEBHOOK_BACKOFF_MAX_SECONDS = 100
    settings.WEBHOOK_RESPONSE_MAX_BYTES = 256
    settings.WEBHOOK_ALLOWED_HTTP_HOSTS = []
    settings.WEBHOOK_PRIVATE_HOST_ALLOWLIST = []


def _account(*, scopes=None) -> ServiceAccount:
    return ServiceAccount.objects.create(
        client_id="mes",
        name="MES",
        scopes=scopes or ["analysis:cancel"],
        created_by="pytest",
    )


def _endpoint(account: ServiceAccount) -> WebhookEndpoint:
    return WebhookEndpoint.objects.create(
        service_account=account,
        name="terminal",
        url="https://hooks.example.test/analysis-events",
        created_by="pytest",
    )


def _run(
    account: ServiceAccount,
    *,
    status=AnalysisRun.Status.PREPARING,
) -> AnalysisRun:
    tool = ToolVersion.objects.create(
        tool_id=f"echo-{uuid.uuid4().hex[:8]}",
        version="1.0.0",
        name="Echo",
        digest="sha256:" + "1" * 64,
        tool_spec={"runtime": {"memory_gb": 1}},
    )
    return AnalysisRun.objects.create(
        run_kind=AnalysisRun.Kind.TOOL_TEST,
        tool_version=tool,
        service_account=account,
        external_run_id=f"run-{uuid.uuid4().hex[:8]}",
        idempotency_key=f"idempotency-{uuid.uuid4().hex[:8]}",
        workflow_name="tool_test.echo",
        sample_id="S001",
        actor="service:mes",
        status=status,
        request_payload={
            "external_ref": {
                "client_id": account.client_id,
                "external_run_id": "mes-run-1",
                "external_analysis_id": "analysis-1",
            }
        },
    )


def _terminal_event(
    run: AnalysisRun,
    *,
    status=AnalysisRun.Status.SUCCEEDED,
) -> IntegrationOutboxEvent:
    with transaction.atomic():
        run.status = status
        run.status_version += 1
        run.progress = 100
        run.finished_at = timezone.now()
        run.output_status = (
            AnalysisRun.OutputStatus.COMPLETE
            if status == AnalysisRun.Status.SUCCEEDED
            else AnalysisRun.OutputStatus.UNAVAILABLE
        )
        run.save()
        event = enqueue_terminal_event(run)
    assert event is not None
    return event


@pytest.mark.django_db
def test_terminal_status_and_outbox_roll_back_together(settings):
    _settings(settings)
    account = _account()
    _endpoint(account)
    run = _run(account)

    with patch(
        "workflows.webhooks.WebhookDelivery.objects.bulk_create",
        side_effect=RuntimeError("database write failed"),
    ):
        with pytest.raises(RuntimeError, match="database write failed"):
            _update_run(
                run,
                status=AnalysisRun.Status.FAILED,
                progress=100,
                finished_at=timezone.now(),
                output_status=AnalysisRun.OutputStatus.UNAVAILABLE,
                error_code="ANALYSIS_FAILED",
            )

    persisted = AnalysisRun.objects.get(pk=run.pk)
    assert persisted.status == AnalysisRun.Status.PREPARING
    assert persisted.status_version == 1
    assert run.status == AnalysisRun.Status.PREPARING
    assert run.status_version == 1
    assert run.lease_token == persisted.lease_token
    assert not IntegrationOutboxEvent.objects.exists()
    assert not WebhookDelivery.objects.exists()


@pytest.mark.django_db
def test_terminal_worker_update_never_calls_external_network(settings):
    _settings(settings)
    account = _account()
    _endpoint(account)
    run = _run(account)

    with patch("workflows.webhooks._send_webhook_request") as send:
        _update_run(
            run,
            status=AnalysisRun.Status.SUCCEEDED,
            progress=100,
            finished_at=timezone.now(),
            output_status=AnalysisRun.OutputStatus.COMPLETE,
        )

    send.assert_not_called()
    run.refresh_from_db()
    assert run.status == AnalysisRun.Status.SUCCEEDED
    assert run.outbox_events.count() == 1
    assert WebhookDelivery.objects.count() == 1


@pytest.mark.django_db
def test_outbox_event_is_deduplicated_immutable_and_excludes_large_outputs(settings):
    _settings(settings)
    account = _account()
    endpoint = _endpoint(account)
    run = _run(account)
    run.outputs = {"large": "x" * 10000}

    event = _terminal_event(run)
    with transaction.atomic():
        duplicate = enqueue_terminal_event(run)

    assert duplicate == event
    assert IntegrationOutboxEvent.objects.count() == 1
    assert WebhookDelivery.objects.filter(event=event, endpoint=endpoint).count() == 1
    assert event.payload["event_id"] == str(event.id)
    assert event.payload["event_type"] == TERMINAL_EVENT_TYPE
    assert event.payload["data"]["status_version"] == run.status_version
    assert "outputs" not in event.payload["data"]
    assert len(canonical_webhook_body(event.payload)) < 4096
    openapi = json.loads(
        (Path(__file__).parents[2] / "schemas" / "integration-openapi-v1.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(
        {
            **openapi,
            "$ref": "#/components/schemas/AnalysisRunTerminalEvent",
        }
    ).validate(event.payload)
    with pytest.raises(ValidationError, match="cannot be updated"):
        IntegrationOutboxEvent.objects.filter(pk=event.pk).update(payload={})
    with pytest.raises(ValidationError, match="cannot be deleted"):
        IntegrationOutboxEvent.objects.filter(pk=event.pk).delete()


@pytest.mark.django_db
def test_delivery_uses_stable_ids_hmac_and_pinned_address(settings):
    _settings(settings)
    account = _account()
    endpoint = _endpoint(account)
    event = _terminal_event(_run(account))
    delivery_snapshot = WebhookDelivery.objects.get()
    original_secret = derive_delivery_secret(delivery_snapshot)
    original_url = endpoint.url
    endpoint.url = "https://new.example.test/replacement"
    endpoint.secret_salt = uuid.uuid4()
    endpoint.secret_version = 2
    endpoint.save()
    delivery = claim_next_delivery()
    assert delivery is not None
    target = WebhookTarget(
        scheme="https",
        hostname="hooks.example.test",
        port=443,
        request_target="/analysis-events",
        address="93.184.216.34",
    )
    captured = {}

    def send(request_target, *, body, headers):
        captured.update(target=request_target, body=body, headers=headers)
        return WebhookHTTPResult(status_code=204, response_excerpt="ok")

    with (
        patch("workflows.webhooks.resolve_webhook_target", return_value=target),
        patch("workflows.webhooks._send_webhook_request", side_effect=send),
    ):
        assert deliver_webhook(delivery) is True

    delivery.refresh_from_db()
    assert delivery.state == WebhookDelivery.State.DELIVERED
    assert delivery.delivered_at is not None
    assert delivery.target_url == original_url
    assert delivery.secret_version == 1
    assert captured["target"] == target
    assert captured["body"] == canonical_webhook_body(event.payload)
    headers = captured["headers"]
    assert headers["X-BioWorkflow-Delivery-ID"] == str(delivery.id)
    assert headers["X-BioWorkflow-Event-ID"] == str(event.id)
    timestamp = int(headers["X-BioWorkflow-Timestamp"])
    signed = b".".join(
        [
            str(delivery.id).encode(),
            str(event.id).encode(),
            str(timestamp).encode(),
            captured["body"],
        ]
    )
    expected = hmac.new(
        original_secret,
        signed,
        hashlib.sha256,
    ).hexdigest()
    assert headers["X-BioWorkflow-Signature"] == f"v1={expected}"
    verified = verify_webhook_signature(
        base64.urlsafe_b64encode(original_secret)
        .rstrip(b"=")
        .decode(),
        headers,
        captured["body"],
        now_timestamp=timestamp,
    )
    assert verified["event_id"] == event.id
    assert verified["delivery_id"] == delivery.id
    with pytest.raises(WebhookError) as tampered:
        verify_webhook_signature(
            base64.urlsafe_b64encode(original_secret)
            .rstrip(b"=")
            .decode(),
            headers,
            captured["body"] + b" ",
            now_timestamp=timestamp,
        )
    assert tampered.value.code == "WEBHOOK_SIGNATURE_INVALID"
    with pytest.raises(WebhookError) as replayed:
        verify_webhook_signature(
            base64.urlsafe_b64encode(original_secret)
            .rstrip(b"=")
            .decode(),
            headers,
            captured["body"],
            now_timestamp=timestamp + 301,
        )
    assert replayed.value.code == "WEBHOOK_TIMESTAMP_OUT_OF_RANGE"
    attempt = delivery.attempts.get()
    assert attempt.outcome == WebhookDeliveryAttempt.Outcome.DELIVERED
    assert attempt.resolved_address == target.address
    assert attempt.status_code == 204


@pytest.mark.django_db
def test_exponential_retry_dead_letter_and_manual_replay(settings):
    _settings(settings)
    account = _account()
    _endpoint(account)
    run = _run(account)
    _terminal_event(run)
    target = WebhookTarget(
        scheme="https",
        hostname="hooks.example.test",
        port=443,
        request_target="/analysis-events",
        address="93.184.216.34",
    )

    with (
        patch("workflows.webhooks.resolve_webhook_target", return_value=target),
        patch(
            "workflows.webhooks._send_webhook_request",
            return_value=WebhookHTTPResult(503, "unavailable"),
        ),
    ):
        for attempt_number in range(1, 4):
            delivery = claim_next_delivery()
            assert delivery is not None
            before = timezone.now()
            assert deliver_webhook(delivery) is True
            delivery.refresh_from_db()
            if attempt_number < 3:
                assert delivery.state == WebhookDelivery.State.PENDING
                minimum_delay = 10 * (2 ** (attempt_number - 1))
                assert delivery.next_attempt_at >= before + timedelta(
                    seconds=minimum_delay
                )
                WebhookDelivery.objects.filter(pk=delivery.pk).update(
                    next_attempt_at=timezone.now() - timedelta(seconds=1)
                )
            else:
                assert delivery.state == WebhookDelivery.State.DEAD_LETTER

    assert delivery.attempt_count == 3
    assert delivery.attempts.count() == 3
    assert AnalysisRun.objects.get(pk=run.pk).status == AnalysisRun.Status.SUCCEEDED

    dead_letter_output = StringIO()
    call_command(
        "webhook_delivery_stats",
        state=WebhookDelivery.State.DEAD_LETTER,
        client_id=account.client_id,
        stdout=dead_letter_output,
    )
    dead_letters = json.loads(dead_letter_output.getvalue())["deliveries"]
    assert dead_letters[0]["delivery_id"] == str(delivery.id)
    assert dead_letters[0]["run_id"] == str(run.id)

    output = StringIO()
    call_command(
        "replay_webhook_delivery",
        delivery_id=str(delivery.id),
        actor="operator@example.test",
        stdout=output,
    )
    delivery.refresh_from_db()
    assert delivery.state == WebhookDelivery.State.PENDING
    assert delivery.replay_count == 1
    assert delivery.last_replayed_by == "operator@example.test"
    assert "REPLAY_QUEUED" in output.getvalue()

    with (
        patch("workflows.webhooks.resolve_webhook_target", return_value=target),
        patch(
            "workflows.webhooks._send_webhook_request",
            return_value=WebhookHTTPResult(200, "ok"),
        ),
    ):
        replay = claim_next_delivery()
        assert replay is not None
        assert deliver_webhook(replay) is True
    delivery.refresh_from_db()
    assert delivery.state == WebhookDelivery.State.DELIVERED
    assert delivery.attempt_count == 4
    assert delivery.attempts.get(attempt_number=4).replay_number == 1
    with pytest.raises(CommandError, match="WEBHOOK_DELIVERY_ID_INVALID"):
        call_command("replay_webhook_delivery", delivery_id="not-a-uuid")


@pytest.mark.django_db
def test_expired_dispatcher_lease_is_retried_with_same_delivery_id(settings):
    _settings(settings)
    account = _account()
    _endpoint(account)
    _terminal_event(_run(account))
    first = claim_next_delivery()
    assert first is not None
    assert claim_next_delivery() is None
    WebhookDelivery.objects.filter(pk=first.pk).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1)
    )

    second = claim_next_delivery()

    assert second is not None
    assert second.id == first.id
    assert second.attempt_count == 2
    first_attempt = second.attempts.get(attempt_number=1)
    assert first_attempt.outcome == WebhookDeliveryAttempt.Outcome.LEASE_EXPIRED
    assert first_attempt.finished_at is not None


@pytest.mark.django_db
def test_terminal_cancel_and_stale_worker_paths_enqueue_outbox(settings):
    _settings(settings)
    account = _account()
    _endpoint(account)
    token, raw_token = issue_service_token(
        account,
        name="pytest",
        actor="pytest",
    )
    queued = _run(account, status=AnalysisRun.Status.QUEUED)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw_token}")

    canceled = client.post(f"/api/v1/integration/analysis-runs/{queued.id}/cancel")

    assert canceled.status_code == 200, canceled.data
    assert canceled.data["status"] == AnalysisRun.Status.CANCELED
    assert queued.outbox_events.get().payload["data"]["status_version"] == 2

    cancel_requested = _run(account, status=AnalysisRun.Status.CANCEL_REQUESTED)
    cancel_requested.status_version = 2
    cancel_requested.lease_token = uuid.uuid4()
    cancel_requested.save()
    assert _finalize_cancelled_run(
        cancel_requested.pk,
        cancel_requested.lease_token,
    )
    assert cancel_requested.outbox_events.get().payload["data"]["status"] == (
        AnalysisRun.Status.CANCELED
    )

    stale = _run(account, status=AnalysisRun.Status.RUNNING)
    stale.lease_token = uuid.uuid4()
    stale.lease_expires_at = timezone.now() - timedelta(seconds=1)
    stale.worker_heartbeat_at = timezone.now() - timedelta(minutes=5)
    stale.save()
    with transaction.atomic():
        _recover_stale_runs(timezone.now())
    stale.refresh_from_db()
    assert stale.status == AnalysisRun.Status.FAILED
    assert stale.outbox_events.get().payload["data"]["status"] == (
        AnalysisRun.Status.FAILED
    )
    assert token.service_account == account


@pytest.mark.django_db
def test_target_validation_blocks_ssrf_and_allows_explicit_private_hosts(
    settings,
):
    _settings(settings)
    with pytest.raises(WebhookError) as insecure:
        resolve_webhook_target("http://example.test/hook")
    assert insecure.value.code == "WEBHOOK_HTTPS_REQUIRED"

    private_record = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443))
    ]
    with patch("workflows.webhooks.socket.getaddrinfo", return_value=private_record):
        with pytest.raises(WebhookError) as private:
            resolve_webhook_target("https://internal.example.test/hook")
    assert private.value.code == "WEBHOOK_TARGET_PRIVATE_ADDRESS"

    mixed_records = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
    ]
    with patch("workflows.webhooks.socket.getaddrinfo", return_value=mixed_records):
        with pytest.raises(WebhookError, match="私网"):
            resolve_webhook_target("https://mixed.example.test/hook")

    settings.WEBHOOK_PRIVATE_HOST_ALLOWLIST = ["internal.example.test"]
    with patch("workflows.webhooks.socket.getaddrinfo", return_value=private_record):
        target = resolve_webhook_target("https://internal.example.test/hook")
    assert target.address == "10.0.0.8"

    settings.WEBHOOK_ALLOWED_HTTP_HOSTS = ["127.0.0.1"]
    settings.WEBHOOK_PRIVATE_HOST_ALLOWLIST = ["127.0.0.1"]
    local = resolve_webhook_target("http://127.0.0.1:8080/hook")
    assert local.scheme == "http"
    assert local.address == "127.0.0.1"

    with pytest.raises(WebhookError, match="userinfo"):
        resolve_webhook_target("https://user:password@example.test/hook")

    multicast_record = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("224.0.0.1", 443))
    ]
    with patch("workflows.webhooks.socket.getaddrinfo", return_value=multicast_record):
        with pytest.raises(WebhookError, match="私网"):
            resolve_webhook_target("https://multicast.example.test/hook")

    with pytest.raises(WebhookError, match="端口"):
        resolve_webhook_target("https://example.test:0/hook")

    with pytest.raises(WebhookError, match="percent-encoding"):
        resolve_webhook_target("https://example.test/回调")


@pytest.mark.django_db
def test_endpoint_command_rotates_derived_secret_and_metrics_are_machine_readable(
    settings,
):
    _settings(settings)
    account = _account()
    settings.WEBHOOK_ALLOWED_HTTP_HOSTS = ["127.0.0.1"]
    settings.WEBHOOK_PRIVATE_HOST_ALLOWLIST = ["127.0.0.1"]
    created_output = StringIO()

    call_command(
        "manage_webhook_endpoint",
        client_id=account.client_id,
        name="terminal",
        url="http://127.0.0.1:8080/hooks",
        actor="pytest",
        stdout=created_output,
    )

    endpoint = WebhookEndpoint.objects.get()
    created_secret = next(
        line.split("=", 1)[1]
        for line in created_output.getvalue().splitlines()
        if line.startswith("SIGNING_SECRET=")
    )
    assert len(base64.urlsafe_b64decode(created_secret + "==")) == 32
    assert created_secret not in endpoint.url
    assert created_secret not in str(endpoint.secret_salt)

    rotated_output = StringIO()
    call_command(
        "manage_webhook_endpoint",
        client_id=account.client_id,
        name="terminal",
        rotate_secret=True,
        actor="pytest",
        stdout=rotated_output,
    )
    endpoint.refresh_from_db()
    rotated_secret = next(
        line.split("=", 1)[1]
        for line in rotated_output.getvalue().splitlines()
        if line.startswith("SIGNING_SECRET=")
    )
    assert endpoint.secret_version == 2
    assert rotated_secret != created_secret

    _terminal_event(_run(account))
    metrics = webhook_delivery_metrics()
    assert metrics["states"][WebhookDelivery.State.PENDING] == 1
    stats_output = StringIO()
    call_command("webhook_delivery_stats", stdout=stats_output)
    assert json.loads(stats_output.getvalue())["due"] == 1


@pytest.mark.django_db
def test_dispatcher_rejects_invalid_signing_key_before_claiming_delivery(settings):
    _settings(settings)
    account = _account()
    _endpoint(account)
    _terminal_event(_run(account))
    settings.WEBHOOK_SIGNING_KEY = "too-short"

    with pytest.raises(CommandError, match="至少需要 32"):
        call_command("run_webhook_dispatcher", once=True)

    delivery = WebhookDelivery.objects.get()
    assert delivery.state == WebhookDelivery.State.PENDING
    assert delivery.attempt_count == 0
