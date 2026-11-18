"""Wiadomości i usterki: korespondencja najemcy z zarządcą w wątkach (temat + rozmowa).

Wątek ma rodzaj:
- 'wiadomosc' — zwykła sprawa (pytanie, informacja); zarządca ją zamyka, odpowiedź najemcy otwiera ponownie;
- 'usterka'   — zgłoszenie usterki: lokal, kategoria, pilność, zdjęcia i status naprawy
                (nowe → przyjęte → umówiona naprawa → naprawione / odrzucone), termin wizyty i koszt.
                Koszt po stronie wynajmującego trafia do Wydatków (a przez nie do Podatku).

Zmiany w obsłudze zgłoszenia zapisują się w rozmowie jako komunikaty systemowe — najemca widzi je jak nowe
wiadomości. Każda strona widzi, co jest dla niej nowe: w wątku pamiętamy id ostatniej obejrzanej wiadomości."""
from datetime import date, datetime, timedelta

from flask import abort, flash, g, redirect, render_template, request, send_file, url_for

from admin import Form, bp
from app import MIESIACE, app, check_csrf, fmt_zl, get_db, login_required
from pomiary import ZDJECIA, _zapisz_zdjecie

TEMAT_MAX = 120
TRESC_MAX = 5000
LIMIT_NA_GODZINE = 20          # ochrona przed zalaniem skrzynki zarządcy
ZDJEC_MAX = 4                  # zdjęć w jednej wiadomości

KATEGORIE_USTEREK = ["hydraulika", "elektryka", "ogrzewanie", "sprzęt AGD", "okna i drzwi", "inne"]
STATUSY_USTERKI = {"nowe": "nowe", "przyjete": "przyjęte", "umowiona": "umówiona naprawa",
                   "naprawione": "naprawione", "odrzucone": "odrzucone"}
ZAMYKAJACE = {"naprawione", "odrzucone"}
KTO_PLACI = {"wynajmujacy": "wynajmujący", "najemca": "najemca"}
KATEGORIA_WYDATKU = "naprawa / remont"


def _teraz():
    return datetime.now().isoformat(" ", "seconds")


@app.template_filter("czas")
def fmt_czas(s):
    """'2026-11-18 14:05:00' → 'dziś, 14:05' / 'wczoraj, 14:05' / '3 listopada 2026, 14:05'."""
    if not s:
        return "—"
    t = datetime.fromisoformat(s)
    d, godz = t.date(), t.strftime("%H:%M")
    if d == date.today():
        return f"dziś, {godz}"
    if d == date.today() - timedelta(days=1):
        return f"wczoraj, {godz}"
    if d == date.today() + timedelta(days=1):
        return f"jutro, {godz}"
    return f"{d.day} {MIESIACE[d.month - 1]} {d.year}, {godz}"


# globalne (nie context_processor): makra z _watek.html są importowane bez kontekstu
app.jinja_env.globals.update(statusy_usterki=STATUSY_USTERKI, kategorie_usterek=KATEGORIE_USTEREK,
                             kto_placi_opcje=KTO_PLACI)


# ---------- zapytania ----------
WATKI_SQL = """
SELECT w.*, l.nazwa AS lokal_nazwa, k.login AS najemca_login, coalesce(k.full_name, k.login) AS najemca,
       (SELECT tresc FROM wiadomosci WHERE watek_id=w.id ORDER BY id DESC LIMIT 1) AS ost_tresc,
       (SELECT od_zarzadcy FROM wiadomosci WHERE watek_id=w.id ORDER BY id DESC LIMIT 1) AS ost_od_zarzadcy,
       (SELECT systemowa FROM wiadomosci WHERE watek_id=w.id ORDER BY id DESC LIMIT 1) AS ost_systemowa,
       (SELECT count(*) FROM wiadomosci m WHERE m.watek_id=w.id AND m.od_zarzadcy={strona_obca}
           AND m.id > w.{przecz}) AS nowe,
       (SELECT count(*) FROM wiadomosci WHERE watek_id=w.id AND systemowa=0) AS liczba
FROM watki w JOIN users k ON k.id=w.najemca_id LEFT JOIN lokale l ON l.id=w.lokal_id
"""
SQL_NAJEMCA = WATKI_SQL.format(strona_obca=1, przecz="przecz_najemca")
SQL_ZARZADCA = WATKI_SQL.format(strona_obca=0, przecz="przecz_zarzadca")


def nowe_dla(user):
    """Liczba nieprzeczytanych wiadomości od drugiej strony."""
    if user["role"] == "zarzadca":
        q, args = ("SELECT count(*) FROM wiadomosci m JOIN watki w ON w.id=m.watek_id "
                   "WHERE m.od_zarzadcy=0 AND m.id > w.przecz_zarzadca"), ()
    else:
        q, args = ("SELECT count(*) FROM wiadomosci m JOIN watki w ON w.id=m.watek_id "
                   "WHERE w.najemca_id=? AND m.od_zarzadcy=1 AND m.id > w.przecz_najemca"), (user["id"],)
    return get_db().execute(q, args).fetchone()[0]


@app.context_processor
def _ctx_wiadomosci():
    u = g.get("user")
    if u is None:
        return {"nowe_wiadomosci": 0}
    out = {"nowe_wiadomosci": nowe_dla(u)}
    if request.endpoint == "pulpit":                 # pulpit najemcy: zgłoszenia w toku
        out["usterki_w_toku"] = get_db().execute(
            SQL_NAJEMCA + " WHERE w.najemca_id=? AND w.rodzaj='usterka' AND w.zamkniety=0 ORDER BY w.zmieniony DESC",
            (u["id"],)).fetchall()
    elif request.endpoint == "zarzadca.pulpit":
        r = get_db().execute("SELECT count(*), coalesce(sum(pilna),0) FROM watki "
                             "WHERE rodzaj='usterka' AND zamkniety=0").fetchone()
        out["usterki_otwarte"], out["usterki_pilne"] = r[0], r[1]
    elif request.endpoint == "zarzadca.lokal_edycja" and request.view_args:
        out["usterki_lokalu"] = get_db().execute(
            SQL_ZARZADCA + " WHERE w.rodzaj='usterka' AND w.lokal_id=? ORDER BY w.utworzony DESC",
            (request.view_args.get("lid"),)).fetchall()
    return out


def _wiadomosci(wid):
    db = get_db()
    msgs = [dict(m) for m in db.execute(
        """SELECT m.*, coalesce(u.full_name, u.login) AS autor FROM wiadomosci m JOIN users u ON u.id=m.autor_id
           WHERE m.watek_id=? ORDER BY m.id""", (wid,))]
    zal = {}
    for z in db.execute("""SELECT z.id, z.wiadomosc_id FROM zalaczniki z JOIN wiadomosci m ON m.id=z.wiadomosc_id
                           WHERE m.watek_id=? ORDER BY z.id""", (wid,)):
        zal.setdefault(z["wiadomosc_id"], []).append(z["id"])
    for m in msgs:
        m["zdjecia"] = zal.get(m["id"], [])
    return msgs


def _oznacz_przeczytane(wid, kolumna):
    get_db().execute(f"""UPDATE watki SET {kolumna} = coalesce((SELECT max(id) FROM wiadomosci WHERE watek_id=?), 0)
                         WHERE id=?""", (wid, wid))
    get_db().commit()


def _tresc_z_formularza(errors, wymagana=True):
    tresc = request.form.get("tresc", "").replace("\r\n", "\n").strip()
    if not tresc and wymagana:
        errors["tresc"] = "Wpisz treść wiadomości."
    elif len(tresc) > TRESC_MAX:
        errors["tresc"] = f"Wiadomość jest za długa (maks. {TRESC_MAX} znaków)."
    return tresc


def _temat_z_formularza(errors):
    temat = " ".join(request.form.get("temat", "").split())
    if not temat:
        errors["temat"] = "Wpisz temat."
    elif len(temat) > TEMAT_MAX:
        errors["temat"] = f"Temat jest za długi (maks. {TEMAT_MAX} znaków)."
    return temat


def _zdjecia_z_formularza(errors):
    """Zapisuje przesłane zdjęcia (JPG/PNG) i zwraca nazwy plików."""
    pliki = [p for p in request.files.getlist("zdjecia") if p and p.filename]
    if len(pliki) > ZDJEC_MAX:
        errors["zdjecia"] = f"Można dołączyć najwyżej {ZDJEC_MAX} zdjęcia."
        return []
    if errors:
        return []
    nazwy = []
    try:
        for p in pliki:
            nazwy.append(_zapisz_zdjecie(p))
    except ValueError as e:
        for n in nazwy:
            (ZDJECIA / n).unlink(missing_ok=True)
        errors["zdjecia"] = str(e)
        return []
    return nazwy


def _dopisz(wid, tresc, od_zarzadcy, systemowa=False, zdjecia=()):
    """Dodaje wiadomość do wątku; nadawca ma ją od razu jako przeczytaną."""
    db, teraz = get_db(), _teraz()
    mid = db.execute(
        "INSERT INTO wiadomosci (watek_id, autor_id, od_zarzadcy, tresc, czas, systemowa) VALUES (?,?,?,?,?,?)",
        (wid, g.user["id"], 1 if od_zarzadcy else 0, tresc, teraz, 1 if systemowa else 0)).lastrowid
    for plik in zdjecia:
        db.execute("INSERT INTO zalaczniki (wiadomosc_id, plik) VALUES (?,?)", (mid, plik))
    kol = "przecz_zarzadca" if od_zarzadcy else "przecz_najemca"
    db.execute(f"UPDATE watki SET zmieniony=?, {kol}=? WHERE id=?", (teraz, mid, wid))
    return mid


def _za_duzo(uid):
    od = (datetime.now() - timedelta(hours=1)).isoformat(" ", "seconds")
    n = get_db().execute("SELECT count(*) FROM wiadomosci WHERE autor_id=? AND systemowa=0 AND czas > ?",
                         (uid, od)).fetchone()[0]
    return n >= LIMIT_NA_GODZINE


def _nowy_watek(najemca_id, lokal_id, temat, tresc, od_zarzadcy, usterka=None, zdjecia=()):
    """usterka = {'kategoria': ..., 'pilna': 0/1} dla zgłoszenia usterki, None dla wiadomości."""
    teraz = _teraz()
    u = usterka or {}
    wid = get_db().execute(
        """INSERT INTO watki (najemca_id, lokal_id, temat, utworzony, zmieniony, rodzaj, kategoria, pilna, status)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (najemca_id, lokal_id, temat, teraz, teraz, "usterka" if usterka else "wiadomosc",
         u.get("kategoria"), u.get("pilna", 0), "nowe" if usterka else None)).lastrowid
    _dopisz(wid, tresc, od_zarzadcy, zdjecia=zdjecia)
    get_db().commit()
    return wid


def _usterka_z_formularza(errors):
    kat = request.form.get("kategoria", "")
    if kat not in KATEGORIE_USTEREK:
        errors["kategoria"] = "Wybierz rodzaj usterki."
    return {"kategoria": kat, "pilna": 1 if request.form.get("pilna") else 0}


@app.get("/zalacznik/<int:zid>")
def zalacznik(zid):
    """Zdjęcie z wiadomości — dla zarządcy i najemcy, którego dotyczy wątek."""
    if g.user is None:
        abort(404)
    z = get_db().execute("""SELECT z.plik, w.najemca_id FROM zalaczniki z JOIN wiadomosci m ON m.id=z.wiadomosc_id
                            JOIN watki w ON w.id=m.watek_id WHERE z.id=?""", (zid,)).fetchone()
    if z is None or (g.user["role"] != "zarzadca" and z["najemca_id"] != g.user["id"]):
        abort(404)
    plik = ZDJECIA / z["plik"]
    if not plik.is_file() or plik.parent != ZDJECIA:
        abort(404)
    return send_file(plik, mimetype="image/png" if plik.suffix == ".png" else "image/jpeg", max_age=3600)


# ---------- panel najemcy ----------
def _lokale_najemcy(uid):
    return get_db().execute(
        """SELECT DISTINCT l.id, l.nazwa FROM lokale l JOIN umowy u ON u.lokal_id=l.id
           WHERE u.najemca_id=? ORDER BY l.nazwa""", (uid,)).fetchall()


@app.get("/panel/zgloszenia")                         # dawna pozycja menu „Zgłoszenia i usterki”
@login_required
def zgloszenia():
    return redirect(url_for("wiadomosci", rodzaj="usterka"))


@app.route("/panel/wiadomosci", methods=["GET", "POST"])
@login_required
def wiadomosci():
    uid = g.user["id"]
    lokale = _lokale_najemcy(uid)
    errors, form = {}, {"rodzaj": "usterka" if request.args.get("rodzaj") == "usterka" else "wiadomosc"}
    if request.method == "POST":
        check_csrf()
        rodzaj = "usterka" if request.form.get("rodzaj") == "usterka" else "wiadomosc"
        temat, tresc = _temat_z_formularza(errors), _tresc_z_formularza(errors)
        lokal_id = request.form.get("lokal_id", type=int)
        if lokal_id not in {l["id"] for l in lokale}:
            lokal_id = lokale[0]["id"] if len(lokale) == 1 else None
        usterka = None
        if rodzaj == "usterka":
            usterka = _usterka_z_formularza(errors)
            if lokal_id is None:
                errors["lokal_id"] = "Wybierz lokal, którego dotyczy usterka."
        if not errors and _za_duzo(uid):
            errors["tresc"] = "Wysłano już dużo wiadomości w ostatniej godzinie — spróbuj później."
        zdjecia = _zdjecia_z_formularza(errors)
        if not errors:
            wid = _nowy_watek(uid, lokal_id, temat, tresc, od_zarzadcy=False, usterka=usterka, zdjecia=zdjecia)
            flash("Zgłoszenie przekazane zarządcy." if usterka else "Wiadomość wysłana do zarządcy.", "ok")
            return redirect(url_for("wiadomosc", wid=wid))
        form = {"rodzaj": rodzaj, "temat": temat, "tresc": tresc, "lokal_id": lokal_id,
                "kategoria": request.form.get("kategoria"), "pilna": bool(request.form.get("pilna"))}
    watki = get_db().execute(SQL_NAJEMCA + " WHERE w.najemca_id=? ORDER BY w.zamkniety, w.zmieniony DESC, w.id DESC",
                             (uid,)).fetchall()
    return render_template("wiadomosci.html", active="wiadomosci", watki=watki, lokale=lokale,
                           errors=errors, form=form, nowy=bool(errors) or not watki or "rodzaj" in request.args,
                           temat_max=TEMAT_MAX, tresc_max=TRESC_MAX, zdjec_max=ZDJEC_MAX)


@app.route("/panel/wiadomosci/<int:wid>", methods=["GET", "POST"])
@login_required
def wiadomosc(wid):
    w = get_db().execute(SQL_NAJEMCA + " WHERE w.id=? AND w.najemca_id=?", (wid, g.user["id"])).fetchone()
    if w is None:                                    # najemca widzi tylko swoje wątki
        abort(404)
    errors, tresc = {}, ""
    if request.method == "POST":
        check_csrf()
        pliki = [p for p in request.files.getlist("zdjecia") if p and p.filename]
        tresc = _tresc_z_formularza(errors, wymagana=not pliki)     # samo zdjęcie też jest odpowiedzią
        if not errors and _za_duzo(g.user["id"]):
            errors["tresc"] = "Wysłano już dużo wiadomości w ostatniej godzinie — spróbuj później."
        zdjecia = _zdjecia_z_formularza(errors)
        if not errors:
            _dopisz(wid, tresc, od_zarzadcy=False, zdjecia=zdjecia)
            if w["rodzaj"] == "wiadomosc":
                get_db().execute("UPDATE watki SET zamkniety=0 WHERE id=?", (wid,))   # odpowiedź otwiera sprawę
            elif w["zamkniety"] and request.form.get("ponownie"):
                get_db().execute("UPDATE watki SET zamkniety=0, status='przyjete' WHERE id=?", (wid,))
                _dopisz(wid, "Najemca zgłosił, że usterka nadal występuje — zgłoszenie otwarte ponownie.",
                        od_zarzadcy=False, systemowa=True)
            get_db().commit()
            return redirect(url_for("wiadomosc", wid=wid) + "#ostatnia")
    nowe_od = w["przecz_najemca"]
    _oznacz_przeczytane(wid, "przecz_najemca")
    return render_template("watek.html", active="wiadomosci", w=w, msgs=_wiadomosci(wid), nowe_od=nowe_od,
                           errors=errors, tresc=tresc, tresc_max=TRESC_MAX, zdjec_max=ZDJEC_MAX, moje_od_zarzadcy=0,
                           wstecz=url_for("wiadomosci"), akcja=url_for("wiadomosc", wid=wid))


# ---------- panel zarządcy ----------
STATUSY = {"otwarte": "Otwarte", "zamkniete": "Zamknięte", "wszystkie": "Wszystkie"}
RODZAJE = {"": "Wiadomości i usterki", "usterka": "Tylko usterki", "wiadomosc": "Tylko wiadomości"}


def _najemcy():
    """Aktywni klienci z lokalem bieżącej (albo ostatniej) umowy — do wyboru adresata."""
    dzis = date.today().isoformat()
    return get_db().execute(
        """SELECT k.id, coalesce(k.full_name, k.login) AS nazwa,
                  (SELECT u.lokal_id FROM umowy u WHERE u.najemca_id=k.id
                   ORDER BY (u.data_od <= ? AND coalesce(u.rozwiazana_dnia, u.data_do, '9999-12-31') >= ?) DESC,
                            u.data_od DESC LIMIT 1) AS lokal_id,
                  (SELECT l.nazwa FROM umowy u JOIN lokale l ON l.id=u.lokal_id WHERE u.najemca_id=k.id
                   ORDER BY (u.data_od <= ? AND coalesce(u.rozwiazana_dnia, u.data_do, '9999-12-31') >= ?) DESC,
                            u.data_od DESC LIMIT 1) AS lokal_nazwa
           FROM users k WHERE k.role='klient' AND k.aktywny=1 ORDER BY nazwa""",
        (dzis, dzis, dzis, dzis)).fetchall()


@bp.route("/wiadomosci", methods=["GET", "POST"], endpoint="wiadomosci")
def zarzadca_wiadomosci():
    najemcy = _najemcy()
    errors, form = {}, {}
    if request.method == "POST":                      # CSRF sprawdza _guard blueprintu
        rodzaj = "usterka" if request.form.get("rodzaj") == "usterka" else "wiadomosc"
        temat, tresc = _temat_z_formularza(errors), _tresc_z_formularza(errors)
        do = next((k for k in najemcy if k["id"] == request.form.get("najemca_id", type=int)), None)
        if do is None:
            errors["najemca_id"] = "Wybierz najemcę."
        usterka = _usterka_z_formularza(errors) if rodzaj == "usterka" else None
        if usterka and do is not None and not do["lokal_id"]:
            errors["najemca_id"] = "Ten najemca nie ma lokalu — usterkę można zapisać tylko przy lokalu."
        zdjecia = _zdjecia_z_formularza(errors)
        if not errors:
            wid = _nowy_watek(do["id"], do["lokal_id"], temat, tresc, od_zarzadcy=True, usterka=usterka,
                              zdjecia=zdjecia)
            flash(f"Zgłoszenie zapisane: {do['nazwa']}." if usterka else f"Wiadomość wysłana do: {do['nazwa']}.", "ok")
            return redirect(url_for("zarzadca.wiadomosc", wid=wid))
        form = {"rodzaj": rodzaj, "temat": temat, "tresc": tresc, "najemca_id": request.form.get("najemca_id", type=int),
                "kategoria": request.form.get("kategoria"), "pilna": bool(request.form.get("pilna"))}
    elif request.args.get("do", type=int):           # „Napisz” z karty klienta
        form = {"najemca_id": request.args.get("do", type=int)}

    status = request.args.get("status", "otwarte")
    if status not in STATUSY:
        status = "otwarte"
    rodzaj = request.args.get("rodzaj", "")
    if rodzaj not in RODZAJE:
        rodzaj = ""
    k = request.args.get("k", type=int)
    sql, args = SQL_ZARZADCA + " WHERE 1=1", []
    if status == "otwarte":
        sql += " AND (w.zamkniety=0 OR EXISTS (SELECT 1 FROM wiadomosci m WHERE m.watek_id=w.id "\
               "AND m.od_zarzadcy=0 AND m.id > w.przecz_zarzadca))"
    elif status == "zamkniete":
        sql += " AND w.zamkniety=1"
    if rodzaj:
        sql += " AND w.rodzaj=?"
        args.append(rodzaj)
    if k:
        sql += " AND w.najemca_id=?"
        args.append(k)
    watki = get_db().execute(sql + " ORDER BY nowe > 0 DESC, (w.pilna AND NOT w.zamkniety) DESC, "
                                   "w.zmieniony DESC, w.id DESC", args).fetchall()
    return render_template("admin/wiadomosci.html", active="wiadomosci", watki=watki, najemcy=najemcy,
                           status=status, statusy=STATUSY, rodzaj=rodzaj, rodzaje=RODZAJE, k=k,
                           errors=errors, form=form, nowy=bool(errors) or bool(form),
                           temat_max=TEMAT_MAX, tresc_max=TRESC_MAX, zdjec_max=ZDJEC_MAX)


@bp.route("/wiadomosci/<int:wid>", methods=["GET", "POST"], endpoint="wiadomosc")
def zarzadca_wiadomosc(wid):
    w = get_db().execute(SQL_ZARZADCA + " WHERE w.id=?", (wid,)).fetchone()
    if w is None:
        abort(404)
    errors, tresc = {}, ""
    if request.method == "POST":
        pliki = [p for p in request.files.getlist("zdjecia") if p and p.filename]
        tresc = _tresc_z_formularza(errors, wymagana=not pliki)
        zdjecia = _zdjecia_z_formularza(errors)
        if not errors:
            _dopisz(wid, tresc, od_zarzadcy=True, zdjecia=zdjecia)
            zamknij = "zamknij" in request.form
            if w["rodzaj"] == "usterka":
                if zamknij and w["status"] not in ZAMYKAJACE:
                    _zmien_usterke(w, {"status": "naprawione"})
            else:
                get_db().execute("UPDATE watki SET zamkniety=? WHERE id=?", (1 if zamknij else 0, wid))
            get_db().commit()
            if zamknij:
                flash("Odpowiedź wysłana, sprawa zamknięta.", "ok")
                return redirect(url_for("zarzadca.wiadomosci"))
            return redirect(url_for("zarzadca.wiadomosc", wid=wid) + "#ostatnia")
    nowe_od = w["przecz_zarzadca"]
    _oznacz_przeczytane(wid, "przecz_zarzadca")
    wydatek = get_db().execute("SELECT * FROM wydatki WHERE id=?", (w["wydatek_id"],)).fetchone() \
        if w["wydatek_id"] else None
    return render_template("admin/watek.html", active="wiadomosci", w=w, msgs=_wiadomosci(wid), nowe_od=nowe_od,
                           errors=errors, tresc=tresc, tresc_max=TRESC_MAX, zdjec_max=ZDJEC_MAX, moje_od_zarzadcy=1,
                           wydatek=wydatek, f=_usterka_form_pusty(w),
                           wstecz=url_for("zarzadca.wiadomosci"), akcja=url_for("zarzadca.wiadomosc", wid=wid))


@bp.post("/wiadomosci/<int:wid>/status")
def wiadomosc_status(wid):
    """Zamknięcie / ponowne otwarcie zwykłej wiadomości."""
    if get_db().execute("SELECT 1 FROM watki WHERE id=? AND rodzaj='wiadomosc'", (wid,)).fetchone() is None:
        abort(404)
    zamknij = request.form.get("zamkniety") == "1"
    get_db().execute("UPDATE watki SET zamkniety=? WHERE id=?", (1 if zamknij else 0, wid))
    get_db().commit()
    flash("Sprawa zamknięta." if zamknij else "Sprawa ponownie otwarta.", "ok")
    return redirect(url_for("zarzadca.wiadomosci") if zamknij else url_for("zarzadca.wiadomosc", wid=wid))


# ---------- obsługa zgłoszenia usterki ----------
def _usterka_form_pusty(w):
    f = Form({})
    if w["rodzaj"] == "usterka":
        f.raw = {"status": w["status"], "wizyta": w["wizyta"] or "", "kategoria": w["kategoria"],
                 "pilna": w["pilna"], "kto_placi": w["kto_placi"] or "wynajmujacy",
                 "koszt": "" if w["koszt"] is None else f"{w['koszt'] // 100},{w['koszt'] % 100:02d}"}
    return f


def _wizyta_txt(s):
    return fmt_czas(s.replace("T", " ") + (":00" if len(s) == 16 else "")) if s else None


def _zmien_usterke(w, nowe):
    """Zapisuje zmiany zgłoszenia, księguje koszt w Wydatkach i dopisuje komunikat dla najemcy."""
    db = get_db()
    stan = {k: w[k] for k in ("status", "wizyta", "koszt", "kto_placi", "kategoria", "pilna")}
    stan.update(nowe)
    zmiany = []
    if stan["status"] != w["status"]:
        zmiany.append(f"Status: {STATUSY_USTERKI[stan['status']]}")
    if (stan["wizyta"] or None) != (w["wizyta"] or None):
        zmiany.append(f"Termin wizyty: {_wizyta_txt(stan['wizyta'])}" if stan["wizyta"] else "Termin wizyty odwołany")
    if stan["kategoria"] != w["kategoria"]:
        zmiany.append(f"Rodzaj usterki: {stan['kategoria']}")
    if stan["pilna"] != w["pilna"]:
        zmiany.append("Oznaczone jako pilne" if stan["pilna"] else "Zgłoszenie nie jest już oznaczone jako pilne")
    # koszt pokazujemy najemcy tylko wtedy, gdy to on płaci
    kn_stary = w["koszt"] if w["kto_placi"] == "najemca" else None
    kn_nowy = stan["koszt"] if stan["kto_placi"] == "najemca" else None
    if kn_nowy != kn_stary:
        zmiany.append(f"Koszt naprawy po stronie najemcy: {fmt_zl(kn_nowy)}" if kn_nowy
                      else "Koszt naprawy nie obciąża najemcy")

    # Wydatki: koszt wynajmującego = wydatek „naprawa / remont” przy lokalu
    wydatek_id = w["wydatek_id"]
    if wydatek_id and not db.execute("SELECT 1 FROM wydatki WHERE id=?", (wydatek_id,)).fetchone():
        wydatek_id = None                           # usunięty ręcznie w Wydatkach
    if stan["koszt"] and stan["kto_placi"] == "wynajmujacy":
        if wydatek_id:
            db.execute("UPDATE wydatki SET kwota=? WHERE id=?", (stan["koszt"], wydatek_id))
        else:
            dzien = (stan["wizyta"] or "")[:10]
            dzien = dzien if dzien and dzien <= date.today().isoformat() else date.today().isoformat()
            bud = db.execute("SELECT budynek_id FROM lokale WHERE id=?", (w["lokal_id"],)).fetchone()
            wydatek_id = db.execute(
                "INSERT INTO wydatki (data, opis, kwota, kategoria, budynek_id, lokal_id) VALUES (?,?,?,?,?,?)",
                (dzien, f"Naprawa: {w['temat']}"[:300], stan["koszt"], KATEGORIA_WYDATKU,
                 bud["budynek_id"] if bud else None, w["lokal_id"])).lastrowid
    do_usuniecia = None
    if wydatek_id and not (stan["koszt"] and stan["kto_placi"] == "wynajmujacy"):
        do_usuniecia, wydatek_id = wydatek_id, None

    db.execute("""UPDATE watki SET status=?, wizyta=?, koszt=?, kto_placi=?, kategoria=?, pilna=?, wydatek_id=?,
                  zamkniety=? WHERE id=?""",
               (stan["status"], stan["wizyta"] or None, stan["koszt"], stan["kto_placi"], stan["kategoria"],
                stan["pilna"], wydatek_id, 1 if stan["status"] in ZAMYKAJACE else 0, w["id"]))
    if do_usuniecia:                                # koszt już nie po stronie wynajmującego
        db.execute("DELETE FROM wydatki WHERE id=?", (do_usuniecia,))
    if zmiany:
        _dopisz(w["id"], " · ".join(zmiany), od_zarzadcy=True, systemowa=True)
    return zmiany


@bp.post("/wiadomosci/<int:wid>/usterka")
def usterka_zapisz(wid):
    w = get_db().execute("SELECT * FROM watki WHERE id=? AND rodzaj='usterka'", (wid,)).fetchone()
    if w is None:
        abort(404)
    f = Form(request.form)
    status = f.choice("status", "Status", list(STATUSY_USTERKI))
    kategoria = f.choice("kategoria", "Rodzaj usterki", KATEGORIE_USTEREK)
    kto = f.choice("kto_placi", "Kto płaci", list(KTO_PLACI))
    koszt = f.money("koszt", "Koszt naprawy", positive=True) if f.raw.get("koszt", "").strip() else None
    wizyta = (request.form.get("wizyta") or "").strip()
    if wizyta:
        try:
            datetime.fromisoformat(wizyta)
            wizyta = wizyta[:16]
        except ValueError:
            f.error("wizyta", "Podaj datę i godzinę wizyty.")
    if status == "umowiona" and not wizyta:
        f.error("wizyta", "Przy umówionej naprawie podaj termin wizyty.")
    if not f.ok:
        flash("Nie zapisano: " + " ".join(f.errors.values()), "err")
        return redirect(url_for("zarzadca.wiadomosc", wid=wid) + "#obsluga")
    zmiany = _zmien_usterke(w, {"status": status, "kategoria": kategoria, "kto_placi": kto, "koszt": koszt,
                                "wizyta": wizyta or None, "pilna": 1 if request.form.get("pilna") else 0})
    get_db().commit()
    flash("Zapisano: " + "; ".join(zmiany) if zmiany else "Zapisano.", "ok")
    return redirect(url_for("zarzadca.wiadomosc", wid=wid) + "#obsluga")
