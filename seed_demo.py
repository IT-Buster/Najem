"""Tworzy bazę demo z danymi FIKCYJNYMI: budynek, lokale, najemcy, umowy, odczyty liczników,
faktury za media, zawiadomienia i wpłaty z ostatniego roku.

Użycie:
    python seed_demo.py                      # → demo/najem-demo.sqlite3
    python seed_demo.py ścieżka/do/bazy.sqlite3
    NAJEM_DB=demo/najem-demo.sqlite3 gunicorn app:app   # uruchomienie na bazie demo

Konta (hasło dla wszystkich: demo1234): admin (zarządca), jan.kowalski, marta.zawadzka,
piotr.sikorski, ewa.zielinska (najemcy).
"""
import os
import random
import sys
from datetime import date
from pathlib import Path

cel = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / "demo" / "najem-demo.sqlite3").resolve()
if cel.exists():
    raise SystemExit(f"{cel} już istnieje — usuń plik, aby utworzyć bazę od nowa.")
cel.parent.mkdir(parents=True, exist_ok=True)
os.environ["NAJEM_DB"] = str(cel)

import sqlite3  # noqa: E402

from app import app, create_user, init_db  # noqa: E402
import rozliczenia as R  # noqa: E402

random.seed(2026)
HASLO = "demo1234"
init_db()
create_user("admin", HASLO, "Administrator Demo", "zarzadca")
for login, imie in [("jan.kowalski", "Jan Kowalski"), ("marta.zawadzka", "Marta Zawadzka"),
                    ("piotr.sikorski", "Piotr Sikorski"), ("ewa.zielinska", "Ewa Zielińska")]:
    create_user(login, HASLO, imie, "klient")

db = sqlite3.connect(cel)
uid = {r[0]: r[1] for r in db.execute("SELECT login, id FROM users")}


def ins(table, **k):
    return db.execute(f"INSERT INTO {table} ({','.join(k)}) VALUES ({','.join('?' * len(k))})", list(k.values())).lastrowid


# ---------- ustawienia i budynek ----------
for k, v in {"swiatlo": "500", "smieci_1": "3500", "rachunek": "PL00 1234 5678 9000 0000 0000 0000 (fikcyjny)"}.items():
    db.execute("INSERT OR REPLACE INTO ustawienia (klucz, wartosc) VALUES (?,?)", (k, v))
bid = ins("budynki", nazwa="Lipowa 5", ulica="ul. Lipowa 5", kod="00-950", miasto="Przykładowo", lokale_cieplo=4,
          uwagi="Budynek przykładowy — dane fikcyjne.")

LOKALE = [  # nr, powierzchnia, pokoje, piętro, średnie zużycie wody m³/mies., udział w cieple
    ("1", 52.4, 2, "parter", 4.2, 1.0), ("2", 38.0, 1, "parter", 2.6, 0.8),
    ("3", 61.7, 3, "1 piętro", 6.1, 1.3), ("4", 29.5, 1, "1 piętro", 0.0, 0.25)]   # 4 = pustostan
lok = {}
for nr, pw, pk, pt, _, _ in LOKALE:
    lok[nr] = ins("lokale", nazwa=f"Lipowa 5/{nr}", ulica="ul. Lipowa 5", nr_lokalu=nr, kod="00-950", miasto="Przykładowo",
                  rodzaj="mieszkanie" if pk > 1 else "kawalerka", powierzchnia=pw, pokoje=pk, pietro=pt,
                  nr_wodomierza=f"W-{1000 + int(nr)}", nr_cieplomierza=f"C-{2000 + int(nr)}", budynek_id=bid,
                  wyposazenie="Lodówka\nPralka\nKuchenka indukcyjna\nSzafa")
kaw = ins("lokale", nazwa="Kawalerka Centrum", ulica="ul. Słoneczna 12", nr_lokalu="8", kod="00-951", miasto="Przykładowo",
          rodzaj="kawalerka", powierzchnia=27.0, pokoje=1, pietro="3 z 4", opis="Lokal z ryczałtem za media (bez budynku).")

# ---------- umowy ----------
dzis = date.today()
biez = dzis.strftime("%Y-%m")
start = R.przesun(biez, -12)                       # rok historii
umowy = {
    "1": ins("umowy", numer="NZ/2024/001", lokal_id=lok["1"], najemca_id=uid["jan.kowalski"], rodzaj="najem zwykły",
             data_zawarcia="2024-03-20", data_od="2024-04-01", czynsz=150000, oplaty=0, kaucja=300000,
             dzien_platnosci=10, wypowiedzenie="1 miesiąc", liczba_osob=1),
    "2": ins("umowy", numer="NO/2025/001", lokal_id=lok["2"], najemca_id=uid["marta.zawadzka"], rodzaj="najem okazjonalny",
             data_zawarcia="2025-01-15", data_od="2025-02-01", data_do=R.przesun(biez, 14) + "-28", czynsz=180000,
             oplaty=0, kaucja=360000, dzien_platnosci=10, wypowiedzenie="3 miesiące", liczba_osob=2),
    "3": ins("umowy", numer="NZ/2023/002", lokal_id=lok["3"], najemca_id=uid["piotr.sikorski"], rodzaj="najem zwykły",
             data_zawarcia="2023-08-25", data_od="2023-09-01", czynsz=120000, oplaty=0, kaucja=240000,
             dzien_platnosci=5, wypowiedzenie="1 miesiąc", liczba_osob=3),
}
ins("aneksy", umowa_id=umowy["3"], numer="Aneks nr 1", data="2025-08-20", opis="Waloryzacja czynszu z 1 150,00 zł na 1 200,00 zł.")
ins("umowy", numer="NZ/2025/002", lokal_id=kaw, najemca_id=uid["ewa.zielinska"], rodzaj="najem zwykły",
    data_zawarcia="2025-05-10", data_od="2025-06-01", czynsz=140000, oplaty=35000, kaucja=140000,
    dzien_platnosci=10, wypowiedzenie="1 miesiąc", liczba_osob=1, uwagi="Media w ryczałcie.")

# ---------- odczyty i faktury ----------
GJ_MIES = [3.2, 2.9, 2.2, 1.2, 0.3, 0, 0, 0, 0.2, 1.1, 2.0, 2.8]   # sezon grzewczy (styczeń…grudzień)
stan = {nr: [10.0 + i * 3, 100.0 + i * 50] for i, (nr, *_r) in enumerate(LOKALE)}   # [ciepło GJ, woda m³]
glowny = [500.0, 1000.0]
okres = start
while okres <= biez:
    for nr, *_r in LOKALE:
        ins("odczyty", lokal_id=lok[nr], okres=okres, data=okres + "-01",
            cieplo=round(stan[nr][0], 3), woda=round(stan[nr][1], 2))
    ins("odczyty", lokal_id=-bid, okres=okres, data=okres + "-01", cieplo=round(glowny[0], 3), woda=round(glowny[1], 2))
    if okres == biez:
        break
    m = int(okres[5:]) - 1
    sum_c = sum_w = 0
    for nr, _, _, _, woda, udz in LOKALE:
        c = round(GJ_MIES[m] * udz * random.uniform(0.85, 1.15), 3)
        w = round(woda * random.uniform(0.8, 1.2), 2) if woda else 0.0
        stan[nr][0] += c
        stan[nr][1] += w
        sum_c += c
        sum_w += w
    fc, fw = round(sum_c * random.uniform(0.95, 1.06), 2), round(sum_w * random.uniform(0.96, 1.08) + 0.4, 1)
    glowny[0] += fc
    glowny[1] += fw
    ogrod = round(random.uniform(5, 15), 2) if m in (5, 6, 7) else 0       # woda ogrodowa latem
    ins("faktury", budynek_id=bid, rodzaj="woda", numer=f"WOD/{okres.replace('-', '/')}", okres=okres,
        data=R.nastepny(okres) + "-05", zuzycie=fw, kwota=round((fw * 10.45 + ogrod) * 100), odliczenie=round(ogrod * 100))
    ins("faktury", budynek_id=bid, rodzaj="cieplo", numer=f"CIE/{okres.replace('-', '/')}", okres=okres,
        data=R.nastepny(okres) + "-08", zuzycie=fc, kwota=round((fc * 112.30 + 128.40) * 100), oplata_stala=12840)
    okres = R.nastepny(okres)
db.commit()
db.close()

# ---------- zawiadomienia i wpłaty (ostatnie 6 miesięcy) ----------
with app.test_request_context():
    baza = R.get_db()
    okres = R.przesun(biez, -5)
    while okres <= biez:
        for u in R.umowy_w_okresie(okres):
            nid = R.wystaw(u, okres)
            if okres == biez:
                continue                                   # bieżący miesiąc jeszcze nieopłacony
            n = baza.execute("SELECT * FROM naliczenia WHERE id=?", (nid,)).fetchone()
            kw = n["do_zaplaty"]
            if u["numer"] == "NZ/2023/002" and okres == R.przesun(biez, -1):
                kw -= 20000                                # jedna niedopłata 200 zł
            if kw > 0:
                baza.execute("INSERT INTO wplaty (umowa_id, data, kwota, opis) VALUES (?,?,?,?)",
                             (u["id"], R.termin(u, okres), kw, "przelew"))
        baza.commit()
        okres = R.nastepny(okres)

print(f"Gotowe: {cel}\nKonta: admin / jan.kowalski / marta.zawadzka / piotr.sikorski / ewa.zielinska, hasło: {HASLO}")
