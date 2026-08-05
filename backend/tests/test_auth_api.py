from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from rest_framework.test import APIClient


DEFAULT_USERNAMES = (
    "zhuqin",
    "zhangrusong",
    "hejingjing",
    "zhuying",
    "hangzhili",
)


@pytest.fixture
def seeded_users(db):
    call_command("seed_users", verbosity=0)
    user_model = get_user_model()
    return user_model.objects.filter(
        username__in=DEFAULT_USERNAMES
    ).order_by("username")


@pytest.mark.django_db
def test_health_and_csrf_are_public_when_authentication_is_required(settings):
    settings.AUTH_REQUIRED = True
    client = APIClient()

    health = client.get("/api/v1/health")
    csrf = client.get("/api/v1/auth/csrf")

    assert health.status_code == 200
    assert csrf.status_code == 200
    assert csrf.data["csrf_token"]
    assert "csrftoken" in csrf.cookies


@pytest.mark.django_db
def test_protected_api_requires_login_and_login_me_logout_round_trip(
    settings, seeded_users
):
    settings.AUTH_REQUIRED = True
    client = APIClient()

    unauthorized = client.get("/api/v1/wdl-assets")
    assert unauthorized.status_code == 401

    invalid = client.post(
        "/api/v1/auth/login",
        {"username": "zhuqin", "password": "wrong"},
        format="json",
    )
    assert invalid.status_code == 401
    assert invalid.data["error"]["code"] == "AUTH_INVALID_CREDENTIALS"

    logged_in = client.post(
        "/api/v1/auth/login",
        {"username": "zhuqin", "password": "zhuqin"},
        format="json",
    )
    assert logged_in.status_code == 200
    assert logged_in.data["user"]["username"] == "zhuqin"

    current = client.get("/api/v1/auth/me")
    assert current.status_code == 200
    assert current.data["user"]["username"] == "zhuqin"
    assert client.get("/api/v1/wdl-assets").status_code == 200

    logged_out = client.post("/api/v1/auth/logout", {}, format="json")
    assert logged_out.status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401


@pytest.mark.django_db
def test_session_write_requires_csrf_token(settings, seeded_users):
    settings.AUTH_REQUIRED = True
    client = APIClient(enforce_csrf_checks=True)
    assert client.login(username="zhuqin", password="zhuqin")

    csrf = client.get("/api/v1/auth/csrf")
    token = csrf.cookies["csrftoken"].value
    body = {"name": "认证测试标签"}

    missing = client.post("/api/v1/wdl-assets/tags", body, format="json")
    assert missing.status_code == 403

    created = client.post(
        "/api/v1/wdl-assets/tags",
        body,
        format="json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert created.status_code == 201


@pytest.mark.django_db
def test_production_https_origin_can_submit_authenticated_write(settings, seeded_users):
    settings.AUTH_REQUIRED = True
    client = APIClient(enforce_csrf_checks=True)
    assert client.login(username="zhuqin", password="zhuqin")

    csrf = client.get(
        "/api/v1/auth/csrf",
        HTTP_HOST="wdl.qzhqzh.com",
        HTTP_X_FORWARDED_PROTO="https",
    )
    token = csrf.cookies["csrftoken"].value
    created = client.post(
        "/api/v1/wdl-assets/tags",
        {"name": "正式域名认证测试"},
        format="json",
        HTTP_HOST="wdl.qzhqzh.com",
        HTTP_ORIGIN="https://wdl.qzhqzh.com",
        HTTP_X_FORWARDED_PROTO="https",
        HTTP_X_CSRFTOKEN=token,
    )

    assert csrf.status_code == 200
    assert created.status_code == 201


@pytest.mark.django_db
def test_authenticated_wdl_import_records_request_user(settings, seeded_users):
    settings.AUTH_REQUIRED = True
    client = APIClient()
    client.force_login(seeded_users.get(username="zhuqin"))

    response = client.post(
        "/api/v1/wdl-assets",
        {
            "name": "认证审计流程",
            "filename": "audit.wdl",
            "content": "version 1.0\n\ntask hello {\n  command <<<\n    echo hello\n  >>>\n}\n",
        },
        format="json",
    )

    assert response.status_code == 201
    assert response.data["created_by"] == "zhuqin"
    assert response.data["audit_events"][0]["actor"] == "zhuqin"


@pytest.mark.django_db
def test_seed_users_is_idempotent_and_derives_passwords_from_usernames():
    call_command("seed_users", verbosity=0)
    call_command("seed_users", verbosity=0)

    user_model = get_user_model()
    assert user_model.objects.filter(username__in=DEFAULT_USERNAMES).count() == len(
        DEFAULT_USERNAMES
    )
    for username in DEFAULT_USERNAMES:
        user = user_model.objects.get(username=username)
        assert user.is_active
        assert user.check_password(username)
