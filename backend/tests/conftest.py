import pytest


@pytest.fixture(autouse=True)
def allow_django_test_client_host(settings):
    if "testserver" not in settings.ALLOWED_HOSTS:
        settings.ALLOWED_HOSTS = [*settings.ALLOWED_HOSTS, "testserver"]


@pytest.fixture
def auth_disabled(settings):
    """Explicitly isolate API contract tests that are not authentication tests."""
    settings.AUTH_REQUIRED = False
