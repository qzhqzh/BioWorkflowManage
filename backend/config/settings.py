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
ANALYSIS_INPUT_STAGING_ROOT = Path(
    os.environ.get(
        "ANALYSIS_INPUT_STAGING_ROOT",
        BASE_DIR / "data" / "input-staging",
    )
)
ANALYSIS_INPUT_STAGING_EXECUTION_ROOT = Path(
    os.environ.get(
        "ANALYSIS_INPUT_STAGING_EXECUTION_ROOT",
        ANALYSIS_INPUT_STAGING_ROOT,
    )
)
ANALYSIS_OBJECT_STORAGE_PROFILE_DIR = Path(
    os.environ.get(
        "ANALYSIS_OBJECT_STORAGE_PROFILE_DIR",
        BASE_DIR / "secrets" / "object-storage",
    )
)
ANALYSIS_OBJECT_HEAD_TIMEOUT_SECONDS = max(
    0.1,
    float(os.environ.get("ANALYSIS_OBJECT_HEAD_TIMEOUT_SECONDS", "10")),
)
ANALYSIS_OBJECT_HEAD_REQUEST_TIMEOUT_SECONDS = min(
    20.0,
    max(
        0.1,
        float(os.environ.get("ANALYSIS_OBJECT_HEAD_REQUEST_TIMEOUT_SECONDS", "20")),
    ),
)
ANALYSIS_OBJECT_HEAD_MAX_CONCURRENT = max(
    1,
    int(os.environ.get("ANALYSIS_OBJECT_HEAD_MAX_CONCURRENT", "8")),
)
ANALYSIS_OBJECT_STAGE_TIMEOUT_SECONDS = max(
    1.0,
    float(os.environ.get("ANALYSIS_OBJECT_STAGE_TIMEOUT_SECONDS", "3600")),
)
ANALYSIS_OBJECT_STAGE_RUN_TIMEOUT_SECONDS = max(
    ANALYSIS_OBJECT_STAGE_TIMEOUT_SECONDS,
    float(os.environ.get("ANALYSIS_OBJECT_STAGE_RUN_TIMEOUT_SECONDS", "7200")),
)
ANALYSIS_OBJECT_STAGE_SLOT_WAIT_SECONDS = max(
    0.0,
    float(os.environ.get("ANALYSIS_OBJECT_STAGE_SLOT_WAIT_SECONDS", "600")),
)
ANALYSIS_OBJECT_STAGE_LEASE_SECONDS = max(
    int(ANALYSIS_OBJECT_STAGE_RUN_TIMEOUT_SECONDS) + 60,
    int(os.environ.get("ANALYSIS_OBJECT_STAGE_LEASE_SECONDS", "7260")),
)
ANALYSIS_OBJECT_STAGE_MAX_CONCURRENT_RUNS = max(
    1,
    int(os.environ.get("ANALYSIS_OBJECT_STAGE_MAX_CONCURRENT_RUNS", "2")),
)
ANALYSIS_OBJECT_STAGE_MAX_ITEMS = max(
    1,
    int(os.environ.get("ANALYSIS_OBJECT_STAGE_MAX_ITEMS", "64")),
)
ANALYSIS_OBJECT_STAGE_MAX_OBJECT_BYTES = max(
    1,
    int(os.environ.get("ANALYSIS_OBJECT_STAGE_MAX_OBJECT_BYTES", "1099511627776")),
)
ANALYSIS_OBJECT_STAGE_MAX_RUN_BYTES = max(
    ANALYSIS_OBJECT_STAGE_MAX_OBJECT_BYTES,
    int(os.environ.get("ANALYSIS_OBJECT_STAGE_MAX_RUN_BYTES", "2199023255552")),
)
ANALYSIS_OBJECT_STAGE_MAX_RESERVED_BYTES = max(
    ANALYSIS_OBJECT_STAGE_MAX_RUN_BYTES,
    int(
        os.environ.get(
            "ANALYSIS_OBJECT_STAGE_MAX_RESERVED_BYTES",
            "4398046511104",
        )
    ),
)
ANALYSIS_OBJECT_STAGE_MIN_FREE_BYTES = max(
    0,
    int(os.environ.get("ANALYSIS_OBJECT_STAGE_MIN_FREE_BYTES", "10737418240")),
)
ANALYSIS_OBJECT_STAGE_CHUNK_BYTES = min(
    64 * 1024 * 1024,
    max(
        64 * 1024,
        int(os.environ.get("ANALYSIS_OBJECT_STAGE_CHUNK_BYTES", "8388608")),
    ),
)
ANALYSIS_ARTIFACT_EXPORT_ROOT = Path(
    os.environ.get(
        "ANALYSIS_ARTIFACT_EXPORT_ROOT",
        BASE_DIR / "data" / "artifact-exports",
    )
)
ANALYSIS_ARTIFACT_EXPORT_PROFILE_DIR = Path(
    os.environ.get(
        "ANALYSIS_ARTIFACT_EXPORT_PROFILE_DIR",
        BASE_DIR / "secrets" / "artifact-export",
    )
)
ANALYSIS_ARTIFACT_EXPORT_TIMEOUT_SECONDS = max(
    1.0,
    float(os.environ.get("ANALYSIS_ARTIFACT_EXPORT_TIMEOUT_SECONDS", "3600")),
)
ANALYSIS_ARTIFACT_EXPORT_POLL_SECONDS = max(
    0.2,
    float(os.environ.get("ANALYSIS_ARTIFACT_EXPORT_POLL_SECONDS", "2")),
)
ANALYSIS_ARTIFACT_EXPORT_LEASE_SECONDS = max(
    int(ANALYSIS_ARTIFACT_EXPORT_TIMEOUT_SECONDS) + 60,
    int(os.environ.get("ANALYSIS_ARTIFACT_EXPORT_LEASE_SECONDS", "3660")),
)
ANALYSIS_ARTIFACT_EXPORT_MAX_ATTEMPTS = max(
    1,
    int(os.environ.get("ANALYSIS_ARTIFACT_EXPORT_MAX_ATTEMPTS", "5")),
)
ANALYSIS_ARTIFACT_EXPORT_BACKOFF_BASE_SECONDS = max(
    1.0,
    float(os.environ.get("ANALYSIS_ARTIFACT_EXPORT_BACKOFF_BASE_SECONDS", "5")),
)
ANALYSIS_ARTIFACT_EXPORT_BACKOFF_MAX_SECONDS = max(
    ANALYSIS_ARTIFACT_EXPORT_BACKOFF_BASE_SECONDS,
    float(os.environ.get("ANALYSIS_ARTIFACT_EXPORT_BACKOFF_MAX_SECONDS", "3600")),
)
ANALYSIS_ARTIFACT_EXPORT_CHUNK_BYTES = min(
    64 * 1024 * 1024,
    max(
        64 * 1024,
        int(os.environ.get("ANALYSIS_ARTIFACT_EXPORT_CHUNK_BYTES", "8388608")),
    ),
)
ANALYSIS_ARTIFACT_EXPORT_MANIFEST_MAX_BYTES = max(
    1024,
    int(os.environ.get("ANALYSIS_ARTIFACT_EXPORT_MANIFEST_MAX_BYTES", "1048576")),
)
ANALYSIS_ARTIFACT_RETENTION_MIN_DAYS = max(
    0,
    int(os.environ.get("ANALYSIS_ARTIFACT_RETENTION_MIN_DAYS", "30")),
)
ANALYSIS_ARTIFACT_RETENTION_MAX_DAYS = max(
    ANALYSIS_ARTIFACT_RETENTION_MIN_DAYS,
    int(os.environ.get("ANALYSIS_ARTIFACT_RETENTION_MAX_DAYS", "3650")),
)
ANALYSIS_ARTIFACT_CLEANUP_LEASE_SECONDS = max(
    60,
    int(os.environ.get("ANALYSIS_ARTIFACT_CLEANUP_LEASE_SECONDS", "3600")),
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

WEBHOOK_SIGNING_KEY = os.environ.get("WEBHOOK_SIGNING_KEY") or (
    f"bioworkflow-webhook-v1:{SECRET_KEY}"
)
WEBHOOK_DELIVERY_TIMEOUT_SECONDS = max(
    0.1,
    float(os.environ.get("WEBHOOK_DELIVERY_TIMEOUT_SECONDS", "10")),
)
WEBHOOK_DELIVERY_POLL_SECONDS = max(
    0.2,
    float(os.environ.get("WEBHOOK_DELIVERY_POLL_SECONDS", "2")),
)
WEBHOOK_DELIVERY_LEASE_SECONDS = max(
    30,
    int(os.environ.get("WEBHOOK_DELIVERY_LEASE_SECONDS", "60")),
)
WEBHOOK_MAX_ATTEMPTS = max(
    1,
    int(os.environ.get("WEBHOOK_MAX_ATTEMPTS", "8")),
)
WEBHOOK_BACKOFF_BASE_SECONDS = max(
    1.0,
    float(os.environ.get("WEBHOOK_BACKOFF_BASE_SECONDS", "5")),
)
WEBHOOK_BACKOFF_MAX_SECONDS = max(
    WEBHOOK_BACKOFF_BASE_SECONDS,
    float(os.environ.get("WEBHOOK_BACKOFF_MAX_SECONDS", "3600")),
)
WEBHOOK_RESPONSE_MAX_BYTES = max(
    0,
    int(os.environ.get("WEBHOOK_RESPONSE_MAX_BYTES", "2048")),
)
WEBHOOK_ALLOWED_HTTP_HOSTS = _csv_environment("WEBHOOK_ALLOWED_HTTP_HOSTS", "")
WEBHOOK_PRIVATE_HOST_ALLOWLIST = _csv_environment(
    "WEBHOOK_PRIVATE_HOST_ALLOWLIST",
    "",
)
