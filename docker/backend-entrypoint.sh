#!/bin/sh
set -eu

python backend/manage.py migrate --noinput
if [ "${DJANGO_SEED_DEMO:-0}" = "1" ]; then
    python backend/manage.py seed_demo
fi
exec gunicorn config.wsgi:application --chdir backend --bind 0.0.0.0:8000 --workers 2 --threads 4 --timeout 30 --access-logfile -
