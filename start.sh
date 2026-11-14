#!/bin/sh
# Uruchamia aplikację Najem na porcie 8000 (za proxy OpenResty z HTTPS).
cd "$(dirname "$0")"
exec ../.venv/bin/gunicorn -w 2 -b 0.0.0.0:8000 --access-logfile - app:app
