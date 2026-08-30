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
CSRF_FAILURE_VIEW = "workflows.auth_views.csrf_failure"
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
    "workflows.request_ids.IntegrationRequestIdMiddleware",
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
CORS_ALLOWED_HEADERS = [
    "Accept",
    "Authorization",
    "Content-Type",
    "Idempotency-Key",
    "X-Request-ID",
    "X-CSRFToken",
]
CORS_EXPOSE_HEADERS = ["Idempotency-Replayed", "X-Request-ID"]
CORS_PREFLIGHT_MAX_AGE = 600
CORS_ALLOW_CREDENTIALS = True
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

# Authentication is enabled by default. Contract-focused API test modules opt out
# explicitly; authentication tests exercise the deployed setting.
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
        "workflows.auth_permissions.ServiceTokenAuthentication",
        "workflows.auth_permissions.SessionAuthenticationWithHeader",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "workflows.auth_permissions.AuthenticationRequiredPermission",
    ],
    "EXCEPTION_HANDLER": "workflows.integration_exceptions.integration_exception_handler",
    "UNAUTHENTICATED_USER": None,
    "NUM_PROXIES": max(0, int(os.environ.get("DJANGO_TRUSTED_PROXY_COUNT", "1"))),
    "DEFAULT_THROTTLE_RATES": {
        "login": os.environ.get("DJANGO_LOGIN_THROTTLE_RATE", "30/min"),
    },
}
LOGIN_THROTTLE_RETENTION_DAYS = max(
    1,
    int(os.environ.get("DJANGO_LOGIN_THROTTLE_RETENTION_DAYS", "7")),
)

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
RAWDATA_SCAN_BATCH_ENTRIES = max(
    100,
    int(os.environ.get("RAWDATA_SCAN_BATCH_ENTRIES", "1000")),
)
RAWDATA_SCAN_MAX_FILES = max(
    RAWDATA_SCAN_BATCH_ENTRIES,
    int(os.environ.get("RAWDATA_SCAN_MAX_FILES", "20000")),
)
RAWDATA_SCAN_MAX_ENTRIES = max(
    RAWDATA_SCAN_BATCH_ENTRIES,
    int(os.environ.get("RAWDATA_SCAN_MAX_ENTRIES", "100000")),
)
RAWDATA_SCAN_MAX_DEPTH = max(
    1,
    int(os.environ.get("RAWDATA_SCAN_MAX_DEPTH", "8")),
)
RAWDATA_SCAN_BATCH_SECONDS = max(
    0.25,
    float(os.environ.get("RAWDATA_SCAN_BATCH_SECONDS", "2")),
)
ANALYSIS_RESOURCE_MANIFEST_TIMEOUT_SECONDS = max(
    0.1,
    float(os.environ.get("ANALYSIS_RESOURCE_MANIFEST_TIMEOUT_SECONDS", "2")),
)
ANALYSIS_WORKER_RESOURCE_MANIFEST_TIMEOUT_SECONDS = max(
    0.1,
    float(
        os.environ.get(
            "ANALYSIS_WORKER_RESOURCE_MANIFEST_TIMEOUT_SECONDS",
            "300",
        )
    ),
)
ANALYSIS_RESOURCE_MANIFEST_MAX_ENTRIES = max(
    1,
    int(os.environ.get("ANALYSIS_RESOURCE_MANIFEST_MAX_ENTRIES", "100000")),
)
ANALYSIS_RESOURCE_MANIFEST_MAX_DEPTH = max(
    1,
    int(os.environ.get("ANALYSIS_RESOURCE_MANIFEST_MAX_DEPTH", "128")),
)
ANALYSIS_MANAGED_RESOURCE_MAX_ITEMS = max(
    1,
    int(os.environ.get("ANALYSIS_MANAGED_RESOURCE_MAX_ITEMS", "256")),
)
ANALYSIS_INPUT_TEXT_LINE_MAX_CHARS = max(
    1,
    int(os.environ.get("ANALYSIS_INPUT_TEXT_LINE_MAX_CHARS", "1048576")),
)
ANALYSIS_INPUT_GZIP_HEADER_MAX_BYTES = max(
    10,
    int(os.environ.get("ANALYSIS_INPUT_GZIP_HEADER_MAX_BYTES", "65536")),
)
ANALYSIS_MANAGED_FILE_CHECKSUM_MAX_BYTES = max(
    1,
    int(os.environ.get("ANALYSIS_MANAGED_FILE_CHECKSUM_MAX_BYTES", "17179869184")),
)
ANALYSIS_OUTPUT_SNAPSHOT_MAX_ITEMS = max(
    1,
    int(os.environ.get("ANALYSIS_OUTPUT_SNAPSHOT_MAX_ITEMS", "256")),
)
ANALYSIS_OUTPUT_SNAPSHOT_MAX_BYTES = max(
    1,
    int(os.environ.get("ANALYSIS_OUTPUT_SNAPSHOT_MAX_BYTES", "1099511627776")),
)
ANALYSIS_OUTPUT_SNAPSHOT_MAX_DIRECTORY_ENTRIES = max(
    1,
    int(
        os.environ.get(
            "ANALYSIS_OUTPUT_SNAPSHOT_MAX_DIRECTORY_ENTRIES",
            "200000",
        )
    ),
)
ANALYSIS_OUTPUT_SNAPSHOT_TIMEOUT_SECONDS = max(
    0.1,
    float(os.environ.get("ANALYSIS_OUTPUT_SNAPSHOT_TIMEOUT_SECONDS", "300")),
)
ANALYSIS_OUTPUT_SNAPSHOT_MIN_FREE_BYTES = max(
    0,
    int(os.environ.get("ANALYSIS_OUTPUT_SNAPSHOT_MIN_FREE_BYTES", "1073741824")),
)
ANALYSIS_OUTPUT_MANIFEST_MAX_DEPTH = max(
    1,
    int(os.environ.get("ANALYSIS_OUTPUT_MANIFEST_MAX_DEPTH", "32")),
)
ANALYSIS_OUTPUT_VALUE_MAX_BYTES = max(
    1,
    int(os.environ.get("ANALYSIS_OUTPUT_VALUE_MAX_BYTES", "65536")),
)
ANALYSIS_RESULT_JSON_MAX_BYTES = max(
    1,
    int(os.environ.get("ANALYSIS_RESULT_JSON_MAX_BYTES", "67108864")),
)
RAWDATA_SCAN_LEASE_SECONDS = max(
    30,
    int(os.environ.get("RAWDATA_SCAN_LEASE_SECONDS", "60")),
)
RAWDATA_INDEX_INTERVAL_SECONDS = max(
    30,
    int(os.environ.get("RAWDATA_INDEX_INTERVAL_SECONDS", "300")),
)
RAWDATA_INDEX_STALE_SECONDS = max(
    RAWDATA_INDEX_INTERVAL_SECONDS,
    int(os.environ.get("RAWDATA_INDEX_STALE_SECONDS", "900")),
)
RAWDATA_MANUAL_SCAN_COOLDOWN_SECONDS = max(
    5,
    int(os.environ.get("RAWDATA_MANUAL_SCAN_COOLDOWN_SECONDS", "30")),
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
