#!/bin/sh
set -eu

if [ "$#" -gt 0 ]; then
    # Management commands run through `docker compose run` must work on a
    # fresh database; normal one-off commands keep their existing semantics.
    if { [ "$1" = "python" ] || [ "$1" = "python3" ]; } && [ "${2:-}" = "backend/manage.py" ]; then
        python backend/manage.py migrate --noinput
    fi
    exec "$@"
fi

python backend/manage.py migrate --noinput
if [ "${DJANGO_SEED_USERS:-0}" = "1" ]; then
    python backend/manage.py seed_users
fi
if [ "${DJANGO_SEED_DEMO:-0}" = "1" ]; then
    python backend/manage.py seed_demo
fi
exec gunicorn config.wsgi:application --chdir backend --bind 0.0.0.0:8000 --workers 2 --threads 4 --timeout 30 --access-logfile -
