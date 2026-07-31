#!/bin/sh
set -eu

python backend/manage.py migrate --noinput
python backend/manage.py seed_demo
exec gunicorn config.wsgi:application --chdir backend --bind 0.0.0.0:8000 --workers 2 --threads 4 --timeout 30 --access-logfile -

