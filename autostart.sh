#!/bin/sh
# Startuje instancje aplikacji Najem w tle (gunicorn), jeśli jeszcze nie działają.
# Jeden kod, osobne dane: każda instancja ma własny katalog (baza, klucz sesji, zdjęcia, log, PID),
# własny port i odpowiada tylko na swoją domenę.
#
#   demo (dane fikcyjne)   najem.bk.waw.pl          :8000   instance/
#   prawdziwe dane         asystentnajmu.bk.waw.pl  :8001   /workspace/asystentnajmu-dane/
#
# Można wołać wielokrotnie — działająca instancja jest pomijana.
# Przy starcie kontenera:  sh -c '/workspace/najem/autostart.sh; exec sleep infinity'
# Przeładowanie po zmianach w kodzie (każda instancja osobno):
#   kill -HUP "$(cat instance/gunicorn.pid)"
#   kill -HUP "$(cat /workspace/asystentnajmu-dane/gunicorn.pid)"
cd "$(dirname "$0")" || exit 1
GUNICORN=../.venv/bin/gunicorn

start() {   # nazwa katalog port domeny
    nazwa=$1; katalog=$2; port=$3; hosty=$4
    pid="$katalog/gunicorn.pid"; log="$katalog/gunicorn.log"
    if [ -f "$pid" ] && kill -0 "$(cat "$pid")" 2>/dev/null; then
        echo "$nazwa już działa (PID $(cat "$pid"), port $port)."
        return 0
    fi
    mkdir -p "$katalog"
    NAJEM_INSTANCE="$katalog" NAJEM_HOSTY="$hosty" "$GUNICORN" -w 2 -b "0.0.0.0:$port" \
        --daemon --pid "$pid" --access-logfile "$log" --error-logfile "$log" app:app || return 1
    i=0
    while [ $i -lt 20 ]; do
        if [ -f "$pid" ] && kill -0 "$(cat "$pid")" 2>/dev/null; then
            echo "$nazwa uruchomiony (PID $(cat "$pid"), port $port), log: $log"
            return 0
        fi
        sleep 0.5
        i=$((i + 1))
    done
    echo "$nazwa nie wystartował — zobacz $log" >&2
    return 1
}

ok=0
start "Najem demo" instance 8000 najem.bk.waw.pl || ok=1
start "Asystent najmu" /workspace/asystentnajmu-dane 8001 asystentnajmu.bk.waw.pl || ok=1

# codzienna kopia danych asystentnajmu (lokalnie + Google Drive przez rclone) — zob. kopia.sh
HPID=/workspace/asystentnajmu-kopie/harmonogram.pid
if [ -f "$HPID" ] && kill -0 "$(cat "$HPID")" 2>/dev/null; then
    echo "Harmonogram kopii już działa (PID $(cat "$HPID"))."
else
    mkdir -p /workspace/asystentnajmu-kopie
    nohup setsid sh ./kopia-harmonogram.sh >/dev/null 2>&1 &
    echo "Harmonogram kopii uruchomiony (codziennie po 3:00), log: /workspace/asystentnajmu-kopie/kopia.log"
fi
exit $ok
