from __future__ import annotations

from importlib import import_module

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from rest_framework.test import APIClient


DEFAULT_USERNAMES = (
    "zhuqin",
    "zhangrusong",
    "hejingjing",
    "zhuying",
    "hangzhili",
    "chaohuaiyu",
)


@pytest.fixture
def seeded_users(db, monkeypatch):
    monkeypatch.setenv("DJANGO_SEED_ALLOW_DEFAULT_PASSWORDS", "1")
    call_command("seed_users", verbosity=0)
    user_model = get_user_model()
    return user_model.objects.filter(username__in=DEFAULT_USERNAMES).order_by(
        "username"
    )


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
    assert logged_in.data["user"]["role"] == "admin"
    assert "overview" in logged_in.data["user"]["allowed_sections"]
    assert "wdl" in logged_in.data["user"]["allowed_sections"]

    current = client.get("/api/v1/auth/me")
    assert current.status_code == 200
    assert current.data["user"]["username"] == "zhuqin"
    assert client.get("/api/v1/wdl-assets").status_code == 200

    logged_out = client.post("/api/v1/auth/logout", {}, format="json")
    assert logged_out.status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401


@pytest.mark.django_db
def test_analysis_operator_can_only_access_analysis_api(settings, seeded_users):
    settings.AUTH_REQUIRED = True
    client = APIClient()

    logged_in = client.post(
        "/api/v1/auth/login",
        {"username": "chaohuaiyu", "password": "chaohuaiyu"},
        format="json",
    )

    assert logged_in.status_code == 200
    assert logged_in.data["user"] == {
        "id": seeded_users.get(username="chaohuaiyu").pk,
        "username": "chaohuaiyu",
        "is_admin": False,
        "role": "analysis_operator",
        "allowed_sections": ["rawdata", "runs"],
    }
    assert client.get("/api/v1/analysis/catalog").status_code == 200
    assert client.get("/api/v1/rawdata/catalog").status_code == 200
    assert client.get("/api/v1/analysis-runs").status_code == 200
    assert client.get("/api/v1/wdl-assets").status_code == 403


@pytest.mark.django_db
def test_workflow_maintainer_can_collaborate_on_assets_and_run_analysis(
    settings, seeded_users
):
    settings.AUTH_REQUIRED = True
    client = APIClient()

    logged_in = client.post(
        "/api/v1/auth/login",
        {"username": "zhangrusong", "password": "zhangrusong"},
        format="json",
    )

    assert logged_in.status_code == 200
    assert logged_in.data["user"] == {
        "id": seeded_users.get(username="zhangrusong").pk,
        "username": "zhangrusong",
        "is_admin": False,
        "role": "workflow_maintainer",
        "allowed_sections": [
            "overview",
            "edit",
            "artifacts",
            "packages",
            "tools",
            "resources",
            "rawdata",
            "runs",
            "wdl",
            "help",
        ],
    }
    assert client.get("/api/v1/wdl-assets").status_code == 200
    assert client.get("/api/v1/wdl-packages").status_code == 200
    assert client.get("/api/v1/tools").status_code == 200
    assert client.get("/api/v1/editor/workflows").status_code == 200
    assert client.get("/api/v1/analysis/catalog").status_code == 200
    assert client.get("/api/v1/analysis-runs").status_code == 200
    created = client.post(
        "/api/v1/wdl-assets",
        {
            "name": "协作权限审计",
            "filename": "maintainer-audit.wdl",
            "content": "version 1.0\n\ntask hello {\n  command <<<\n    echo hello\n  >>>\n}\n",
            "note": "workflow maintainer 创建",
        },
        format="json",
    )
    assert created.status_code == 201
    assert created.data["created_by"] == "zhangrusong"
    assert created.data["audit_events"][0]["actor"] == "zhangrusong"


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
def test_seed_users_is_idempotent_and_derives_passwords_from_usernames(monkeypatch):
    monkeypatch.setenv("DJANGO_SEED_ALLOW_DEFAULT_PASSWORDS", "1")
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
        if username == "zhuqin":
            assert user.is_staff
            assert user.is_superuser
        elif username == "chaohuaiyu":
            assert not user.is_staff
            assert not user.is_superuser
            assert user.groups.filter(name="analysis-operators").exists()
            assert not user.groups.filter(name="workflow-maintainers").exists()
        else:
            assert not user.is_staff
            assert not user.is_superuser
            assert not user.groups.filter(name="analysis-operators").exists()
            assert user.groups.filter(name="workflow-maintainers").exists()
    assert Group.objects.filter(name="analysis-operators").exists()
    assert Group.objects.filter(name="workflow-maintainers").exists()


@pytest.mark.django_db
def test_seed_users_does_not_reactivate_disabled_user(monkeypatch):
    monkeypatch.setenv("DJANGO_SEED_ALLOW_DEFAULT_PASSWORDS", "1")
    user_model = get_user_model()
    user = user_model.objects.create_user(
        username="chaohuaiyu",
        password="chaohuaiyu",
        is_active=False,
    )

    call_command("seed_users", verbosity=0)

    user.refresh_from_db()
    assert not user.is_active


@pytest.mark.django_db
def test_workflow_maintainer_data_migration_assigns_existing_default_users():
    migration = import_module("workflows.migrations.0019_workflow_maintainer_role")
    user_model = get_user_model()
    operator_group, _ = Group.objects.get_or_create(name="analysis-operators")
    users = [
        user_model.objects.create_user(username=username, password=None)
        for username in ("zhangrusong", "hejingjing", "zhuying", "hangzhili")
    ]
    for user in users:
        user.groups.add(operator_group)

    migration.assign_existing_maintainers(apps, None)
    migration.assign_existing_maintainers(apps, None)

    for user in users:
        assert user.groups.filter(name="workflow-maintainers").exists()
        assert not user.groups.filter(name="analysis-operators").exists()


@pytest.mark.django_db
def test_seed_users_requires_explicit_password_configuration(monkeypatch):
    monkeypatch.delenv("DJANGO_SEED_ALLOW_DEFAULT_PASSWORDS", raising=False)

    with pytest.raises(CommandError, match="DJANGO_SEED_PASSWORD_ZHUQIN"):
        call_command("seed_users", verbosity=0)


@pytest.mark.django_db
def test_seed_users_preserves_existing_non_target_user_permissions(monkeypatch):
    monkeypatch.setenv("DJANGO_SEED_ALLOW_DEFAULT_PASSWORDS", "1")
    user = get_user_model().objects.create_user(
        username="zhangrusong",
        password="existing-password",
        is_staff=True,
    )

    call_command("seed_users", verbosity=0)

    user.refresh_from_db()
    assert user.is_staff
    assert user.check_password("existing-password")
    assert not user.groups.filter(name="analysis-operators").exists()
