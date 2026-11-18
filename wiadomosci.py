"""Wiadomości: korespondencja najemcy z zarządcą w wątkach (temat + rozmowa).

Wątek zakłada najemca (do zarządcy) albo zarządca (do wybranego najemcy). Każda strona widzi,
co jest dla niej nowe — w wątku pamiętamy id ostatniej wiadomości, którą dana strona już oglądała.
Zarządca może wątek zamknąć (sprawa załatwiona); odpowiedź najemcy otwiera go ponownie."""
from datetime import date, datetime, timedelta

from flask import abort, flash, g, redirect, render_template, request, url_for

from admin import bp
from app import MIESIACE, app, check_csrf, get_db, login_required

TEMAT_MAX = 120
TRESC_MAX = 5000
LIMIT_NA_GODZINE = 20          # ochrona przed zalaniem skrzynki zarządcy


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
    return f"{d.day} {MIESIACE[d.month - 1]} {d.year}, {godz}"


# ---------- zapytania ----------
WATKI_SQL = """
SELECT w.*, l.nazwa AS lokal_nazwa, k.login AS najemca_login, coalesce(k.full_name, k.login) AS najemca,
       (SELECT tresc FROM wiadomosci WHERE watek_id=w.id ORDER BY id DESC LIMIT 1) AS ost_tresc,
       (SELECT od_zarzadcy FROM wiadomosci WHERE watek_id=w.id ORDER BY id DESC LIMIT 1) AS ost_od_zarzadcy,
       (SELECT count(*) FROM wiadomosci m WHERE m.watek_id=w.id AND m.od_zarzadcy={strona_obca}
           AND m.id > w.{przecz}) AS nowe,
       (SELECT count(*) FROM wiadomosci WHERE watek_id=w.id) AS liczba
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
    return {"nowe_wiadomosci": nowe_dla(u) if u is not None else 0}


def _wiadomosci(wid):
    return get_db().execute(
        """SELECT m.*, coalesce(u.full_name, u.login) AS autor FROM wiadomosci m JOIN users u ON u.id=m.autor_id
           WHERE m.watek_id=? ORDER BY m.id""", (wid,)).fetchall()


def _oznacz_przeczytane(wid, kolumna):
    get_db().execute(f"""UPDATE watki SET {kolumna} = coalesce((SELECT max(id) FROM wiadomosci WHERE watek_id=?), 0)
                         WHERE id=?""", (wid, wid))
    get_db().commit()


def _tresc_z_formularza(errors):
    tresc = request.form.get("tresc", "").replace("\r\n", "\n").strip()
    if not tresc:
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


def _dopisz(wid, tresc, od_zarzadcy):
    """Dodaje wiadomość do wątku; nadawca ma ją od razu jako przeczytaną."""
    db, teraz = get_db(), _teraz()
    mid = db.execute("INSERT INTO wiadomosci (watek_id, autor_id, od_zarzadcy, tresc, czas) VALUES (?,?,?,?,?)",
                     (wid, g.user["id"], 1 if od_zarzadcy else 0, tresc, teraz)).lastrowid
    kol = "przecz_zarzadca" if od_zarzadcy else "przecz_najemca"
    db.execute(f"UPDATE watki SET zmieniony=?, {kol}=? WHERE id=?", (teraz, mid, wid))
    return mid


def _za_duzo(uid):
    od = (datetime.now() - timedelta(hours=1)).isoformat(" ", "seconds")
    n = get_db().execute("SELECT count(*) FROM wiadomosci WHERE autor_id=? AND czas > ?", (uid, od)).fetchone()[0]
    return n >= LIMIT_NA_GODZINE


def _nowy_watek(najemca_id, lokal_id, temat, tresc, od_zarzadcy):
    teraz = _teraz()
    wid = get_db().execute(
        "INSERT INTO watki (najemca_id, lokal_id, temat, utworzony, zmieniony) VALUES (?,?,?,?,?)",
        (najemca_id, lokal_id, temat, teraz, teraz)).lastrowid
    _dopisz(wid, tresc, od_zarzadcy)
    get_db().commit()
    return wid


# ---------- panel najemcy ----------
def _lokale_najemcy(uid):
    return get_db().execute(
        """SELECT DISTINCT l.id, l.nazwa FROM lokale l JOIN umowy u ON u.lokal_id=l.id
           WHERE u.najemca_id=? ORDER BY l.nazwa""", (uid,)).fetchall()


@app.route("/panel/wiadomosci", methods=["GET", "POST"])
@login_required
def wiadomosci():
    uid = g.user["id"]
    lokale = _lokale_najemcy(uid)
    errors, form = {}, {}
    if request.method == "POST":
        check_csrf()
        temat, tresc = _temat_z_formularza(errors), _tresc_z_formularza(errors)
        lokal_id = request.form.get("lokal_id", type=int)
        if lokal_id not in {l["id"] for l in lokale}:
            lokal_id = lokale[0]["id"] if len(lokale) == 1 else None
        if not errors and _za_duzo(uid):
            errors["tresc"] = "Wysłano już dużo wiadomości w ostatniej godzinie — spróbuj później."
        if not errors:
            wid = _nowy_watek(uid, lokal_id, temat, tresc, od_zarzadcy=False)
            flash("Wiadomość wysłana do zarządcy.", "ok")
            return redirect(url_for("wiadomosc", wid=wid))
        form = {"temat": temat, "tresc": tresc, "lokal_id": lokal_id}
    watki = get_db().execute(SQL_NAJEMCA + " WHERE w.najemca_id=? ORDER BY w.zmieniony DESC, w.id DESC",
                             (uid,)).fetchall()
    return render_template("wiadomosci.html", active="wiadomosci", watki=watki, lokale=lokale,
                           errors=errors, form=form, nowy=bool(errors) or not watki,
                           temat_max=TEMAT_MAX, tresc_max=TRESC_MAX)


@app.route("/panel/wiadomosci/<int:wid>", methods=["GET", "POST"])
@login_required
def wiadomosc(wid):
    w = get_db().execute(SQL_NAJEMCA + " WHERE w.id=? AND w.najemca_id=?", (wid, g.user["id"])).fetchone()
    if w is None:                                    # najemca widzi tylko swoje wątki
        abort(404)
    errors, tresc = {}, ""
    if request.method == "POST":
        check_csrf()
        tresc = _tresc_z_formularza(errors)
        if not errors and _za_duzo(g.user["id"]):
            errors["tresc"] = "Wysłano już dużo wiadomości w ostatniej godzinie — spróbuj później."
        if not errors:
            _dopisz(wid, tresc, od_zarzadcy=False)
            get_db().execute("UPDATE watki SET zamkniety=0 WHERE id=?", (wid,))   # odpowiedź otwiera sprawę
            get_db().commit()
            return redirect(url_for("wiadomosc", wid=wid) + "#ostatnia")
    nowe_od = w["przecz_najemca"]
    _oznacz_przeczytane(wid, "przecz_najemca")
    return render_template("watek.html", active="wiadomosci", w=w, msgs=_wiadomosci(wid), nowe_od=nowe_od,
                           errors=errors, tresc=tresc, tresc_max=TRESC_MAX, moje_od_zarzadcy=0,
                           wstecz=url_for("wiadomosci"), akcja=url_for("wiadomosc", wid=wid))


# ---------- panel zarządcy ----------
STATUSY = {"otwarte": "Otwarte", "zamkniete": "Zamknięte", "wszystkie": "Wszystkie"}


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
        temat, tresc = _temat_z_formularza(errors), _tresc_z_formularza(errors)
        do = next((k for k in najemcy if k["id"] == request.form.get("najemca_id", type=int)), None)
        if do is None:
            errors["najemca_id"] = "Wybierz najemcę."
        if not errors:
            wid = _nowy_watek(do["id"], do["lokal_id"], temat, tresc, od_zarzadcy=True)
            flash(f"Wiadomość wysłana do: {do['nazwa']}.", "ok")
            return redirect(url_for("zarzadca.wiadomosc", wid=wid))
        form = {"temat": temat, "tresc": tresc, "najemca_id": request.form.get("najemca_id", type=int)}
    elif request.args.get("do", type=int):           # „Napisz” z karty klienta
        form = {"najemca_id": request.args.get("do", type=int)}

    status = request.args.get("status", "otwarte")
    if status not in STATUSY:
        status = "otwarte"
    k = request.args.get("k", type=int)
    sql, args = SQL_ZARZADCA + " WHERE 1=1", []
    if status == "otwarte":
        sql += " AND (w.zamkniety=0 OR EXISTS (SELECT 1 FROM wiadomosci m WHERE m.watek_id=w.id "\
               "AND m.od_zarzadcy=0 AND m.id > w.przecz_zarzadca))"
    elif status == "zamkniete":
        sql += " AND w.zamkniety=1"
    if k:
        sql += " AND w.najemca_id=?"
        args.append(k)
    watki = get_db().execute(sql + " ORDER BY nowe > 0 DESC, w.zmieniony DESC, w.id DESC", args).fetchall()
    return render_template("admin/wiadomosci.html", active="wiadomosci", watki=watki, najemcy=najemcy,
                           status=status, statusy=STATUSY, k=k, errors=errors, form=form,
                           nowy=bool(errors) or bool(form), temat_max=TEMAT_MAX, tresc_max=TRESC_MAX)


@bp.route("/wiadomosci/<int:wid>", methods=["GET", "POST"], endpoint="wiadomosc")
def zarzadca_wiadomosc(wid):
    w = get_db().execute(SQL_ZARZADCA + " WHERE w.id=?", (wid,)).fetchone()
    if w is None:
        abort(404)
    errors, tresc = {}, ""
    if request.method == "POST":
        tresc = _tresc_z_formularza(errors)
        if not errors:
            _dopisz(wid, tresc, od_zarzadcy=True)
            zamknij = "zamknij" in request.form
            get_db().execute("UPDATE watki SET zamkniety=? WHERE id=?", (1 if zamknij else 0, wid))
            get_db().commit()
            if zamknij:
                flash("Odpowiedź wysłana, sprawa zamknięta.", "ok")
                return redirect(url_for("zarzadca.wiadomosci"))
            return redirect(url_for("zarzadca.wiadomosc", wid=wid) + "#ostatnia")
    nowe_od = w["przecz_zarzadca"]
    _oznacz_przeczytane(wid, "przecz_zarzadca")
    return render_template("admin/watek.html", active="wiadomosci", w=w, msgs=_wiadomosci(wid), nowe_od=nowe_od,
                           errors=errors, tresc=tresc, tresc_max=TRESC_MAX, moje_od_zarzadcy=1,
                           wstecz=url_for("zarzadca.wiadomosci"), akcja=url_for("zarzadca.wiadomosc", wid=wid))


@bp.post("/wiadomosci/<int:wid>/status")
def wiadomosc_status(wid):
    if get_db().execute("SELECT 1 FROM watki WHERE id=?", (wid,)).fetchone() is None:
        abort(404)
    zamknij = request.form.get("zamkniety") == "1"
    get_db().execute("UPDATE watki SET zamkniety=? WHERE id=?", (1 if zamknij else 0, wid))
    get_db().commit()
    flash("Sprawa zamknięta." if zamknij else "Sprawa ponownie otwarta.", "ok")
    return redirect(url_for("zarzadca.wiadomosci") if zamknij else url_for("zarzadca.wiadomosc", wid=wid))
