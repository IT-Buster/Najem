"""Kreator zawiadomień — szybka ścieżka co miesiąc, wygodna na telefonie:

 1. Liczniki: odczyty, które już są (telemetria przez /api/pomiary, zatwierdzone odczyty najemców), kreator
    pokazuje jako gotowe ze źródłem. Brakujące zarządca fotografuje i wpisuje → odczyt na 1. dzień miesiąca
    zawiadomień (ręczny odczyt zarządcy ma pierwszeństwo przed pomiarami); zdjęcie zostaje w historii pomiarów.
 2. Faktury: aplikacja przeszukuje Gmail (MWiO — woda, OPEC — ciepło) i proponuje faktury za miesiąc
    zużycia; zużycie z liczników głównych, opłaty stałe ciepła z poprzedniej faktury. Zarządca zatwierdza.
 3. Gdy są odczyty i obie faktury — podgląd i wystawienie zawiadomień dla umów w budynku.

Zawiadomienia za miesiąc N rozliczają media z miesiąca N−1: potrzebne odczyty na 1.N i faktury za N−1.
"""
from datetime import date, datetime

from flask import flash, g, redirect, render_template, request, url_for

import gmail
from admin import Form, _insert, bp
from app import get_db
from pomiary import OKNO, _liczba, _zapisz_zdjecie, auto_przenies, granica, kandydat, przenies_okres
from rozliczenia import (NAZWA_LICZNIKA, OKRES_RE, _faktura_form, biezacy, budynek, fmt_okres, glowny_id, ma_cieplo, nastepny,
                         oblicz, odczyt, poprzedni, umowy_w_okresie, wybrany_budynek, wystaw, zuzycie)

RODZAJE = ("woda", "cieplo")
JEDN = {"woda": "m³", "cieplo": "GJ"}


def umowy_budynku(bid, okres):
    lokale = {r[0] for r in get_db().execute("SELECT id FROM lokale WHERE budynek_id=?", (bid,))}
    return [u for u in umowy_w_okresie(okres) if u["lokal_id"] in lokale]


def domyslny_okres(bid):
    """Najbliższy miesiąc zawiadomień, które jeszcze nie są wystawione (bieżący albo następny)."""
    o = biezacy()
    umowy = umowy_budynku(bid, o)
    db = get_db()
    if any(db.execute("SELECT 1 FROM naliczenia WHERE umowa_id=? AND okres_czynsz=?", (u["id"], o)).fetchone()
           for u in umowy):
        return nastepny(o)
    return o


def domyslnie_wystawiac(u):
    """Umowa bez czynszu i bez żadnego zawiadomienia — domyślnie odznaczona."""
    return bool(u["czynsz"]) or get_db().execute("SELECT 1 FROM naliczenia WHERE umowa_id=? LIMIT 1",
                                                 (u["id"],)).fetchone() is not None


def liczniki(b, okres):
    """Liczniki do spisania: główne budynku + lokale. Każdy: id lokalu (−id budynku = główne), rodzaj, stan."""
    db = get_db()
    gid = glowny_id(b["id"])
    out = []
    pop_g = odczyt(gid, poprzedni(okres))
    lokale = db.execute("SELECT * FROM lokale WHERE budynek_id=? ORDER BY nazwa", (b["id"],)).fetchall()
    for k in RODZAJE:
        nr = b["nr_wodomierza"] if k == "woda" else b["nr_cieplomierza"]
        if nr or (pop_g is not None and pop_g[k] is not None):
            out.append({"lid": gid, "nazwa": "Licznik główny budynku", "rodzaj": k, "nr": nr, "glowny": True})
    for l in lokale:
        pop = odczyt(l["id"], poprzedni(okres))
        if l["nr_wodomierza"] or (pop is not None and pop["woda"] is not None):
            out.append({"lid": l["id"], "nazwa": l["nazwa"], "rodzaj": "woda", "nr": l["nr_wodomierza"], "glowny": False})
        if ma_cieplo(l):
            out.append({"lid": l["id"], "nazwa": l["nazwa"], "rodzaj": "cieplo", "nr": l["nr_cieplomierza"], "glowny": False})
    g_ = granica(okres)
    okno = ((g_ - OKNO).isoformat(), (g_ + OKNO).isoformat())
    for x in out:
        o, p = odczyt(x["lid"], okres), odczyt(x["lid"], poprzedni(okres))
        x["zrodlo"] = None
        if o is not None and o[x["rodzaj"]] is not None:
            if o["zrodlo"] is None:
                x["zrodlo"] = "wpisany ręcznie"
            else:
                k = kandydat(x["lid"], x["rodzaj"], okres)
                if k is not None and k["zrodlo"] == "telemetria":
                    u = db.execute("SELECT nazwa FROM urzadzenia WHERE id=?", (k["urzadzenie_id"],)).fetchone()
                    x["zrodlo"] = f"telemetria{' · ' + u['nazwa'] if u else ''} · {k['czas'][:16].replace('T', ' ')}"
                elif k is not None:
                    x["zrodlo"] = {"najemca": "podał najemca", "zarzadca": "zarządca"}.get(k["zrodlo"], k["zrodlo"])
        x["oczekuje"] = db.execute(
            "SELECT count(*) FROM pomiary WHERE lokal_id=? AND rodzaj=? AND status='oczekuje' AND czas BETWEEN ? AND ?",
            (x["lid"], x["rodzaj"], *okno)).fetchone()[0]
        x["stan"] = o[x["rodzaj"]] if o else None
        x["poprz"] = p[x["rodzaj"]] if p else None
        x["zuzycie"] = (zuzycie(x["lid"], poprzedni(okres))[x["rodzaj"]]) if x["stan"] is not None else None
        x["zdjecie"] = db.execute(
            """SELECT id FROM pomiary WHERE lokal_id=? AND rodzaj=? AND zdjecie IS NOT NULL AND status='zatwierdzony'
               AND czas>=? ORDER BY czas DESC, id DESC LIMIT 1""",
            (x["lid"], x["rodzaj"], poprzedni(okres) + "-20")).fetchone()
        x["klucz"] = f"{x['lid']}-{x['rodzaj']}"
    return out


def faktury_za(bid, om):
    return {f["rodzaj"]: f for f in get_db().execute(
        "SELECT * FROM faktury WHERE budynek_id=? AND okres=? ORDER BY id", (bid, om))}


def propozycja(f_mail, b):
    """Wartości formularza faktury na podstawie e-maila."""
    db = get_db()
    z = zuzycie(glowny_id(b["id"]), f_mail["okres"])[f_mail["rodzaj"]]
    poprz = db.execute("SELECT * FROM faktury WHERE budynek_id=? AND rodzaj=? AND okres<? ORDER BY okres DESC, id DESC LIMIT 1",
                       (b["id"], f_mail["rodzaj"], f_mail["okres"])).fetchone()
    return {"budynek_id": str(b["id"]), "rodzaj": f_mail["rodzaj"], "numer": f_mail["numer"], "okres": f_mail["okres"],
            "data": f_mail["data"], "kwota": f"{f_mail['kwota'] / 100:.2f}".replace(".", ","),
            "zuzycie": "" if z is None else f"{z:g}".replace(".", ","),
            "oplata_stala": (f"{poprz['oplata_stala'] / 100:.2f}".replace(".", ",")
                             if f_mail["rodzaj"] == "cieplo" and poprz else "0"),
            "odliczenie": "0", "uwagi": f"z e-maila {f_mail['dostawca']} z {f_mail['data']}",
            "zuzycie_z_licznika": z is not None, "oplata_z": poprz["numer"] if poprz and f_mail["rodzaj"] == "cieplo" else None}


def _ctx(b, bl, okres, **kw):
    om = poprzedni(okres)
    lc = liczniki(b, okres) if b else []
    fv = faktury_za(b["id"], om) if b else {}
    potrzebne = [k for k in RODZAJE if any(x["rodzaj"] == k for x in lc)] or list(RODZAJE)
    umowy = umowy_budynku(b["id"], okres) if b else []
    db = get_db()
    podglad = []
    for u in umowy:
        n = db.execute("SELECT * FROM naliczenia WHERE umowa_id=? AND okres_czynsz=?", (u["id"], okres)).fetchone()
        podglad.append({"u": u, "n": n, "r": None if n else oblicz(u, okres), "domyslnie": domyslnie_wystawiac(u)})
    krok1 = bool(lc) and all(x["stan"] is not None for x in lc)
    krok2 = all(k in fv for k in potrzebne)
    gm = gmail.dane()
    return dict(active="kreator", b=b, budynki=bl, okres=okres, om=om, liczniki=lc, faktury=fv, potrzebne=potrzebne,
                podglad=podglad, krok1=krok1 or not lc, krok2=krok2, bez_licznikow=not lc,
                do_wystawienia=sum(1 for p in podglad if not p["n"] and not p["u"]["tylko_informacja"]),
                gmail_login=gm["login"] if gm else None, odbiorcy=gmail.odbiorcy(), nazwa_licznika=NAZWA_LICZNIKA, jedn=JEDN,
                dzis=date.today().isoformat(), **kw)


def _okres():
    o = request.values.get("okres", "")
    return o if OKRES_RE.fullmatch(o) else None


@bp.get("/kreator")
def kreator():
    auto_przenies()                         # pomiary z telemetrii / zatwierdzone od najemców → odczyty miesiąca
    b, bl = wybrany_budynek()
    if b is None:
        return render_template("admin/kreator.html", **_ctx(None, bl, biezacy()))
    okres = _okres() or domyslny_okres(b["id"])
    if okres not in (biezacy(), poprzedni(biezacy())):
        przenies_okres(okres)
    return render_template("admin/kreator.html", **_ctx(b, bl, okres))


@bp.post("/kreator/licznik")
def kreator_licznik():
    b, _ = wybrany_budynek()
    okres = _okres()
    lid, rodzaj = request.form.get("lid", type=int), request.form.get("rodzaj")
    if b is None or okres is None or rodzaj not in RODZAJE:
        flash("Nieprawidłowe dane licznika.", "err")
        return redirect(url_for(".kreator"))
    wroc = url_for(".kreator", b=b["id"], okres=okres) + f"#l-{lid}-{rodzaj}"
    if not any(x["lid"] == lid and x["rodzaj"] == rodzaj for x in liczniki(b, okres)):
        flash("Ten licznik nie należy do budynku.", "err")
        return redirect(wroc)
    try:
        v = _liczba(request.form.get("stan"))
    except ValueError:
        v = None
    if v is None:
        flash("Podaj stan licznika (liczba, np. 123,456).", "err")
        return redirect(wroc)
    try:
        zdj = _zapisz_zdjecie(request.files.get("zdjecia"))
    except ValueError as e:
        flash(str(e), "err")
        return redirect(wroc)
    db = get_db()
    teraz = datetime.now().replace(microsecond=0).isoformat()
    db.execute("""INSERT INTO pomiary (lokal_id, rodzaj, wartosc, czas, zrodlo, status, user_id, zdjecie, uwagi)
                  VALUES (?,?,?,?, 'zarzadca', 'zatwierdzony', ?,?, 'kreator zawiadomień')""",
               (lid, rodzaj, float(v), teraz, g.user["id"], zdj))
    ist = odczyt(lid, okres)
    if ist is None:
        db.execute(f"INSERT INTO odczyty (lokal_id, okres, data, {rodzaj}) VALUES (?,?,?,?)",
                   (lid, okres, date.today().isoformat(), float(v)))
    else:                                   # zrodlo NULL = odczyt ręczny zarządcy (pomiary go nie nadpiszą)
        db.execute(f"UPDATE odczyty SET {rodzaj}=?, data=?, zrodlo=NULL WHERE id=?",
                   (float(v), date.today().isoformat(), ist["id"]))
    db.commit()
    pop = odczyt(lid, poprzedni(okres))
    msg = f"Zapisano: {NAZWA_LICZNIKA[rodzaj]} {float(v):g}".replace(".", ",")
    if pop is not None and pop[rodzaj] is not None and float(v) < pop[rodzaj]:
        flash(f"Uwaga: stan {float(v):g} jest mniejszy niż poprzedni ({pop[rodzaj]:g}) — sprawdź odczyt "
              "albo wpisz wymianę licznika.", "err")
    else:
        flash(msg + (" ze zdjęciem." if zdj else " (bez zdjęcia)."), "ok")
    nast = next((x for x in liczniki(b, okres) if x["stan"] is None), None)
    return redirect(url_for(".kreator", b=b["id"], okres=okres) + (f"#l-{nast['klucz']}" if nast else "#faktury"))


@bp.post("/kreator/gmail")
def kreator_gmail():
    b, bl = wybrany_budynek()
    okres = _okres() or biezacy()
    if b is None:
        return redirect(url_for(".kreator"))
    om = poprzedni(okres)
    try:
        maile = gmail.szukaj(date.fromisoformat(om + "-01"))
        blad = None
    except gmail.BladGmail as e:
        maile, blad = [], str(e)
    znane = {gmail.klucz_numeru(r[0]) for r in get_db().execute("SELECT numer FROM faktury")}
    for m in maile:
        m["jest"] = gmail.klucz_numeru(m["numer"]) in znane
        m["form"] = propozycja(m, b)
    if blad:
        flash(blad, "err")
    elif not maile:
        flash(f"Brak e-maili z fakturami MWiO / OPEC od {om}-01.", "err")
    return render_template("admin/kreator.html", **_ctx(b, bl, okres, maile=maile, sprawdzono=True)), (200 if not blad else 502)


@bp.post("/kreator/faktura")
def kreator_faktura():
    b, bl = wybrany_budynek()
    okres = _okres() or biezacy()
    f = _faktura_form(Form(request.form))
    if f.ok and gmail.klucz_numeru(f.clean["numer"]) in {gmail.klucz_numeru(r[0]) for r in get_db().execute("SELECT numer FROM faktury")}:
        f.error("numer", "Faktura o tym numerze już jest w bazie.")
    if not f.ok:
        flash("Faktura nie została dodana: " + " ".join(f.errors.values()), "err")
        return redirect(url_for(".kreator", b=b["id"] if b else "", okres=okres) + "#faktury")
    _insert("faktury", f.clean)
    get_db().commit()
    flash(f"Dodano fakturę {f.clean['numer']} ({'woda' if f.clean['rodzaj'] == 'woda' else 'ciepło'}).", "ok")
    powiadom_faktura(f.clean)
    return redirect(url_for(".kreator", b=f.clean["budynek_id"], okres=okres) + "#faktury")


@bp.post("/kreator/gmail/ustawienia")
def kreator_gmail_ustawienia():
    b, _ = wybrany_budynek()
    okres = _okres() or ""
    wroc = url_for(".kreator", b=b["id"] if b else "", okres=okres) + "#gmail"
    if request.form.get("akcja") == "usun":
        gmail.usun_dane()
        flash("Usunięto dane dostępu do Gmaila.", "ok")
        return redirect(wroc)
    login = request.form.get("login", "").strip()
    haslo = "".join(request.form.get("haslo", "").split())     # Google pokazuje hasło w grupach ze spacjami
    if "@" not in login or len(haslo) < 8:
        flash("Podaj adres Gmail i 16-znakowe hasło do aplikacji.", "err")
        return redirect(wroc)
    try:
        gmail.polacz(login, haslo).logout()
    except gmail.BladGmail as e:
        flash(str(e), "err")
        return redirect(wroc)
    gmail.zapisz_dane(login, haslo)
    flash("Połączono ze skrzynką Gmail.", "ok")
    return redirect(wroc)


@bp.post("/kreator/wystaw")
def kreator_wystaw():
    b, _ = wybrany_budynek()
    okres = _okres()
    if b is None or okres is None:
        return redirect(url_for(".kreator"))
    db = get_db()
    nowe = 0
    wybrane = set(request.form.getlist("umowa", type=int))
    for u in umowy_budynku(b["id"], okres):
        if u["id"] in wybrane and not u["tylko_informacja"] and not db.execute("SELECT 1 FROM naliczenia WHERE umowa_id=? AND okres_czynsz=?", (u["id"], okres)).fetchone():
            wystaw(u, okres)
            nowe += 1
    db.commit()
    flash(f"Wystawiono zawiadomienia: {nowe}." if nowe else "Nie było nic do wystawienia.", "ok")
    if nowe:
        powiadom_zawiadomienia(okres)
    return redirect(url_for(".kreator", b=b["id"], okres=okres) + "#wystaw")


# ---------- powiadomienia e-mail ----------
def _liczba_txt(v):
    return f"{float(v):g}".replace(".", ",")


def _zl(gr):
    return f"{gr / 100:,.2f} zł".replace(",", " ").replace(".", ",")


def powiadom_faktura(f):
    b = budynek(int(f["budynek_id"]))
    co = "MWiO — woda i ścieki" if f["rodzaj"] == "woda" else "OPEC — ciepło"
    jedn = JEDN[f["rodzaj"]]
    tresc = (f"Dodano fakturę {co} za {fmt_okres(f['okres'])} ({b['nazwa'] if b else ''}).\n\n"
             f"Numer: {f['numer']}\nKwota brutto: {_zl(f['kwota'])}\nZużycie: {_liczba_txt(f['zuzycie'])} {jedn}\n")
    if f["rodzaj"] == "cieplo" and f["oplata_stala"]:
        tresc += f"Opłaty stałe: {_zl(f['oplata_stala'])}\n"
    if f["rodzaj"] == "woda" and f["odliczenie"]:
        tresc += f"Odliczenie (woda ogrodowa): {_zl(f['odliczenie'])}\n"
    tresc += f"\n{request.url_root.rstrip('/')}{url_for('.faktury')}\n"
    gmail.powiadom(f"Faktura {co.split(' — ')[0]} za {fmt_okres(f['okres'])}: {_zl(f['kwota'])}", tresc)


def powiadom_zawiadomienia(okres):
    rows = get_db().execute("""SELECT l.nazwa, n.do_zaplaty FROM naliczenia n JOIN umowy u ON u.id=n.umowa_id
                               JOIN lokale l ON l.id=u.lokal_id WHERE n.okres_czynsz=? ORDER BY l.nazwa""", (okres,)).fetchall()
    tresc = f"Wystawiono zawiadomienia za {fmt_okres(okres)}:\n\n" + "".join(
        f"  {r['nazwa']}: {_zl(r['do_zaplaty'])}\n" for r in rows)
    tresc += f"\nRazem: {_zl(sum(r['do_zaplaty'] for r in rows))}\n\n"
    tresc += f"{request.url_root.rstrip('/')}{url_for('.rozliczenia', okres=okres)}\n"
    gmail.powiadom(f"Zawiadomienia za {fmt_okres(okres)} wystawione", tresc)
