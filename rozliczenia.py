"""Rozliczenia mediów, wpłaty i zawiadomienia o wysokości opłat (wzór: arkusz „Najem”).

Okresy zapisujemy jako 'RRRR-MM'. Zawiadomienie za czynsz z miesiąca M rozlicza media z M-1:
- zużycie lokalu  = odczyt z początku M − odczyt z początku M-1,
- cena wody       = kwota faktury / m³ z faktury; woda ogrodowa (pole „odliczenie”) płaci właściciel,
                    więc na lokale dzielona jest kwota faktury − woda ogrodowa,
- cena ciepła     = (kwota faktury − opłaty stałe) / GJ z faktury,
- współczynnik    = zużycie do podziału / suma zużyć wszystkich podliczników (liczony co miesiąc,
                    osobno dla wody i ciepła) — podliczniki mają błąd pomiaru, a suma obciążeń
                    lokali ma się równać fakturze. Udział pustostanów pokrywa właściciel.
- przesył + moc   = opłaty stałe z faktury ciepła / liczba lokali ogrzewanych,
- saldo           = suma wcześniejszych zawiadomień − suma wpłat (+ niedopłata, − nadpłata).
Umowa z ryczałtem (pole „Opłaty” > 0) dostaje jedną pozycję ryczałtu zamiast mediów.
"""
import re
from calendar import monthrange
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from flask import abort, flash, g, redirect, render_template, request, url_for

from admin import UMOWY_SQL, Form, _get_or_404, _insert, _update, bp, zl_input
from app import app, check_csrf, fmt_data, get_db, login_required

# ---------- okresy ----------
MIESIACE_M = ["styczeń", "luty", "marzec", "kwiecień", "maj", "czerwiec", "lipiec",
              "sierpień", "wrzesień", "październik", "listopad", "grudzień"]
OKRES_RE = re.compile(r"\d{4}-(0[1-9]|1[0-2])")


def biezacy():
    return date.today().strftime("%Y-%m")


def przesun(okres, n):
    y, m = map(int, okres.split("-"))
    i = y * 12 + (m - 1) + n
    return f"{i // 12}-{i % 12 + 1:02d}"


def poprzedni(okres):
    return przesun(okres, -1)


def nastepny(okres):
    return przesun(okres, 1)


def granice(okres):
    y, m = map(int, okres.split("-"))
    return date(y, m, 1).isoformat(), date(y, m, monthrange(y, m)[1]).isoformat()


def okres_z_args(name="okres"):
    o = request.values.get(name, "")
    return o if OKRES_RE.fullmatch(o) else biezacy()


@app.template_filter("okres")
def fmt_okres(o):
    if not o:
        return "—"
    return f"{MIESIACE_M[int(o[5:7]) - 1]} {o[:4]}"


def krotko(o):
    """Format z arkusza: 2026 / 10."""
    return f"{o[:4]} / {o[5:7]}"


app.jinja_env.globals.update(poprzedni=poprzedni, nastepny=nastepny, krotko=krotko)


@app.context_processor
def _ctx_dzis():
    return {"today": date.today().isoformat()}


@app.template_filter("umowa_opcja")
def umowa_opcja(u):
    return (u["id"], f"{u['lokal_nazwa']} · {u['numer']} · {u['najemca_nazwa'] or u['najemca_login']}")


# ---------- liczby ----------
def grosze(zl):
    return int((Decimal(zl) * 100).quantize(Decimal("1"), ROUND_HALF_UP))


def liczba(x, miejsc=2):
    if x is None:
        return "—"
    s = f"{Decimal(x).quantize(Decimal(1).scaleb(-miejsc), ROUND_HALF_UP):,.{miejsc}f}"
    return s.replace(",", " ").replace(".", ",")


@app.template_filter("liczba")
def fmt_liczba(x, miejsc=2):
    return liczba(x, miejsc)


def zl_txt(zl):
    return liczba(zl, 2) + " zł"


def dec(x):
    return None if x is None else Decimal(str(x))


# ---------- ustawienia ----------
# klucz, etykieta, wartość domyślna (zapisana), podpowiedź, typ
USTAWIENIA = [
    ("swiatlo", "Światło – klatka schodowa, podwórze", "500", "Kwota miesięcznie na lokal. Puste = nie naliczaj.", "money"),
    ("smieci_1", "Wywóz nieczystości – 1 osoba", "4200", "Kwota miesięcznie", "money"),
    ("smieci_2", "Wywóz nieczystości – 2 osoby", "", "Puste = stawka za 1 osobę × 2", "money"),
    ("smieci_3", "Wywóz nieczystości – 3 osoby", "", "Puste = stawka za 1 osobę × 3", "money"),
    ("smieci_4", "Wywóz nieczystości – 4 osoby", "", "Puste = stawka za 1 osobę × 4", "money"),
    ("smieci_5", "Wywóz nieczystości – 5 i więcej osób", "", "Puste = stawka za 1 osobę × liczba osób", "money"),
    ("rachunek", "Rachunek bankowy", "", "Drukowany na zawiadomieniu", "text"),
]


def ust():
    zapisane = {r["klucz"]: r["wartosc"] for r in get_db().execute("SELECT * FROM ustawienia")}
    out = {}
    for k, _, dom, _, typ in USTAWIENIA:
        v = zapisane.get(k, dom)
        if v in (None, ""):
            out[k] = None
        elif typ in ("money", "int"):
            out[k] = int(v)
        elif typ == "number":
            out[k] = Decimal(v)
        else:
            out[k] = v
    return out


STAWKI_BUDYNKU = ["swiatlo", "smieci_1", "smieci_2", "smieci_3", "smieci_4", "smieci_5", "rachunek"]


def budynek(bid):
    return None if bid is None else get_db().execute("SELECT * FROM budynki WHERE id=?", (bid,)).fetchone()


def stawki(b):
    """Stawki budynku; puste pola budynku = wartości domyślne z ustawień."""
    s = ust()
    if b is not None:
        for k in STAWKI_BUDYNKU:
            if b[k] not in (None, ""):
                s[k] = b[k]
    return s


def budynek_lokalu(lokal_id):
    r = get_db().execute("SELECT budynek_id FROM lokale WHERE id=?", (lokal_id,)).fetchone()
    return budynek(r[0]) if r else None


def rachunek_umowy(u):
    return stawki(budynek_lokalu(u["lokal_id"]))["rachunek"]


def glowny_id(bid):
    """Liczniki główne budynku zapisujemy w odczytach jako lokal_id = −id budynku."""
    return -bid


# ---------- dane ----------
def odczyt(lokal_id, okres):
    return get_db().execute("SELECT * FROM odczyty WHERE lokal_id=? AND okres=?", (lokal_id, okres)).fetchone()


def wymiana(lokal_id, rodzaj, okres_media):
    return get_db().execute("SELECT * FROM wymiany WHERE lokal_id=? AND rodzaj=? AND okres=?",
                            (lokal_id, rodzaj, okres_media)).fetchone()


def zuzycie(lokal_id, okres_media):
    """Zużycie w miesiącu okres_media: odczyt z początku następnego miesiąca − odczyt z początku tego.
    Przy wymianie licznika w tym miesiącu: (stan końcowy starego − odczyt początkowy)
    + (odczyt końcowy − stan początkowy nowego)."""
    a, b = odczyt(lokal_id, okres_media), odczyt(lokal_id, nastepny(okres_media))
    out = {}
    for k in ("cieplo", "woda"):
        if not (a and b and a[k] is not None and b[k] is not None):
            out[k] = None
            continue
        w = wymiana(lokal_id, k, okres_media)
        if w:
            out[k] = (dec(w["stan_stary"]) - dec(a[k])) + (dec(b[k]) - dec(w["stan_nowy"]))
        else:
            out[k] = dec(b[k]) - dec(a[k])
    return out


def faktura(rodzaj, okres, bid, ze_zuzyciem=True):
    """Faktura budynku za dany miesiąc albo — gdy jej brak — ostatnia wcześniejsza (jak w arkuszu).
    ze_zuzyciem: tylko faktury z niezerowym zużyciem (do ceny jednostkowej)."""
    return get_db().execute(
        "SELECT * FROM faktury WHERE rodzaj=? AND okres<=? AND budynek_id=?" + (" AND zuzycie>0" if ze_zuzyciem else "")
        + " ORDER BY okres DESC, id DESC LIMIT 1", (rodzaj, okres, bid)).fetchone()


def cena_jedn(f):
    """Cena za m³ / GJ z faktury. Woda: pełna cena (kwota / m³). Ciepło: bez opłat stałych."""
    if f is None or not f["zuzycie"]:
        return None
    kw = f["kwota"] - (f["oplata_stala"] if f["rodzaj"] == "cieplo" else 0)
    return Decimal(kw) / 100 / dec(f["zuzycie"])


def netto_lokale(f):
    """Część faktury dzielona na lokale: woda bez wody ogrodowej (płaci właściciel), ciepło bez opłat stałych."""
    return f["kwota"] - (f["odliczenie"] if f["rodzaj"] == "woda" else f["oplata_stala"])


def lokale_cieplo(b):
    """Na ile lokali dzielić opłatę przesyłową budynku: ustawienie budynku albo lokale z ciepłomierzem."""
    if b["lokale_cieplo"]:
        return b["lokale_cieplo"]
    n = get_db().execute("SELECT count(*) FROM lokale WHERE budynek_id=? AND coalesce(nr_cieplomierza,'')<>''",
                         (b["id"],)).fetchone()[0]
    return n or 1


def ma_cieplo(lokal):
    if lokal["nr_cieplomierza"]:
        return True
    return get_db().execute("SELECT 1 FROM odczyty WHERE lokal_id=? AND cieplo IS NOT NULL",
                            (lokal["id"],)).fetchone() is not None


def umowy_w_okresie(okres):
    od, do = granice(okres)
    return get_db().execute(
        UMOWY_SQL + " WHERE u.data_od<=? AND coalesce(u.rozwiazana_dnia, u.data_do, '9999-12-31')>=?"
        " ORDER BY l.nazwa", (do, od)).fetchall()


def saldo(umowa_id, przed_okresem=None):
    """+ niedopłata, − nadpłata. Liczone z zawiadomień wcześniejszych niż przed_okresem i wszystkich wpłat."""
    db = get_db()
    q, args = "SELECT coalesce(sum(suma_pozycji),0) FROM naliczenia WHERE umowa_id=?", [umowa_id]
    if przed_okresem:
        q += " AND okres_czynsz<?"
        args.append(przed_okresem)
    nal = db.execute(q, args).fetchone()[0]
    wpl = db.execute("SELECT coalesce(sum(kwota),0) FROM wplaty WHERE umowa_id=?", (umowa_id,)).fetchone()[0]
    return nal - wpl


def termin(u, okres):
    y, m = map(int, okres.split("-"))
    return date(y, m, min(u["dzien_platnosci"], monthrange(y, m)[1])).isoformat()


# ---------- podział faktury na lokale ----------
JEDN = {"woda": ("m³", 2), "cieplo": ("GJ", 3)}
NAZWA = {"woda": "wody", "cieplo": "ciepła"}


def rozdzial(rodzaj, om, bid):
    """Podział faktury budynku bid za media z miesiąca om na wszystkie lokale budynku z podlicznikami.

    Współczynnik = zużycie z faktury / suma zużyć podliczników, więc suma obciążeń = kwota faktury
    (grosze z zaokrągleń rozdzielane metodą największych reszt). Lokale bez umowy (pustostany)
    też dostają swój udział — nikt go nie płaci, czyli pokrywa go właściciel.
    Wynik w g (cache na czas żądania)."""
    cache = g.setdefault("_rozdzial", {})
    if (rodzaj, om, bid) in cache:
        return cache[(rodzaj, om, bid)]
    db = get_db()
    zu, bledne = {}, []
    for (lid,) in db.execute("""SELECT DISTINCT o.lokal_id FROM odczyty o JOIN lokale l ON l.id=o.lokal_id
                                WHERE l.budynek_id=? AND o.okres IN (?,?)""", (bid, om, nastepny(om))):
        z = zuzycie(lid, om)[rodzaj]
        if z is None or z < 0:
            bledne.append(lid)
        else:
            zu[lid] = z
    suma = sum(zu.values(), Decimal(0))
    fv = db.execute("SELECT * FROM faktury WHERE rodzaj=? AND okres=? AND budynek_id=? ORDER BY id DESC LIMIT 1",
                    (rodzaj, om, bid)).fetchone()
    wynik = {"zuzycie": zu, "suma": suma, "bledne": bledne, "faktura": fv, "wsp": None, "cena": None,
             "kwoty": {}, "uwagi": [], "zrodlo": None}
    jedn, miejsc = JEDN[rodzaj]
    if fv is not None:
        netto = netto_lokale(fv)
        if fv["zuzycie"] and suma > 0:
            wynik["cena"] = cena_jedn(fv)
            # m³/GJ do podziału = netto / cena (woda: zużycie z faktury minus woda ogrodowa)
            wynik["wsp"] = Decimal(netto) / 100 / wynik["cena"] / suma
            wynik["zrodlo"] = "faktura"
            # największe reszty: suma groszy = netto faktury
            dokl = {lid: Decimal(netto) * z / suma for lid, z in zu.items()}
            kw = {lid: int(v) for lid, v in dokl.items()}
            reszta = netto - sum(kw.values())
            for lid in sorted(dokl, key=lambda l: dokl[l] - kw[l], reverse=True)[:max(reszta, 0)]:
                kw[lid] += 1
            wynik["kwoty"] = kw
        else:
            wynik["kwoty"] = {lid: 0 for lid in zu}     # 0 na fakturze albo 0 na podlicznikach
            wynik["wsp"] = None
            wynik["zrodlo"] = "faktura"
    else:
        # brak faktury za ten miesiąc: cena z ostatniej faktury, współczynnik z licznika głównego
        fp = faktura(rodzaj, om, bid)
        glowny = zuzycie(glowny_id(bid), om)[rodzaj]
        if fp is not None and suma > 0:
            wynik["cena"] = cena_jedn(fp)
            if glowny is not None and glowny >= 0:
                wynik["wsp"] = glowny / suma
                wynik["zrodlo"] = "licznik główny"
            else:
                wynik["wsp"] = Decimal(1)
                wynik["zrodlo"] = "brak"
            wynik["kwoty"] = {lid: grosze(wynik["cena"] * z * wynik["wsp"]) for lid, z in zu.items()}
            wynik["uwagi"].append(
                f"Brak faktury za {NAZWA[rodzaj]} za {fmt_okres(om)}: cena z faktury {fp['numer']} za {fmt_okres(fp['okres'])}, "
                + ("współczynnik z licznika głównego." if wynik["zrodlo"] == "licznik główny"
                   else "bez współczynnika (brak odczytu licznika głównego)."))
        elif suma == 0 and zu:
            wynik["kwoty"] = {lid: 0 for lid in zu}
    if bledne and fv is not None:
        wynik["uwagi"].append(f"Brak lub błędne odczyty {NAZWA[rodzaj]} w {len(bledne)} lokalu(-ach) — "
                              "nie biorą udziału w podziale faktury.")
    cache[(rodzaj, om, bid)] = wynik
    return wynik


def media_lokalu(lokal, om):
    """Pozycje za wodę i ciepło dla lokalu (+ opłata przesyłowa) oraz uwagi."""
    poz, uwagi = [], []
    b = budynek(lokal["budynek_id"])
    if b is None:
        return [], ["Lokal nie jest przypisany do budynku — woda i ciepło pominięte. Przypisz budynek w danych lokalu."]
    z = zuzycie(lokal["id"], om)
    for rodzaj in ("woda", "cieplo"):
        if rodzaj == "cieplo" and not ma_cieplo(lokal):
            continue
        jedn, miejsc = JEDN[rodzaj]
        tytul = (f"Zużycie wody i odprowadzenie ścieków – {krotko(om)}" if rodzaj == "woda"
                 else f"Ogrzewanie – zużycie – {krotko(om)}")
        if z[rodzaj] is None:
            uwagi.append(f"Brak odczytów {NAZWA[rodzaj]} na {fmt_data(om + '-01')} lub {fmt_data(nastepny(om) + '-01')} — pozycja pominięta.")
            continue
        if z[rodzaj] < 0:
            uwagi.append(f"Ujemne zużycie {NAZWA[rodzaj]} — sprawdź odczyty. Pozycja pominięta.")
            continue
        r = rozdzial(rodzaj, om, b["id"])
        uwagi += r["uwagi"]
        if lokal["id"] not in r["kwoty"]:
            uwagi.append(f"Brak faktury za {NAZWA[rodzaj]} za {fmt_okres(om)} lub wcześniej — pozycja pominięta.")
            continue
        kw = r["kwoty"][lokal["id"]]
        if r["cena"] is None:
            sposob = f"{liczba(z[rodzaj], miejsc)} {jedn}" + (" (faktura 0)" if r["faktura"] and not r["faktura"]["zuzycie"] else "")
        else:
            sposob = f"{zl_txt(r['cena'])} × {liczba(z[rodzaj], miejsc)} {jedn}"
            if r["wsp"] is not None and r["wsp"] != 1:
                sposob += f" × wsp. {liczba(r['wsp'], 4)}"
        poz.append((tytul, sposob, kw))
    if ma_cieplo(lokal):
        fs = faktura("cieplo", om, b["id"], ze_zuzyciem=False)    # opłaty stałe (są też latem, przy 0 GJ)
        if fs is None:
            uwagi.append(f"Brak faktury za ciepło za {fmt_okres(om)} lub wcześniej — opłata przesyłowa pominięta.")
        else:
            if fs["okres"] != om:
                uwagi.append(f"Opłata przesyłowa z faktury {fs['numer']} za {fmt_okres(fs['okres'])} (brak faktury za {fmt_okres(om)}).")
            if fs["oplata_stala"]:
                nl = lokale_cieplo(b)
                poz.append(("Ogrzewanie – opłata przesyłowa + moc cieplna",
                            f"{zl_txt(Decimal(fs['oplata_stala']) / 100)} / {nl} lok.",
                            grosze(Decimal(fs["oplata_stala"]) / 100 / nl)))
    # te same uwagi z podziału mogą się powtórzyć
    return poz, list(dict.fromkeys(uwagi))


def oblicz(u, okres):
    """Pozycje zawiadomienia dla umowy u za czynsz z miesiąca okres (media z miesiąca poprzedniego)."""
    db, om = get_db(), poprzedni(okres)
    lokal = db.execute("SELECT * FROM lokale WHERE id=?", (u["lokal_id"],)).fetchone()
    s = stawki(budynek(lokal["budynek_id"]))
    poz, uwagi = [], []

    if u["oplaty"]:
        poz.append((f"Opłaty eksploatacyjne – {krotko(okres)}", "ryczałt wg umowy", u["oplaty"]))
    else:
        if s["swiatlo"]:
            poz.append(("Światło – klatka schodowa, podwórze", "", s["swiatlo"]))
        n = u["liczba_osob"] or 1
        st = s[f"smieci_{min(n, 5)}"]
        if st is None and s["smieci_1"] is not None:
            st = s["smieci_1"] * n
        if st:
            poz.append((f"Wywóz nieczystości – {krotko(om)}", f"{n} os.", st))

        poz_media, uw = media_lokalu(lokal, om)
        poz += poz_media
        uwagi += uw

    do = granice(okres)[1]
    aneks = db.execute("SELECT * FROM aneksy WHERE umowa_id=? AND data<=? ORDER BY data DESC LIMIT 1",
                       (u["id"], do)).fetchone()
    if aneks:
        d = date.fromisoformat(aneks["data"])
        sposob = f"Aneks z dnia {d:%d-%m-%Y}"
    else:
        sposob = f"Umowa {u['numer']}"
    poz.append((f"Czynsz – {krotko(okres)}", sposob, u["czynsz"]))

    suma = sum(p[2] for p in poz)
    sal = saldo(u["id"], okres)
    return {"pozycje": [{"lp": i + 1, "tytul": t, "sposob": sp, "kwota": k} for i, (t, sp, k) in enumerate(poz)],
            "uwagi": uwagi, "suma_pozycji": suma, "saldo": sal, "do_zaplaty": suma + sal,
            "okres_czynsz": okres, "okres_media": om, "termin": termin(u, okres)}


def wystaw(u, okres):
    r = oblicz(u, okres)
    db = get_db()
    nid = _insert("naliczenia", {
        "umowa_id": u["id"], "okres_czynsz": okres, "okres_media": r["okres_media"],
        "data_wystawienia": date.today().isoformat(), "termin": r["termin"],
        "suma_pozycji": r["suma_pozycji"], "saldo": r["saldo"], "do_zaplaty": r["do_zaplaty"]})
    for p in r["pozycje"]:
        db.execute("INSERT INTO naliczenia_pozycje (naliczenie_id, lp, tytul, sposob, kwota) VALUES (?,?,?,?,?)",
                   (nid, p["lp"], p["tytul"], p["sposob"], p["kwota"]))
    return nid


def zawiadomienie_dane(nid):
    db = get_db()
    n = db.execute("SELECT * FROM naliczenia WHERE id=?", (nid,)).fetchone()
    if n is None:
        abort(404)
    u = db.execute(UMOWY_SQL + " WHERE u.id=?", (n["umowa_id"],)).fetchone()
    poz = db.execute("SELECT * FROM naliczenia_pozycje WHERE naliczenie_id=? ORDER BY lp", (nid,)).fetchall()
    return n, u, poz


# =====================================================================
# PANEL ZARZĄDCY
# =====================================================================
def budynki_lista():
    return get_db().execute("SELECT * FROM budynki ORDER BY nazwa").fetchall()


def wybrany_budynek():
    """Budynek z parametru b (domyślnie pierwszy). None, gdy nie ma żadnego."""
    bl = budynki_lista()
    bid = request.values.get("b", type=int)
    for b in bl:
        if b["id"] == bid:
            return b, bl
    return (bl[0] if bl else None), bl


@bp.app_template_global()
def nazwa_budynku(bid):
    b = budynek(bid)
    return b["nazwa"] if b else None
@bp.get("/rozliczenia")
def rozliczenia():
    okres = okres_z_args()
    db = get_db()
    wiersze = []
    for u in umowy_w_okresie(okres):
        n = db.execute("SELECT * FROM naliczenia WHERE umowa_id=? AND okres_czynsz=?", (u["id"], okres)).fetchone()
        b = budynek_lokalu(u["lokal_id"])
        wiersze.append({"u": u, "n": n, "r": None if n else oblicz(u, okres), "b": b["nazwa"] if b else None})
    wiersze.sort(key=lambda w: (w["b"] is None, w["b"] or "", w["u"]["lokal_nazwa"]))
    do_wystawienia = sum(1 for w in wiersze if not w["n"])
    return render_template("admin/rozliczenia.html", active="rozliczenia", okres=okres,
                           wiersze=wiersze, do_wystawienia=do_wystawienia)


@bp.post("/rozliczenia/wystaw")
def rozliczenia_wystaw():
    okres = okres_z_args()
    uid = request.form.get("umowa_id", type=int)
    db = get_db()
    nowe = 0
    for u in umowy_w_okresie(okres):
        if uid and u["id"] != uid:
            continue
        if db.execute("SELECT 1 FROM naliczenia WHERE umowa_id=? AND okres_czynsz=?", (u["id"], okres)).fetchone():
            continue
        wystaw(u, okres)
        nowe += 1
    db.commit()
    flash(f"Wystawiono zawiadomienia: {nowe}." if nowe else "Nie było nic do wystawienia.", "ok")
    return redirect(url_for(".rozliczenia", okres=okres))


@bp.get("/rozliczenia/druk")
def rozliczenia_druk():
    okres = okres_z_args()
    kwity = []
    for (nid,) in get_db().execute(
            """SELECT n.id FROM naliczenia n JOIN umowy u ON u.id=n.umowa_id JOIN lokale l ON l.id=u.lokal_id
               WHERE n.okres_czynsz=? ORDER BY l.nazwa""", (okres,)).fetchall():
        n, u, poz = zawiadomienie_dane(nid)
        kwity.append({"n": n, "u": u, "pozycje": poz, "rachunek": rachunek_umowy(u)})
    return render_template("admin/kwity_druk.html", active="rozliczenia", okres=okres, kwity=kwity)


@bp.get("/rozliczenia/podglad/<int:uid>")
def rozliczenie_podglad(uid):
    okres = okres_z_args()
    u = get_db().execute(UMOWY_SQL + " WHERE u.id=?", (uid,)).fetchone()
    if u is None:
        abort(404)
    r = oblicz(u, okres)
    return render_template("zawiadomienie.html", base_tpl="admin/base.html", active="rozliczenia",
                           u=u, n=r, pozycje=r["pozycje"], uwagi=r["uwagi"], podglad=True,
                           rachunek=rachunek_umowy(u), wstecz=url_for(".rozliczenia", okres=okres))


@bp.get("/rozliczenia/<int:nid>")
def rozliczenie(nid):
    n, u, poz = zawiadomienie_dane(nid)
    return render_template("zawiadomienie.html", base_tpl="admin/base.html", active="rozliczenia",
                           u=u, n=n, pozycje=poz, uwagi=[], podglad=False, rachunek=rachunek_umowy(u),
                           wstecz=url_for(".rozliczenia", okres=n["okres_czynsz"]))


@bp.post("/rozliczenia/<int:nid>/usun")
def rozliczenie_usun(nid):
    n = _get_or_404("naliczenia", nid)
    db = get_db()
    pozniejsze = db.execute("SELECT count(*) FROM naliczenia WHERE umowa_id=? AND okres_czynsz>?",
                            (n["umowa_id"], n["okres_czynsz"])).fetchone()[0]
    db.execute("DELETE FROM naliczenia_pozycje WHERE naliczenie_id=?", (nid,))
    db.execute("DELETE FROM naliczenia WHERE id=?", (nid,))
    db.commit()
    msg = "Cofnięto zawiadomienie — możesz je wystawić ponownie."
    if pozniejsze:
        msg += " Uwaga: późniejsze zawiadomienia tej umowy mają saldo liczone z jego udziałem."
    flash(msg, "ok")
    return redirect(url_for(".rozliczenia", okres=n["okres_czynsz"]))


# ---------- odczyty liczników ----------
def _num(v):
    v = (v or "").strip().replace(" ", "").replace(",", ".")
    if not v:
        return None
    try:
        d = Decimal(v)
        if d < 0 or d > Decimal("1e9"):
            raise InvalidOperation
        return d
    except InvalidOperation:
        raise ValueError


@bp.route("/liczniki", methods=["GET", "POST"])
def liczniki():
    okres = okres_z_args()
    db = get_db()
    b, bl = wybrany_budynek()
    if b is None:
        return render_template("admin/liczniki.html", active="liczniki", okres=okres, b=None, budynki=bl)
    gid = glowny_id(b["id"])
    lokale = [{"id": gid, "nazwa": "Liczniki główne budynku", "glowny": True}] + [
        dict(l, glowny=False) for l in db.execute("SELECT * FROM lokale WHERE budynek_id=? ORDER BY nazwa", (b["id"],))]
    bledy = {}
    if request.method == "POST":
        data = request.form.get("data") or granice(okres)[0]
        try:
            data = date.fromisoformat(data).isoformat()
        except ValueError:
            bledy["data"] = "Nieprawidłowa data."
        zmiany = []
        for l in lokale:
            vals = {}
            for k in ("cieplo", "woda"):
                try:
                    vals[k] = _num(request.form.get(f"{k}_{l['id']}"))
                except ValueError:
                    bledy[f"{k}_{l['id']}"] = "Podaj liczbę ≥ 0."
            zmiany.append((l["id"], vals))
        if not bledy:
            for lid, vals in zmiany:
                if vals["cieplo"] is None and vals["woda"] is None:
                    db.execute("DELETE FROM odczyty WHERE lokal_id=? AND okres=?", (lid, okres))
                else:
                    db.execute(
                        "INSERT INTO odczyty (lokal_id, okres, data, cieplo, woda) VALUES (?,?,?,?,?)"
                        " ON CONFLICT(lokal_id, okres) DO UPDATE SET data=excluded.data,"
                        " cieplo=excluded.cieplo, woda=excluded.woda",
                        (lid, okres, data,
                         None if vals["cieplo"] is None else float(vals["cieplo"]),
                         None if vals["woda"] is None else float(vals["woda"])))
            db.commit()
            flash(f"Zapisano odczyty: {b['nazwa']}, stan na {fmt_data(okres + '-01')}.", "ok")
            return redirect(url_for(".liczniki", okres=okres, b=b["id"]))
        flash("Popraw zaznaczone pola.", "err")

    wiersze, suma = [], {"cieplo": Decimal(0), "woda": Decimal(0)}
    data_odczytu = None
    for l in lokale:
        o, p = odczyt(l["id"], okres), odczyt(l["id"], poprzedni(okres))
        z = zuzycie(l["id"], poprzedni(okres))
        if o and o["data"]:
            data_odczytu = data_odczytu or o["data"]
        ostrz = [k for k in ("cieplo", "woda") if z[k] is not None and z[k] < 0]
        wym = [k for k in ("cieplo", "woda") if wymiana(l["id"], k, poprzedni(okres))]
        if not l["glowny"]:
            for k in suma:
                if z[k] is not None:
                    suma[k] += z[k]
        wiersze.append({"l": l, "o": o, "p": p, "z": z, "ostrz": ostrz, "wym": wym})
    glowny = wiersze[0]["z"]
    podzial = {r: rozdzial(r, poprzedni(okres), b["id"]) for r in ("woda", "cieplo")}
    form = request.form if request.method == "POST" else None
    wymian_n = db.execute(f"SELECT count(*) FROM wymiany WHERE lokal_id IN ({','.join('?' * len(lokale))})",
                          [l["id"] for l in lokale]).fetchone()[0]
    return render_template("admin/liczniki.html", active="liczniki", okres=okres, wiersze=wiersze, b=b, budynki=bl,
                           wymian_n=wymian_n,
                           suma=suma, glowny=glowny, bledy=bledy, form=form, podzial=podzial,
                           data_odczytu=data_odczytu or granice(okres)[0])


# ---------- wymiana liczników ----------
NAZWA_LICZNIKA = {"woda": "wodomierz", "cieplo": "ciepłomierz"}


def _liczniki_budynku(b):
    """(id, nazwa, nr wodomierza, nr ciepłomierza) — liczniki główne + lokale budynku."""
    out = [(glowny_id(b["id"]), "Liczniki główne budynku", None, None)]
    for l in get_db().execute("SELECT * FROM lokale WHERE budynek_id=? ORDER BY nazwa", (b["id"],)):
        out.append((l["id"], l["nazwa"], l["nr_wodomierza"], l["nr_cieplomierza"]))
    return out


@bp.route("/liczniki/wymiany", methods=["GET", "POST"])
def wymiany():
    b, bl = wybrany_budynek()
    if b is None:
        return redirect(url_for(".liczniki"))
    db = get_db()
    liczniki_b = _liczniki_budynku(b)
    ids = {x[0]: x for x in liczniki_b}
    lid = request.values.get("lokal", type=int)
    rodzaj = request.values.get("rodzaj", "woda")
    status = 200
    if request.method == "POST":
        f = Form(request.form)
        lid = f.number("lokal_id", "Licznik", -10**9, 10**9, required=True)
        if lid is not None and lid not in ids:
            f.error("lokal_id", "Wybierz lokal tego budynku.")
        rodzaj = f.choice("rodzaj", "Rodzaj", ["woda", "cieplo"])
        d = f.date("data", "Data wymiany")
        ok = f.text("okres", "Miesiąc zużycia", required=True, max_len=7)
        if ok and not OKRES_RE.fullmatch(ok):
            f.error("okres", "Miesiąc w formacie RRRR-MM.")
        try:
            stary, nowy = _num(f.raw.get("stan_stary")), _num(f.raw.get("stan_nowy")) or Decimal(0)
        except ValueError:
            stary = nowy = None
            f.error("stan_stary", "Podaj liczby ≥ 0.")
        if stary is None:
            f.error("stan_stary", "Podaj stan końcowy starego licznika.")
        f.text("nr_stary", "Nr starego licznika", max_len=50)
        f.text("nr_nowy", "Nr nowego licznika", max_len=50)
        f.text("uwagi", "Uwagi", max_len=1000)
        if f.ok:
            a, bb = odczyt(lid, ok), odczyt(lid, nastepny(ok))
            if a and a[rodzaj] is not None and stary < dec(a[rodzaj]):
                f.error("stan_stary", f"Mniejszy niż odczyt z {fmt_data(ok + '-01')} ({liczba(a[rodzaj], 3)}).")
            if bb and bb[rodzaj] is not None and dec(bb[rodzaj]) < nowy:
                f.error("stan_nowy", f"Większy niż odczyt z {fmt_data(nastepny(ok) + '-01')} ({liczba(bb[rodzaj], 3)}).")
        if f.ok:
            db.execute("""INSERT INTO wymiany (lokal_id, rodzaj, okres, data, stan_stary, stan_nowy, nr_stary, nr_nowy, uwagi)
                          VALUES (?,?,?,?,?,?,?,?,?)
                          ON CONFLICT(lokal_id, rodzaj, okres) DO UPDATE SET data=excluded.data,
                          stan_stary=excluded.stan_stary, stan_nowy=excluded.stan_nowy, nr_stary=excluded.nr_stary,
                          nr_nowy=excluded.nr_nowy, uwagi=excluded.uwagi""",
                       (lid, rodzaj, ok, d, float(stary), float(nowy), f.clean["nr_stary"], f.clean["nr_nowy"],
                        f.clean["uwagi"]))
            if lid > 0 and f.clean["nr_nowy"]:            # nowy numer licznika w danych lokalu
                kol = "nr_wodomierza" if rodzaj == "woda" else "nr_cieplomierza"
                db.execute(f"UPDATE lokale SET {kol}=? WHERE id=?", (f.clean["nr_nowy"], lid))
            db.commit()
            flash(f"Zapisano wymianę: {ids[lid][1]}, {NAZWA_LICZNIKA[rodzaj]}, {fmt_okres(ok)}.", "ok")
            return redirect(url_for(".wymiany", b=b["id"]))
        status = 422
    else:
        okres = okres_z_args()
        pre = {"lokal_id": str(lid or ""), "rodzaj": rodzaj, "okres": okres, "data": date.today().isoformat(),
               "stan_nowy": "0"}
        if lid in ids:
            pre["nr_stary"] = ids[lid][2] if rodzaj == "woda" else ids[lid][3]
            pre["nr_stary"] = pre["nr_stary"] or ""
        f = Form(pre)
    lista = db.execute(f"""SELECT * FROM wymiany WHERE lokal_id IN ({','.join('?' * len(ids))})
                           ORDER BY okres DESC, id DESC""", list(ids)).fetchall()
    wiersze = []
    for w in lista:
        a, bb = odczyt(w["lokal_id"], w["okres"]), odczyt(w["lokal_id"], nastepny(w["okres"]))
        wiersze.append({"w": w, "nazwa": ids[w["lokal_id"]][1], "z": zuzycie(w["lokal_id"], w["okres"])[w["rodzaj"]],
                        "a": a[w["rodzaj"]] if a else None, "b": bb[w["rodzaj"]] if bb else None})
    return render_template("admin/wymiany.html", active="liczniki", f=f, b=b, budynki=bl,
                           opcje=[(x[0], x[1]) for x in liczniki_b], wiersze=wiersze), status


@bp.post("/liczniki/wymiany/<int:wid>/usun")
def wymiana_usun(wid):
    w = _get_or_404("wymiany", wid)
    get_db().execute("DELETE FROM wymiany WHERE id=?", (wid,))
    get_db().commit()
    flash("Usunięto wymianę licznika.", "ok")
    return redirect(url_for(".wymiany", b=request.form.get("b") or ""))


# ---------- faktury ----------
def _faktura_form(f):
    bid = f.number("budynek_id", "Budynek", 1, 10**9, required=True)
    if bid and budynek(bid) is None:
        f.error("budynek_id", "Wybierz budynek.")
    f.choice("rodzaj", "Rodzaj", ["woda", "cieplo"])
    f.text("numer", "Numer faktury", required=True, max_len=80)
    o = f.text("okres", "Miesiąc zużycia", required=True, max_len=7)
    if o and not OKRES_RE.fullmatch(o):
        f.error("okres", "Miesiąc w formacie RRRR-MM.")
    f.date("data", "Data wystawienia", required=False)
    f.number("zuzycie", "Zużycie", 0, 10**7, required=True, integer=False)
    f.money("kwota", "Kwota brutto", positive=True)
    f.money("oplata_stala", "Opłaty stałe", required=False)
    f.money("odliczenie", "Odliczenie", required=False)
    f.text("uwagi", "Uwagi", max_len=1000)
    if f.ok:
        odj = f.clean["oplata_stala"] if f.clean["rodzaj"] == "cieplo" else f.clean["odliczenie"]
        if odj > f.clean["kwota"]:
            f.error("oplata_stala" if f.clean["rodzaj"] == "cieplo" else "odliczenie",
                    "Nie może być większe niż kwota faktury.")
        if f.clean["rodzaj"] == "woda":
            f.clean["oplata_stala"] = 0
        else:
            f.clean["odliczenie"] = 0
    return f


def _faktury_lista(bid=None):
    q, args = "SELECT * FROM faktury", []
    if bid:
        q += " WHERE budynek_id=?"
        args.append(bid)
    rows = get_db().execute(q + " ORDER BY okres DESC, rodzaj DESC, id DESC", args).fetchall()
    return [(r, cena_jedn(r)) for r in rows]


def _faktury_ctx(**kw):
    bid = request.args.get("b", type=int)
    return dict(active="faktury", budynki=budynki_lista(), filtr_b=bid, **kw)


@bp.route("/faktury", methods=["GET", "POST"])
def faktury():
    if request.method == "POST":
        f = _faktura_form(Form(request.form))
        if f.ok:
            _insert("faktury", f.clean)
            get_db().commit()
            flash(f"Dodano fakturę {f.clean['numer']}.", "ok")
            return redirect(url_for(".faktury"))
        return render_template("admin/faktury.html", f=f, fv=None, rows=_faktury_lista(),
                               otworz=True, **_faktury_ctx()), 422
    bl = budynki_lista()
    bid = request.args.get("b", type=int) or (bl[0]["id"] if len(bl) == 1 else "")
    f = Form({"rodzaj": request.args.get("rodzaj", "woda"), "okres": poprzedni(biezacy()),
              "data": date.today().isoformat(), "budynek_id": str(bid)})
    return render_template("admin/faktury.html", f=f, fv=None, rows=_faktury_lista(request.args.get("b", type=int)),
                           otworz=False, **_faktury_ctx())


@bp.route("/faktury/<int:fid>", methods=["GET", "POST"])
def faktura_edycja(fid):
    fv = _get_or_404("faktury", fid)
    if request.method == "POST":
        f = _faktura_form(Form(request.form))
        if f.ok:
            _update("faktury", fid, f.clean)
            get_db().commit()
            flash("Zapisano fakturę.", "ok")
            return redirect(url_for(".faktury"))
        return render_template("admin/faktury.html", f=f, fv=fv, rows=[], otworz=True, **_faktury_ctx()), 422
    raw = {k: ("" if fv[k] is None else str(fv[k])) for k in fv.keys()}
    for k in ("kwota", "oplata_stala", "odliczenie"):
        raw[k] = zl_input(fv[k])
    raw["zuzycie"] = f"{fv['zuzycie']:g}".replace(".", ",")
    return render_template("admin/faktury.html", f=Form(raw), fv=fv, rows=[], otworz=True, **_faktury_ctx())


@bp.post("/faktury/<int:fid>/usun")
def faktura_usun(fid):
    fv = _get_or_404("faktury", fid)
    get_db().execute("DELETE FROM faktury WHERE id=?", (fid,))
    get_db().commit()
    flash(f"Usunięto fakturę {fv['numer']}.", "ok")
    return redirect(url_for(".faktury"))


# ---------- wpłaty ----------
def _umowy_opcje():
    return get_db().execute(UMOWY_SQL + " ORDER BY u.data_od DESC").fetchall()


@bp.route("/wplaty", methods=["GET", "POST"])
def wplaty():
    db = get_db()
    filtr = request.values.get("umowa", type=int)
    f = Form({"umowa_id": str(filtr or ""), "data": date.today().isoformat()})
    status = 200
    if request.method == "POST":
        f = Form(request.form)
        uid = f.number("umowa_id", "Umowa", 1, 10**9, required=True)
        if uid and not db.execute("SELECT 1 FROM umowy WHERE id=?", (uid,)).fetchone():
            f.error("umowa_id", "Wybierz umowę.")
        f.date("data", "Data wpłaty")
        f.money("kwota", "Kwota", positive=True)
        f.text("opis", "Opis", max_len=300)
        if f.ok:
            _insert("wplaty", f.clean)
            db.commit()
            flash(f"Zapisano wpłatę {zl_txt(Decimal(f.clean['kwota']) / 100)}.", "ok")
            return redirect(url_for(".wplaty", umowa=filtr or ""))
        status = 422

    umowy = _umowy_opcje()
    salda = []
    for u in umowy:
        nal = db.execute("SELECT coalesce(sum(suma_pozycji),0) FROM naliczenia WHERE umowa_id=?", (u["id"],)).fetchone()[0]
        wpl = db.execute("SELECT coalesce(sum(kwota),0) FROM wplaty WHERE umowa_id=?", (u["id"],)).fetchone()[0]
        if nal or wpl:
            salda.append({"u": u, "nal": nal, "wpl": wpl, "saldo": nal - wpl})
    q = """SELECT w.*, u.numer, l.nazwa AS lokal_nazwa, coalesce(k.full_name, k.login) AS najemca
           FROM wplaty w JOIN umowy u ON u.id=w.umowa_id JOIN lokale l ON l.id=u.lokal_id
           JOIN users k ON k.id=u.najemca_id"""
    args = []
    if filtr:
        q += " WHERE w.umowa_id=?"
        args.append(filtr)
    rows = db.execute(q + " ORDER BY w.data DESC, w.id DESC LIMIT 300", args).fetchall()
    return render_template("admin/wplaty.html", active="wplaty", f=f, rows=rows, umowy=umowy,
                           salda=salda, filtr=filtr), status


@bp.post("/wplaty/<int:wid>/usun")
def wplata_usun(wid):
    _get_or_404("wplaty", wid)
    get_db().execute("DELETE FROM wplaty WHERE id=?", (wid,))
    get_db().commit()
    flash("Usunięto wpłatę.", "ok")
    return redirect(url_for(".wplaty", umowa=request.form.get("umowa", "")))


# ---------- ustawienia ----------
@bp.route("/ustawienia", methods=["GET", "POST"])
def ustawienia():
    db = get_db()
    s = ust()
    if request.method == "POST":
        f = Form(request.form)
        zapis = {}
        for k, label, _, _, typ in USTAWIENIA:
            pusty = not (f.raw.get(k) or "").strip()
            if typ == "money":
                v = f.money(k, label, required=False)
                zapis[k] = "" if pusty else str(v)
            elif typ == "number":
                v = f.number(k, label, 0.001, 100, integer=False)
                zapis[k] = "" if v is None else str(Decimal(str(v)))
            elif typ == "int":
                v = f.number(k, label, 1, 1000)
                zapis[k] = "" if v is None else str(v)
            else:
                zapis[k] = f.text(k, label, max_len=200) or ""
        if f.ok:
            for k, v in zapis.items():
                db.execute("INSERT INTO ustawienia (klucz, wartosc) VALUES (?,?)"
                           " ON CONFLICT(klucz) DO UPDATE SET wartosc=excluded.wartosc", (k, v))
            db.commit()
            flash("Zapisano ustawienia.", "ok")
            return redirect(url_for(".ustawienia"))
        return render_template("admin/ustawienia.html", active="ustawienia", f=f, pola=USTAWIENIA), 422
    raw = {}
    for k, _, _, _, typ in USTAWIENIA:
        v = s[k]
        raw[k] = "" if v is None else zl_input(v) if typ == "money" else str(v).replace(".", ",")
    return render_template("admin/ustawienia.html", active="ustawienia", f=Form(raw), pola=USTAWIENIA)


# =====================================================================
# PANEL KLIENTA
# =====================================================================
def _moje_umowy_ids():
    return [r[0] for r in get_db().execute("SELECT id FROM umowy WHERE najemca_id=?", (g.user["id"],))]


@app.get("/panel/platnosci")
@login_required
def platnosci():
    db = get_db()
    ids = _moje_umowy_ids()
    zaw, wpl, salda = [], [], []
    if ids:
        q = ",".join("?" * len(ids))
        zaw = db.execute(f"""SELECT n.*, u.numer, l.nazwa AS lokal_nazwa FROM naliczenia n
                             JOIN umowy u ON u.id=n.umowa_id JOIN lokale l ON l.id=u.lokal_id
                             WHERE n.umowa_id IN ({q}) ORDER BY n.okres_czynsz DESC""", ids).fetchall()
        wpl = db.execute(f"""SELECT w.*, u.numer FROM wplaty w JOIN umowy u ON u.id=w.umowa_id
                             WHERE w.umowa_id IN ({q}) ORDER BY w.data DESC LIMIT 100""", ids).fetchall()
        for uid in ids:
            if db.execute("SELECT 1 FROM naliczenia WHERE umowa_id=? UNION SELECT 1 FROM wplaty WHERE umowa_id=?",
                          (uid, uid)).fetchone():
                u = db.execute("SELECT u.numer, l.nazwa AS lokal_nazwa FROM umowy u JOIN lokale l ON l.id=u.lokal_id"
                               " WHERE u.id=?", (uid,)).fetchone()
                salda.append({"u": u, "saldo": saldo(uid)})
    ost = db.execute("SELECT * FROM umowy WHERE najemca_id=? ORDER BY data_od DESC LIMIT 1", (g.user["id"],)).fetchone()
    return render_template("platnosci.html", active="platnosci", zaw=zaw, wpl=wpl, salda=salda,
                           rachunek=rachunek_umowy(ost) if ost else ust()["rachunek"])


@app.get("/panel/platnosci/<int:nid>")
@login_required
def platnosc(nid):
    n, u, poz = zawiadomienie_dane(nid)
    if u["najemca_id"] != g.user["id"]:
        abort(404)
    return render_template("zawiadomienie.html", base_tpl="panel.html", active="platnosci",
                           u=u, n=n, pozycje=poz, uwagi=[], podglad=False, rachunek=rachunek_umowy(u),
                           wstecz=url_for("platnosci"))


@app.get("/panel/media")
@login_required
def media():
    db = get_db()
    lokale = db.execute("""SELECT DISTINCT l.* FROM lokale l JOIN umowy u ON u.lokal_id=l.id
                           WHERE u.najemca_id=? ORDER BY l.nazwa""", (g.user["id"],)).fetchall()
    dane = []
    for l in lokale:
        odcz = db.execute("SELECT * FROM odczyty WHERE lokal_id=? ORDER BY okres DESC LIMIT 25",
                          (l["id"],)).fetchall()
        hist = []
        for i, o in enumerate(odcz):
            za = poprzedni(o["okres"])
            z = zuzycie(l["id"], za)
            wym = [NAZWA_LICZNIKA[k] for k in ("cieplo", "woda") if wymiana(l["id"], k, za)]
            hist.append({"o": o, "z": z, "za": za, "wym": wym})
        dane.append({"l": l, "hist": hist[:24]})
    return render_template("media.html", active="media", dane=dane)
