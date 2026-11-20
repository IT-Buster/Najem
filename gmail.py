"""Faktury mediów z Gmaila (IMAP): MWiO (woda) i OPEC (ciepło).

Dostawcy przysyłają e-mail z numerem i kwotą brutto w treści:
  MWiO:  „… została wysłana faktura o numerze RS/100931/10/2026 na kwotę 127,55 zł.”
  OPEC:  „W dniu 04-11-2026 została wystawiona faktura numer FVc/BOK/26/10/08945 o wartości brutto 884,26.”
Miesiąc zużycia wynika z numeru (…/10/2026, …/26/10/…); gdy się nie da — miesiąc przed datą e-maila.

Dostęp: adres Gmail + hasło do aplikacji Google (Konto Google → Bezpieczeństwo → Hasła do aplikacji;
wymaga weryfikacji dwuetapowej). Zapisywane w pliku gmail.json w katalogu instancji (chmod 600) —
nie w bazie, nie w kopii zapasowej i nie w repozytorium. Dostęp tylko do odczytu (EXAMINE).
"""
import email
import imaplib
import json
import os
import re
from datetime import date, timedelta
from decimal import Decimal
from email.policy import default as POLITYKA
from email.utils import parsedate_to_datetime
from html import unescape

from app import INSTANCE

PLIK = INSTANCE / "gmail.json"
SERWER = "imap.gmail.com"

DOSTAWCY = {
    "woda": {"nazwa": "MWiO", "nadawca": "faktury@mwio.pl",
             "wzor": re.compile(r"faktur\w*\s+o\s+numerze\s+(\S+?)\s+na\s+kwot\w*\s+([\d\s .]*\d,\d{2})", re.I)},
    "cieplo": {"nazwa": "OPEC", "nadawca": "bok@opec.pl",
               "wzor": re.compile(r"faktur\w*\s+numer\s+(\S+?)\s+o\s+warto\w+\s+brutto\s+([\d\s .]*\d,\d{2})", re.I)},
}


class BladGmail(Exception):
    pass


# ---------- dane logowania ----------
def dane():
    try:
        d = json.loads(PLIK.read_text())
        return d if d.get("login") and d.get("haslo") else None
    except (OSError, ValueError):
        return None


def zapisz_dane(login, haslo):
    PLIK.parent.mkdir(parents=True, exist_ok=True)
    tmp = PLIK.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"login": login, "haslo": haslo}, f)
    os.replace(tmp, PLIK)


def usun_dane():
    PLIK.unlink(missing_ok=True)


# ---------- parsowanie ----------
def kwota_gr(txt):
    return int(Decimal(re.sub(r"[\s .]", "", txt).replace(",", ".")) * 100)


def okres_z_numeru(rodzaj, numer):
    czesci = re.split(r"[/_\-]", numer)
    try:
        if rodzaj == "woda":                       # RS/100931/10/2026
            m, r = int(czesci[-2]), int(czesci[-1])
        else:                                      # FVc/BOK/26/10/08945
            r, m = 2000 + int(czesci[2]), int(czesci[3])
        if 1 <= m <= 12 and 2000 <= r <= 2100:
            return f"{r}-{m:02d}"
    except (IndexError, ValueError):
        pass
    return None


def tekst(msg):
    czesc = msg.get_body(preferencelist=("plain", "html"))
    if czesc is None:
        return ""
    t = czesc.get_content()
    if czesc.get_content_type() == "text/html":
        t = unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"\s+", " ", t)


def parsuj(rodzaj, msg):
    """Słownik faktury z e-maila albo None (np. wezwanie do zapłaty, inny e-mail)."""
    m = DOSTAWCY[rodzaj]["wzor"].search(tekst(msg))
    if not m:
        return None
    numer = m.group(1).rstrip(".,;")
    try:
        d = parsedate_to_datetime(msg["Date"]).astimezone().date()
    except (TypeError, ValueError):
        d = date.today()
    okres = okres_z_numeru(rodzaj, numer) or (d.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    return {"rodzaj": rodzaj, "numer": numer, "kwota": kwota_gr(m.group(2)), "okres": okres,
            "data": d.isoformat(), "dostawca": DOSTAWCY[rodzaj]["nazwa"], "temat": str(msg["Subject"] or "")}


def klucz_numeru(numer):
    """Numery bywały zapisywane z / albo _ (z nazwy pliku PDF) — porównujemy same litery i cyfry."""
    return re.sub(r"[^0-9A-Za-z]", "", numer or "").upper()


# ---------- IMAP ----------
def _wszystkie(imap):
    """Folder „Wszystkie” Gmaila (atrybut \\All) — e-maile z etykietami są poza Odebranymi."""
    typ, lista = imap.list()
    for linia in lista or []:
        s = linia.decode(errors="replace")
        if "\\All" in s:
            m = re.search(r'"([^"]+)"\s*$', s) or re.search(r"(\S+)\s*$", s)
            if m:
                return m.group(1)
    return "INBOX"


def polacz(login, haslo, timeout=20):
    try:
        imap = imaplib.IMAP4_SSL(SERWER, timeout=timeout)
        imap.login(login, haslo)
        return imap
    except imaplib.IMAP4.error:
        raise BladGmail("Gmail odrzucił logowanie — sprawdź adres i hasło do aplikacji.")
    except OSError as e:
        raise BladGmail(f"Brak połączenia z Gmailem ({e.__class__.__name__}).")


def szukaj(od_dnia, login=None, haslo=None, imap=None):
    """Faktury MWiO i OPEC z e-maili od dnia od_dnia (date). Lista słowników z parsuj()."""
    if imap is None:
        d = {"login": login, "haslo": haslo} if login else dane()
        if not d:
            raise BladGmail("Skrzynka Gmail nie jest skonfigurowana.")
        imap = polacz(d["login"], d["haslo"])
    try:
        imap.select('"' + _wszystkie(imap) + '"', readonly=True)
        wynik = []
        mies = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        od = f"{od_dnia.day:02d}-{mies[od_dnia.month - 1]}-{od_dnia.year}"     # format IMAP, niezależny od locale
        for rodzaj, dost in DOSTAWCY.items():
            typ, ids = imap.search(None, "FROM", f'"{dost["nadawca"]}"', "SINCE", od)
            for i in (ids[0].split() if ids and ids[0] else [])[-30:]:
                typ, dane_ = imap.fetch(i, "(BODY.PEEK[])")
                surowe = next((c[1] for c in dane_ if isinstance(c, tuple)), None)
                if not surowe:
                    continue
                f = parsuj(rodzaj, email.message_from_bytes(surowe, policy=POLITYKA))
                if f:
                    wynik.append(f)
        # ten sam numer w kilku e-mailach (przypomnienia) — zostaw najnowszy
        uniq = {}
        for f in sorted(wynik, key=lambda f: f["data"]):
            uniq[(f["rodzaj"], klucz_numeru(f["numer"]))] = f
        return sorted(uniq.values(), key=lambda f: (f["okres"], f["rodzaj"]), reverse=True)
    except imaplib.IMAP4.error as e:
        raise BladGmail(f"Błąd odczytu skrzynki: {e}")
    finally:
        try:
            imap.logout()
        except Exception:
            pass
