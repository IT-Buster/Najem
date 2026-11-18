#!/bin/sh
# Harmonogram kopii (w kontenerze nie ma cron): co 10 minut sprawdza, czy po GODZINA:00 jest już dzisiejsza kopia;
# jeśli nie — uruchamia kopia.sh. Nieudaną próbę ponawia po godzinie. Uruchamiany w tle przez autostart.sh.
KOPIE=${NAJEM_KOPIE:-/workspace/asystentnajmu-kopie}
GODZINA=${KOPIA_GODZINA:-3}
cd "$(dirname "$0")" || exit 1
mkdir -p "$KOPIE"
echo $$ > "$KOPIE/harmonogram.pid"
while true; do
    dzis=$(date +%Y-%m-%d)
    if [ "$(date +%H)" -ge "$GODZINA" ] && ! ls "$KOPIE"/asystentnajmu-"$dzis"_*.tar.gz >/dev/null 2>&1; then
        proba="$KOPIE/.ostatnia_proba"
        if [ ! -f "$proba" ] || [ $(( $(date +%s) - $(cat "$proba") )) -ge 3600 ]; then
            date +%s > "$proba"
            sh ./kopia.sh >/dev/null 2>&1 || echo "$(date '+%Y-%m-%d %H:%M:%S') BŁĄD: kopia.sh zakończony błędem" >> "$KOPIE/kopia.log"
        fi
    fi
    sleep 600
done
