#!/bin/sh
# Startuje aplikację Najem w tle (gunicorn na :8000), jeśli jeszcze nie działa.
# Można wołać wielokrotnie — drugi start nic nie robi.
#
# Przy starcie kontenera (komenda kontenera zamiast samego `sleep infinity`):
#   sh -c '/workspace/najem/autostart.sh; exec sleep infinity'
#
# Przeładowanie po zmianach w kodzie: kill -HUP "$(cat instance/gunicorn.pid)"
cd "$(dirname "$0")" || exit 1

PID_FILE=instance/gunicorn.pid
LOG_FILE=instance/gunicorn.log

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "Najem już działa (PID $(cat "$PID_FILE"))."
    exit 0
fi

mkdir -p instance
../.venv/bin/gunicorn -w 2 -b 0.0.0.0:8000 \
    --daemon --pid "$PID_FILE" \
    --access-logfile "$LOG_FILE" --error-logfile "$LOG_FILE" \
    app:app || exit 1

# poczekaj, aż aplikacja odpowie
i=0
while [ $i -lt 20 ]; do
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "Najem uruchomiony (PID $(cat "$PID_FILE")), log: $LOG_FILE"
        exit 0
    fi
    sleep 0.5
    i=$((i + 1))
done
echo "Najem nie wystartował — zobacz $LOG_FILE" >&2
exit 1
