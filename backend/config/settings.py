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
    "localhost,127.0.0.1,[::1]",
)

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "workflows",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "config.cors.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
]
CORS_ALLOWED_ORIGINS = _csv_environment(
    "CORS_ALLOWED_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:3000",
)
CORS_ALLOWED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
CORS_ALLOWED_HEADERS = ["Accept", "Content-Type", "X-Request-ID"]
CORS_PREFLIGHT_MAX_AGE = 600
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

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
