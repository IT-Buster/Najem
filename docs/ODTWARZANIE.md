# Kopie zapasowe i odtworzenie systemu

System = **kod** (to repozytorium na GitHubie) + **dane** instancji z prawdziwymi danymi
(baza, zdjęcia, ustawienia instancji — w kopii zapasowej na Google Drive). Danych nie ma w repozytorium.

## Co jest w kopii

Plik `asystentnajmu-RRRR-MM-DD_GGMM.tar.gz`:

| plik | co to |
|---|---|
| `najem.sqlite3` | baza (spójna kopia `sqlite3 .backup`, sprawdzona `integrity_check`) |
| `zdjecia/` | zdjęcia liczników i zdjęcia z wiadomości / usterek |
| `secret_key` | klucz sesji (po odtworzeniu użytkownicy pozostają zalogowani) |
| `nazwa_serwisu`, `motyw` | nazwa w tytułach stron i kolor instancji |

Kopię robi `kopia.sh` codziennie po 3:00 (harmonogram `kopia-harmonogram.sh` uruchamia `autostart.sh`):
lokalnie w `/workspace/asystentnajmu-kopie/` (ostatnie 14 dni) i — gdy skonfigurowany jest rclone —
na Google Drive: `asystentnajmu-kopie/dzienne/` (ostatnie 60 dni) i `asystentnajmu-kopie/miesieczne/`
(kopia z 1. dnia każdego miesiąca, bez usuwania). Log: `/workspace/asystentnajmu-kopie/kopia.log`.

Kopia ręcznie: `sh /workspace/najem/kopia.sh`

## Konfiguracja Google Drive (jednorazowo)

1. rclone w `/workspace/bin/rclone` (albo w `PATH`): https://rclone.org/downloads/ (Linux, amd64).
2. W terminalu kontenera: `/workspace/bin/rclone config`
   - `n` (nowy), nazwa: **`gdrive`**, typ: **Google Drive**, `client_id` / `client_secret`: puste,
   - scope: **`drive.file`** — rclone widzi tylko pliki, które sam utworzył (nie resztę Twojego dysku),
   - *Use web browser to automatically authenticate?* → **n** (kontener nie ma przeglądarki);
     rclone poda polecenie `rclone authorize "drive" "…"` — uruchom je na komputerze z przeglądarką
     (rclone dla Windows/macOS), zaloguj się kontem Google i wklej wynik z powrotem do kontenera.
3. Test: `sh /workspace/najem/kopia.sh` — w logu powinno być `wysłano: gdrive:asystentnajmu-kopie/dzienne/…`.

Plik konfiguracji rclone (`~/.config/rclone/rclone.conf`) zawiera token dostępu do Drive — nie kopiuj go do repozytorium.

## Odtworzenie od zera (nowy kontener / serwer)

```sh
# 1. kod
cd /workspace
git clone https://github.com/IT-Buster/Najem.git najem
python3 -m venv /workspace/.venv
/workspace/.venv/bin/pip install -r najem/requirements.txt

# 2. dane — najnowsza kopia z Google Drive (albo pobrana ręcznie z drive.google.com)
/workspace/bin/rclone copy gdrive:asystentnajmu-kopie/dzienne/ /tmp/kopie/ --max-age 3d
mkdir -p /workspace/asystentnajmu-dane && chmod 700 /workspace/asystentnajmu-dane
tar -xzf "$(ls -t /tmp/kopie/asystentnajmu-*.tar.gz | head -1)" -C /workspace/asystentnajmu-dane

# 3. start (obie instancje + harmonogram kopii)
sh /workspace/najem/autostart.sh
```

4. Proxy (Nginx Proxy Manager): `asystentnajmu.bk.waw.pl` → host kontenera, port **8001**, SSL Let's Encrypt,
   Force SSL; `najem.bk.waw.pl` (demo) → port **8000**.
5. Sprawdzenie: `curl -H 'Host: asystentnajmu.bk.waw.pl' http://127.0.0.1:8001/zdrowie` → `ok`, potem logowanie.

Instancja demo (`najem.bk.waw.pl`) nie wymaga kopii — przy pustym `instance/` aplikacja założy nową bazę
(dane demo do zrzutów: `demo/najem-demo.sqlite3`, `seed_demo.py`).
