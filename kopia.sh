#!/bin/sh
# Kopia zapasowa instancji z prawdziwymi danymi (asystentnajmu.bk.waw.pl).
#
# Tworzy spójną kopię bazy (sqlite3 .backup), sprawdza ją i pakuje razem ze zdjęciami i ustawieniami instancji
# do  KOPIE/asystentnajmu-RRRR-MM-DD_GGMM.tar.gz . Lokalnie trzyma ostatnie LOKALNIE_DNI dni.
# Jeśli jest skonfigurowany rclone (zdalny dysk CEL, np. Google Drive), wysyła kopię do  CEL/dzienne/
# (kopie starsze niż ZDALNIE_DNI dni są usuwane) i — 1. dnia miesiąca — także do  CEL/miesieczne/ (bez usuwania).
#
# Kod aplikacji odtwarza się z GitHuba, dane z tej kopii — zob. docs/ODTWARZANIE.md.
# Ustawienia (zmienne środowiskowe, wszystkie opcjonalne):
#   NAJEM_DANE    katalog danych instancji        (domyślnie /workspace/asystentnajmu-dane)
#   NAJEM_KOPIE   lokalny katalog kopii            (domyślnie /workspace/asystentnajmu-kopie)
#   RCLONE        ścieżka do rclone                (domyślnie /workspace/bin/rclone, potem rclone z PATH)
#   NAJEM_KOPIA_CEL  zdalny cel rclone             (domyślnie gdrive:asystentnajmu-kopie)
set -eu
DANE=${NAJEM_DANE:-/workspace/asystentnajmu-dane}
KOPIE=${NAJEM_KOPIE:-/workspace/asystentnajmu-kopie}
CEL=${NAJEM_KOPIA_CEL:-gdrive:asystentnajmu-kopie}
LOKALNIE_DNI=${LOKALNIE_DNI:-14}
ZDALNIE_DNI=${ZDALNIE_DNI:-60}
RCLONE=${RCLONE:-/workspace/bin/rclone}
[ -x "$RCLONE" ] || RCLONE=$(command -v rclone || true)

umask 077
mkdir -p "$KOPIE"
LOG="$KOPIE/kopia.log"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG"; }

[ -f "$DANE/najem.sqlite3" ] || { log "BŁĄD: brak bazy $DANE/najem.sqlite3"; exit 1; }

znacznik=$(date +%Y-%m-%d_%H%M)
tmp=$(mktemp -d "$KOPIE/.tmp.XXXXXX")
trap 'rm -rf "$tmp"' EXIT

# 1. spójna kopia bazy (działa także, gdy aplikacja pisze do bazy) + kontrola integralności
sqlite3 "$DANE/najem.sqlite3" ".backup '$tmp/najem.sqlite3'"
wynik=$(sqlite3 "$tmp/najem.sqlite3" "PRAGMA integrity_check;")
[ "$wynik" = "ok" ] || { log "BŁĄD: kopia bazy nie przeszła integrity_check: $wynik"; exit 1; }

# 2. ustawienia instancji i zdjęcia
for f in secret_key nazwa_serwisu motyw; do [ -f "$DANE/$f" ] && cp -p "$DANE/$f" "$tmp/"; done
[ -d "$DANE/zdjecia" ] && cp -rp "$DANE/zdjecia" "$tmp/zdjecia"

plik="$KOPIE/asystentnajmu-$znacznik.tar.gz"
tar -czf "$plik.czesc" -C "$tmp" . && mv "$plik.czesc" "$plik"
log "kopia lokalna: $plik ($(du -h "$plik" | cut -f1))"

# 3. rotacja lokalna
find "$KOPIE" -maxdepth 1 -name 'asystentnajmu-*.tar.gz' -mtime +"$LOKALNIE_DNI" -print -delete | sed 's/^/usunięto lokalnie: /' >> "$LOG"

# 4. wysyłka na dysk zdalny (Google Drive przez rclone), jeśli skonfigurowany
zdalny=${CEL%%:*}
if [ -n "$RCLONE" ] && "$RCLONE" listremotes 2>/dev/null | grep -qx "$zdalny:"; then
    "$RCLONE" copy "$plik" "$CEL/dzienne/" && log "wysłano: $CEL/dzienne/$(basename "$plik")"
    if [ "$(date +%d)" = "01" ]; then
        "$RCLONE" copy "$plik" "$CEL/miesieczne/" && log "wysłano: $CEL/miesieczne/$(basename "$plik")"
    fi
    "$RCLONE" delete --min-age "${ZDALNIE_DNI}d" "$CEL/dzienne/" 2>>"$LOG" || log "uwaga: nie udało się usunąć starych kopii zdalnych"
else
    log "uwaga: rclone / dysk '$zdalny:' nieskonfigurowany — kopia tylko lokalna"
fi
