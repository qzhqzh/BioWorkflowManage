from __future__ import annotations

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]


def _csv_environment(name: str, default: str) -> list[str]:
    return [
        value.strip()
        for value in os.environ.get(name, default).split(",")
        if value.strip()
    ]


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "development-only-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = _csv_environment(
    "DJANGO_ALLOWED_HOSTS",
    "localhost,127.0.0.1,[::1],wdl.qzhqzh.com",
)
CSRF_TRUSTED_ORIGINS = _csv_environment(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    "https://wdl.qzhqzh.com,http://localhost:8082,http://127.0.0.1:8082",
)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "rest_framework",
    "workflows",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "config.cors.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
]
CORS_ALLOWED_ORIGINS = _csv_environment(
    "CORS_ALLOWED_ORIGINS",
    "https://wdl.qzhqzh.com,http://localhost:3000,http://127.0.0.1:3000",
)
CORS_ALLOWED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
CORS_ALLOWED_HEADERS = ["Accept", "Content-Type", "X-Request-ID", "X-CSRFToken"]
CORS_PREFLIGHT_MAX_AGE = 600
CORS_ALLOW_CREDENTIALS = True
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

# Authentication is enabled by default for deployed services. Test settings turn
# this off by default so the pre-authentication API tests remain focused on their
# existing contracts; authentication tests opt back in explicitly.
AUTH_REQUIRED = os.environ.get("DJANGO_AUTH_REQUIRED", "1") == "1"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = os.environ.get("DJANGO_SESSION_COOKIE_SAMESITE", "Lax")
SESSION_COOKIE_SECURE = os.environ.get("DJANGO_SESSION_COOKIE_SECURE", "0") == "1"
CSRF_COOKIE_SAMESITE = os.environ.get("DJANGO_CSRF_COOKIE_SAMESITE", "Lax")
CSRF_COOKIE_SECURE = os.environ.get("DJANGO_CSRF_COOKIE_SECURE", "0") == "1"

if os.environ.get("POSTGRES_HOST"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB", "bioworkflow"),
            "USER": os.environ.get("POSTGRES_USER", "bioworkflow"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ["POSTGRES_HOST"],
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": 60,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "data" / "development.sqlite3",
        }
    }

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "workflows.auth_permissions.SessionAuthenticationWithHeader",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "workflows.auth_permissions.AuthenticationRequiredPermission",
    ],
    "UNAUTHENTICATED_USER": None,
}

SPROCKET_BINARY = os.environ.get("SPROCKET_BINARY", "sprocket")
SPROCKET_FORMAT_CONFIG = os.environ.get(
    "SPROCKET_FORMAT_CONFIG",
    str(BASE_DIR / "docker" / "sprocket-format.toml"),
)
SPROCKET_FORMAT_TIMEOUT_SECONDS = float(
    os.environ.get("SPROCKET_FORMAT_TIMEOUT_SECONDS", "10")
)

ANALYSIS_RAWDATA_ROOT = Path(
    os.environ.get("ANALYSIS_RAWDATA_ROOT", BASE_DIR / "workspace" / "rawdata")
)
ANALYSIS_DATABASE_ROOT = Path(
    os.environ.get("ANALYSIS_DATABASE_ROOT", BASE_DIR / "workspace" / "databases")
)
ANALYSIS_DATABASE_CATALOG = Path(
    os.environ.get(
        "ANALYSIS_DATABASE_CATALOG",
        ANALYSIS_DATABASE_ROOT / "catalog.json",
    )
)
ANALYSIS_RAWDATA_EXECUTION_ROOT = Path(
    os.environ.get("ANALYSIS_RAWDATA_EXECUTION_ROOT", ANALYSIS_RAWDATA_ROOT)
)
ANALYSIS_DATABASE_EXECUTION_ROOT = Path(
    os.environ.get("ANALYSIS_DATABASE_EXECUTION_ROOT", ANALYSIS_DATABASE_ROOT)
)
ANALYSIS_RUN_ROOT = Path(
    os.environ.get("ANALYSIS_RUN_ROOT", BASE_DIR / "data" / "analysis-runs")
)
ANALYSIS_RUN_EXECUTION_ROOT = Path(
    os.environ.get("ANALYSIS_RUN_EXECUTION_ROOT", ANALYSIS_RUN_ROOT)
)
ANALYSIS_WORKER_POLL_SECONDS = float(
    os.environ.get("ANALYSIS_WORKER_POLL_SECONDS", "2")
)
ANALYSIS_MIN_AVAILABLE_MEMORY_GB = float(
    os.environ.get("ANALYSIS_MIN_AVAILABLE_MEMORY_GB", "40")
)
ANALYSIS_INFRASTRUCTURE_RETRIES = int(
    os.environ.get("ANALYSIS_INFRASTRUCTURE_RETRIES", "0")
)
ANALYSIS_INFRASTRUCTURE_RETRY_DELAY_SECONDS = float(
    os.environ.get("ANALYSIS_INFRASTRUCTURE_RETRY_DELAY_SECONDS", "15")
)
ANALYSIS_RUN_LEASE_SECONDS = max(
    60, int(os.environ.get("ANALYSIS_RUN_LEASE_SECONDS", "300"))
)
ANALYSIS_RUN_HEARTBEAT_SECONDS = min(
    max(5, int(os.environ.get("ANALYSIS_RUN_HEARTBEAT_SECONDS", "30"))),
    max(5, ANALYSIS_RUN_LEASE_SECONDS // 3),
)
