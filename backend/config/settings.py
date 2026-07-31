from __future__ import annotations

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "development-only-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")
    if host.strip()
]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "workflows",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]
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
