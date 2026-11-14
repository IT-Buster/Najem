# Najem

Aplikacja webowa do obsługi najmu mieszkań: lokale, umowy, najemcy oraz miesięczne rozliczenia
czynszu i mediów z zawiadomieniami o wysokości opłat.

Flask + SQLite, bez frameworków front-endowych; działa na telefonie i drukuje kwity.

## Funkcje

**Panel zarządcy**
- **Budynki** — lokale, faktury za media i liczniki główne należą do budynku; budynek może mieć własne stawki.
- **Lokale, umowy (z aneksami), klienci** — numeracja umów i podpowiedzi przy wpisywaniu.
- **Odczyty liczników** — ciepłomierze i wodomierze lokali oraz liczniki główne budynku, kontrola sumy podliczników.
- **Faktury mediów** — woda (z odliczeniem wody ogrodowej) i ciepło (z opłatami stałymi).
- **Rozliczenia** — zawiadomienia o wysokości opłat: podgląd, wystawianie, cofanie, druk (oryginał + kopia na kartce A4).
- **Wpłaty i salda** (+ niedopłata, − nadpłata).

**Panel najemcy** — moje lokale i umowy, płatności (zawiadomienia, wpłaty, saldo), liczniki i zużycie.

## Zasady rozliczania mediów

Zawiadomienie za czynsz z miesiąca M rozlicza media z miesiąca M−1.

- **Zużycie lokalu** = odczyt z początku M − odczyt z początku M−1.
- **Współczynnik** (co miesiąc, osobno dla wody i ciepła) = zużycie do podziału z faktury ÷ suma zużyć
  wszystkich podliczników budynku. Podliczniki mają błąd pomiaru — dzięki współczynnikowi suma obciążeń
  lokali równa się fakturze co do grosza (reszty z zaokrągleń rozdzielane metodą największych reszt).
- **Pustostany** dostają swój udział w fakturze, którego nikt nie płaci — pokrywa go właściciel.
- **Woda ogrodowa** (pole „odliczenie” na fakturze za wodę) — płaci właściciel.
- **Opłata przesyłowa + moc cieplna** = opłaty stałe z faktury za ciepło ÷ liczba lokali, co miesiąc przez cały rok.
- **Światło i wywóz nieczystości** — stawki stałe (śmieci wg liczby osób w umowie).
- **Saldo** = suma wcześniejszych zawiadomień − suma wpłat.
- Umowa z kwotą w polu „Opłaty / zaliczki” dostaje ją jako ryczałt zamiast rozliczenia liczników.

## Uruchomienie

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# baza demo z fikcyjnymi danymi (jest też gotowa w demo/najem-demo.sqlite3)
.venv/bin/python seed_demo.py /tmp/najem-demo.sqlite3

# lokalnie, bez HTTPS:
NAJEM_DB=demo/najem-demo.sqlite3 NAJEM_INSECURE_COOKIES=1 .venv/bin/flask --app app run
```

Konta demo (hasło `demo1234`): `admin` (zarządca), `jan.kowalski`, `marta.zawadzka`, `piotr.sikorski`, `ewa.zielinska`.

Własna, pusta baza: `.venv/bin/flask --app app init-db`, a potem `.venv/bin/flask --app app create-user`
(rola `zarzadca`). Domyślnie baza leży w `instance/najem.sqlite3`.

### Konfiguracja

| Zmienna / plik | Znaczenie |
|---|---|
| `NAJEM_DB` | ścieżka do bazy SQLite (domyślnie `instance/najem.sqlite3`) |
| `NAJEM_INSECURE_COOKIES=1` | ciasteczka sesji bez flagi `Secure` — tylko do pracy lokalnej bez HTTPS |
| `NAJEM_NAZWA` lub plik `instance/nazwa_serwisu` | nazwa serwisu w tytułach stron (np. domena) |
| `instance/secret_key` | klucz sesji — tworzony automatycznie przy pierwszym uruchomieniu |

Produkcyjnie: `gunicorn -w 2 -b 0.0.0.0:8000 app:app` za reverse proxy z HTTPS (zob. `start.sh`).

## Dane

Repozytorium zawiera wyłącznie kod i bazę demo z **fikcyjnymi** danymi. Katalog `instance/`
(prawdziwa baza, klucz sesji) jest wykluczony w `.gitignore`.
