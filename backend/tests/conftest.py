import pytest


@pytest.fixture(autouse=True)
def allow_django_test_client_host(settings):
    if "testserver" not in settings.ALLOWED_HOSTS:
        settings.ALLOWED_HOSTS = [*settings.ALLOWED_HOSTS, "testserver"]


@pytest.fixture(autouse=True)
def disable_auth_for_existing_api_contracts(settings):
    """Keep legacy API tests focused; auth tests opt in explicitly."""

    settings.AUTH_REQUIRED = False
