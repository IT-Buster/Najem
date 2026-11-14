"""Pomiary liczników: telemetria (API), odczyty podawane przez najemców (ze zdjęciem), zatwierdzanie
i przenoszenie do odczytów miesięcznych.

Odczyt miesiąca M (tabela odczyty, okres = M) = zatwierdzony pomiar najbliższy chwili
„ostatni dzień miesiąca M−1, 23:59” — w oknie ±7 dni. Telemetria notuje stan dokładnie wtedy;
najemca może podać odczyt kilka dni przed lub po końcu miesiąca. Odczytów wpisanych ręcznie
przez zarządcę (odczyty.zrodlo IS NULL) pomiary nie nadpisują.

API telemetrii:  POST /api/pomiary   nagłówek  Authorization: Bearer <token urządzenia>
  {"licznik": "<nr licznika>", "wartosc": 123.456, "czas": "2026-11-30T23:59:00"}
  albo {"pomiary": [ {...}, {...} ]}.  Licznik szukany po numerze wodomierza/ciepłomierza
  lokalu lub licznika głównego budynku; zamiast numeru można podać "lokal_id" i "rodzaj".
"""
import hashlib
import secrets
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from flask import abort, flash, g, jsonify, redirect, render_template, request, send_file, url_for

from admin import Form, _get_or_404, bp
from app import INSTANCE, app, get_db, login_required
from rozliczenia import (NAZWA_LICZNIKA, biezacy, budynek, glowny_id, nastepny, odczyt, okres_z_args,
                         poprzedni, wybrany_budynek)

OKNO = timedelta(days=7)
ZDJECIA = INSTANCE / "zdjecia"
TYPY_ZDJEC = {b"\xff\xd8\xff": ("jpg", "image/jpeg"), b"\x89PNG": ("png", "image/png")}


# ---------- logika ----------
def granica(okres):
    """Chwila odczytu dla okresu: początek miesiąca okres = ostatni dzień poprzedniego, 23:59."""
    return datetime.fromisoformat(okres + "-01T00:00:00") - timedelta(minutes=1)


def okres_dla(czas):
    """Okres odczytu, którego granica jest najbliżej podanej chwili."""
    o = czas.strftime("%Y-%m")
    kandydaci = [poprzedni(o), o, nastepny(o)]
    return min(kandydaci, key=lambda k: abs(granica(k) - czas))


def w_oknie(czas):
    return abs(granica(okres_dla(czas)) - czas) <= OKNO


def nastepne_okno(teraz=None):
    """(od, do) najbliższego okna podawania odczytów, które jeszcze się nie skończyło."""
    teraz = teraz or datetime.now()
    o = okres_dla(teraz)
    if granica(o) + OKNO < teraz:
        o = nastepny(o)
    g_ = granica(o)
    return (g_ - OKNO).date(), (g_ + OKNO).date()


def kandydat(lokal_id, rodzaj, okres):
    g_ = granica(okres)
    od, do = (g_ - OKNO).isoformat(), (g_ + OKNO).isoformat()
    rows = get_db().execute(
        """SELECT * FROM pomiary WHERE lokal_id=? AND rodzaj=? AND status='zatwierdzony' AND czas BETWEEN ? AND ?""",
        (lokal_id, rodzaj, od, do)).fetchall()
    if not rows:
        return None
    # najbliższy granicy; przy remisie wcześniejszy, potem nowszy wpis
    return min(rows, key=lambda r: (abs(datetime.fromisoformat(r["czas"]) - g_), r["czas"], -r["id"]))


def przenies(lokal_id, okres):
    """Ustawia odczyt lokalu za okres z pomiarów. Zwraca True, gdy coś zmieniono."""
    db = get_db()
    ist = odczyt(lokal_id, okres)
    if ist is not None and ist["zrodlo"] is None:
        return False                                      # ręczny odczyt zarządcy ma pierwszeństwo
    nowe = {}
    for k in ("woda", "cieplo"):
        p = kandydat(lokal_id, k, okres)
        if p:
            nowe[k] = p
    if not nowe:
        return False
    woda = nowe["woda"]["wartosc"] if "woda" in nowe else (ist["woda"] if ist else None)
    cieplo = nowe["cieplo"]["wartosc"] if "cieplo" in nowe else (ist["cieplo"] if ist else None)
    data = min(p["czas"] for p in nowe.values())[:10]
    if ist and ist["woda"] == woda and ist["cieplo"] == cieplo:
        return False
    db.execute("""INSERT INTO odczyty (lokal_id, okres, data, cieplo, woda, zrodlo) VALUES (?,?,?,?,?, 'pomiar')
                  ON CONFLICT(lokal_id, okres) DO UPDATE SET data=excluded.data, cieplo=excluded.cieplo,
                  woda=excluded.woda, zrodlo='pomiar'""", (lokal_id, okres, data, cieplo, woda))
    return True


def przenies_okres(okres):
    """Przenosi pomiary z okna wokół końca miesiąca do odczytów za okres (wszystkie lokale)."""
    g_ = granica(okres)
    ids = [r[0] for r in get_db().execute(
        "SELECT DISTINCT lokal_id FROM pomiary WHERE status='zatwierdzony' AND czas BETWEEN ? AND ?",
        ((g_ - OKNO).isoformat(), (g_ + OKNO).isoformat()))]
    n = sum(1 for lid in ids if przenies(lid, okres))
    get_db().commit()
    return n


def auto_przenies():
    """Wywoływane przy wejściu na odczyty/rozliczenia: domyka bieżący i poprzedni miesiąc."""
    for o in (poprzedni(biezacy()), biezacy()):
        przenies_okres(o)


@app.cli.command("przenies-pomiary")
def przenies_cmd():
    """Przenosi pomiary do odczytów (np. z crona 1. dnia miesiąca o 00:05)."""
    with app.test_request_context():
        for o in (poprzedni(biezacy()), biezacy()):
            print(o, "zmienionych odczytów:", przenies_okres(o))


def lokale_najemcy(uid):
    """Lokale z umową obowiązującą dziś (lub w ostatnich 7 dniach) — w budynku z licznikami."""
    od = (date.today() - OKNO).isoformat()
    return get_db().execute("""
        SELECT DISTINCT l.* FROM lokale l JOIN umowy u ON u.lokal_id=l.id
        WHERE u.najemca_id=? AND u.data_od<=? AND coalesce(u.rozwiazana_dnia, u.data_do, '9999-12-31')>=?
        ORDER BY l.nazwa""", (uid, date.today().isoformat(), od)).fetchall()


def _liczba(v):
    v = (v or "").strip().replace(" ", "").replace(",", ".")
    if not v:
        return None
    try:
        d = Decimal(v)
    except InvalidOperation:
        raise ValueError
    if d < 0 or d > Decimal("1e9"):
        raise ValueError
    return d


def _zapisz_zdjecie(plik):
    if not plik or not plik.filename:
        return None
    dane = plik.read(12 * 1024 * 1024 + 1)
    for magia, (ext, _) in TYPY_ZDJEC.items():
        if dane.startswith(magia):
            ZDJECIA.mkdir(parents=True, exist_ok=True)
            nazwa = f"{uuid.uuid4().hex}.{ext}"
            (ZDJECIA / nazwa).write_bytes(dane)
            return nazwa
    raise ValueError("Zdjęcie musi być w formacie JPG lub PNG.")


# ---------- panel najemcy ----------
@app.post("/panel/media/odczyt")
@login_required
def najemca_odczyt():
    from app import check_csrf
    check_csrf()
    lid = request.form.get("lokal_id", type=int)
    if lid not in {l["id"] for l in lokale_najemcy(g.user["id"])}:
        abort(404)
    try:
        d = datetime.fromisoformat(request.form.get("data", ""))
    except ValueError:
        d = None
    bledy = []
    if d is None or d.date() > date.today() or d.date() < date.today() - timedelta(days=60):
        bledy.append("Podaj datę odczytu (nie z przyszłości, najwyżej 60 dni wstecz).")
    wartosci = {}
    for k in ("woda", "cieplo"):
        try:
            v = _liczba(request.form.get(k))
            if v is not None:
                wartosci[k] = v
        except ValueError:
            bledy.append(f"Nieprawidłowy stan: {NAZWA_LICZNIKA[k]}.")
    if not wartosci and not bledy:
        bledy.append("Podaj stan co najmniej jednego licznika.")
    zdjecie = None
    if not bledy:
        try:
            zdjecie = _zapisz_zdjecie(request.files.get("zdjecie"))
        except ValueError as e:
            bledy.append(str(e))
    if bledy:
        flash(" ".join(bledy), "err")
        return redirect(url_for("media") + "#podaj")
    # godzina: data z formularza + bieżąca godzina, jeśli to dziś
    czas = datetime.combine(d.date(), datetime.now().time() if d.date() == date.today() else datetime.min.time().replace(hour=12))
    db = get_db()
    for k, v in wartosci.items():
        db.execute("""INSERT INTO pomiary (lokal_id, rodzaj, wartosc, czas, zrodlo, status, user_id, zdjecie, uwagi)
                      VALUES (?,?,?,?, 'najemca', 'oczekuje', ?,?,?)""",
                   (lid, k, float(v), czas.replace(microsecond=0).isoformat(), g.user["id"], zdjecie,
                    (request.form.get("uwagi") or "").strip()[:300] or None))
    db.commit()
    flash("Dziękujemy — odczyt przekazany zarządcy do zatwierdzenia.", "ok")
    return redirect(url_for("media"))


def moje_pomiary(uid):
    return get_db().execute("""SELECT p.*, l.nazwa AS lokal FROM pomiary p JOIN lokale l ON l.id=p.lokal_id
                               WHERE p.user_id=? ORDER BY p.czas DESC, p.id DESC LIMIT 20""", (uid,)).fetchall()


@app.context_processor
def _ctx_najemca():
    """Dla strony Liczniki i media: formularz odczytu i historia zgłoszeń najemcy."""
    if request.endpoint != "media" or g.get("user") is None:
        return {}
    od, do = nastepne_okno()
    return {"lokale_odczyt": lokale_najemcy(g.user["id"]), "moje_pomiary": moje_pomiary(g.user["id"]),
            "dzis": date.today().isoformat(), "okno_od": od.isoformat(), "okno_do": do.isoformat(),
            "w_oknie_dzis": od <= date.today() <= do}


@app.get("/zdjecie/<int:pid>")
def zdjecie(pid):
    if g.user is None:
        abort(404)
    p = get_db().execute("SELECT * FROM pomiary WHERE id=?", (pid,)).fetchone()
    if p is None or not p["zdjecie"] or (g.user["role"] != "zarzadca" and p["user_id"] != g.user["id"]):
        abort(404)
    plik = ZDJECIA / p["zdjecie"]
    if not plik.is_file() or plik.parent != ZDJECIA:
        abort(404)
    typ = "image/png" if plik.suffix == ".png" else "image/jpeg"
    return send_file(plik, mimetype=typ, max_age=3600)


# ---------- panel zarządcy ----------
def _nazwy(b):
    out = {glowny_id(b["id"]): "Liczniki główne budynku"}
    for l in get_db().execute("SELECT id, nazwa FROM lokale WHERE budynek_id=?", (b["id"],)):
        out[l["id"]] = l["nazwa"]
    return out


@bp.get("/pomiary")
def pomiary():
    auto_przenies()
    b, bl = wybrany_budynek()
    if b is None:
        return redirect(url_for(".liczniki"))
    db = get_db()
    nazwy = _nazwy(b)
    q = ",".join("?" * len(nazwy))
    oczekujace = []
    for p in db.execute(f"""SELECT p.*, coalesce(u.full_name, u.login) AS kto FROM pomiary p
                            LEFT JOIN users u ON u.id=p.user_id
                            WHERE p.status='oczekuje' AND p.lokal_id IN ({q}) ORDER BY p.czas""", list(nazwy)):
        ok = okres_dla(datetime.fromisoformat(p["czas"]))
        poprz = odczyt(p["lokal_id"], poprzedni(ok))
        oczekujace.append({"p": p, "nazwa": nazwy[p["lokal_id"]], "okres": ok,
                           "poprz": poprz[p["rodzaj"]] if poprz else None,
                           "w_oknie": w_oknie(datetime.fromisoformat(p["czas"]))})
    ostatnie = db.execute(f"""SELECT p.*, coalesce(u.full_name, u.login) AS kto, d.nazwa AS urzadzenie FROM pomiary p
                              LEFT JOIN users u ON u.id=p.user_id LEFT JOIN urzadzenia d ON d.id=p.urzadzenie_id
                              WHERE p.lokal_id IN ({q}) ORDER BY p.czas DESC, p.id DESC LIMIT 60""", list(nazwy)).fetchall()
    urz = db.execute("""SELECT d.*, b.nazwa AS budynek FROM urzadzenia d LEFT JOIN budynki b ON b.id=d.budynek_id
                        ORDER BY d.aktywne DESC, d.nazwa""").fetchall()
    return render_template("admin/pomiary.html", active="liczniki", b=b, budynki=bl, nazwy=nazwy,
                           oczekujace=oczekujace, ostatnie=ostatnie, urzadzenia=urz,
                           api_url=request.url_root.rstrip("/") + url_for("api_pomiary"),
                           biezacy_okres=biezacy())


@bp.post("/pomiary/<int:pid>/<decyzja>")
def pomiar_decyzja(pid, decyzja):
    if decyzja not in ("zatwierdz", "odrzuc"):
        abort(404)
    p = _get_or_404("pomiary", pid)
    db = get_db()
    db.execute("UPDATE pomiary SET status=? WHERE id=?", ("zatwierdzony" if decyzja == "zatwierdz" else "odrzucony", pid))
    zmiana = False
    if decyzja == "zatwierdz":
        zmiana = przenies(p["lokal_id"], okres_dla(datetime.fromisoformat(p["czas"])))
    db.commit()
    if decyzja == "zatwierdz":
        if not w_oknie(datetime.fromisoformat(p["czas"])):
            flash("Zatwierdzono odczyt — zapisany w historii pomiarów. Jest z połowy miesiąca (poza ±7 dni od końca "
                  "miesiąca), więc nie zmienia odczytu do rozliczenia.", "ok")
        else:
            flash("Zatwierdzono odczyt." + (" Zapisany jako odczyt miesiąca." if zmiana else
                                             " Odczyt miesiąca bez zmian (jest ręczny albo bliższy pomiar)."), "ok")
    else:
        flash("Odrzucono odczyt.", "ok")
    return redirect(url_for(".pomiary", b=request.form.get("b") or ""))


@bp.post("/pomiary/przenies")
def pomiary_przenies():
    okres = okres_z_args()
    n = przenies_okres(okres)
    flash(f"Odczyty za {okres}: zaktualizowano {n} z pomiarów.", "ok")
    return redirect(url_for(".liczniki", okres=okres, b=request.form.get("b") or ""))


# urządzenia telemetrii
def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


@bp.post("/pomiary/urzadzenia")
def urzadzenie_nowe():
    nazwa = (request.form.get("nazwa") or "").strip()[:100]
    bid = request.form.get("budynek_id", type=int)
    if not nazwa:
        flash("Podaj nazwę urządzenia.", "err")
        return redirect(url_for(".pomiary", b=request.form.get("b") or ""))
    if bid and budynek(bid) is None:
        bid = None
    token = "nj_" + secrets.token_urlsafe(32)
    get_db().execute("INSERT INTO urzadzenia (nazwa, token_hash, budynek_id) VALUES (?,?,?)", (nazwa, _hash(token), bid))
    get_db().commit()
    flash(f"Dodano urządzenie „{nazwa}”. Token (pokazywany tylko raz — skopiuj go teraz): {token}", "token")
    return redirect(url_for(".pomiary", b=request.form.get("b") or ""))


@bp.post("/pomiary/urzadzenia/<int:did>/<akcja>")
def urzadzenie_akcja(did, akcja):
    d = _get_or_404("urzadzenia", did)
    db = get_db()
    if akcja == "wylacz":
        db.execute("UPDATE urzadzenia SET aktywne=1-aktywne WHERE id=?", (did,))
        flash(f"Urządzenie „{d['nazwa']}” " + ("wyłączone." if d["aktywne"] else "włączone."), "ok")
    elif akcja == "usun":
        db.execute("UPDATE pomiary SET urzadzenie_id=NULL WHERE urzadzenie_id=?", (did,))
        db.execute("DELETE FROM urzadzenia WHERE id=?", (did,))
        flash(f"Usunięto urządzenie „{d['nazwa']}”. Jego pomiary zostały.", "ok")
    else:
        abort(404)
    db.commit()
    return redirect(url_for(".pomiary", b=request.form.get("b") or ""))


# ---------- API telemetrii ----------
def _znajdz_licznik(item, bid):
    """(lokal_id, rodzaj) po numerze licznika albo lokal_id+rodzaj; ograniczone do budynku urządzenia."""
    db = get_db()
    nr = str(item.get("licznik") or "").strip()
    if nr:
        trafienia = []
        for kol, rodzaj in (("nr_wodomierza", "woda"), ("nr_cieplomierza", "cieplo")):
            for r in db.execute(f"SELECT id, budynek_id FROM lokale WHERE {kol}=?", (nr,)):
                trafienia.append((r["id"], rodzaj, r["budynek_id"]))
            for r in db.execute(f"SELECT id FROM budynki WHERE {kol}=?", (nr,)):
                trafienia.append((glowny_id(r["id"]), rodzaj, r["id"]))
        if bid:
            trafienia = [t for t in trafienia if t[2] == bid]
        if len(trafienia) != 1:
            return None, ("nieznany licznik" if not trafienia else "numer licznika niejednoznaczny")
        return (trafienia[0][0], trafienia[0][1]), None
    lid, rodzaj = item.get("lokal_id"), item.get("rodzaj")
    if not isinstance(lid, int) or rodzaj not in ("woda", "cieplo"):
        return None, "podaj 'licznik' albo 'lokal_id' i 'rodzaj'"
    if lid < 0:
        b_ = -lid
    else:
        r = db.execute("SELECT budynek_id FROM lokale WHERE id=?", (lid,)).fetchone()
        if r is None:
            return None, "nieznany lokal"
        b_ = r["budynek_id"]
    if bid and b_ != bid:
        return None, "lokal spoza budynku urządzenia"
    if lid < 0 and budynek(b_) is None:
        return None, "nieznany budynek"
    return (lid, rodzaj), None


def _czas(v):
    if v in (None, ""):
        return datetime.now().replace(microsecond=0)
    t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if t.tzinfo is not None:
        t = t.astimezone().replace(tzinfo=None)              # na czas lokalny serwera
    return t.replace(microsecond=0)


@app.post("/api/pomiary")
def api_pomiary():
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    db = get_db()
    d = db.execute("SELECT * FROM urzadzenia WHERE token_hash=? AND aktywne=1", (_hash(token),)).fetchone() if token else None
    if d is None:
        return jsonify(blad="brak lub nieprawidłowy token"), 401
    dane = request.get_json(silent=True)
    if dane is None:
        return jsonify(blad="oczekiwano JSON"), 400
    items = dane.get("pomiary") if isinstance(dane, dict) and "pomiary" in dane else [dane]
    if not isinstance(items, list) or not items or len(items) > 500:
        return jsonify(blad="od 1 do 500 pomiarów"), 400
    wynik, okresy = [], set()
    teraz = datetime.now() + timedelta(minutes=10)
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            wynik.append({"nr": i, "blad": "pomiar musi być obiektem"})
            continue
        licznik, blad = _znajdz_licznik(item, d["budynek_id"])
        try:
            wartosc = float(item.get("wartosc"))
            if not 0 <= wartosc <= 1e9:
                raise ValueError
        except (TypeError, ValueError):
            blad = blad or "nieprawidłowa wartość"
        try:
            czas = _czas(item.get("czas"))
            if czas > teraz:
                blad = blad or "czas z przyszłości"
        except ValueError:
            blad = blad or "nieprawidłowy czas (ISO 8601)"
        if blad:
            wynik.append({"nr": i, "blad": blad})
            continue
        lid, rodzaj = licznik
        db.execute("""INSERT INTO pomiary (lokal_id, rodzaj, wartosc, czas, zrodlo, status, urzadzenie_id)
                      VALUES (?,?,?,?, 'telemetria', 'zatwierdzony', ?)""", (lid, rodzaj, wartosc, czas.isoformat(), d["id"]))
        okresy.add((lid, okres_dla(czas)))
        wynik.append({"nr": i, "ok": True, "lokal_id": lid, "rodzaj": rodzaj, "czas": czas.isoformat()})
    db.execute("UPDATE urzadzenia SET ostatnio=? WHERE id=?", (datetime.now().replace(microsecond=0).isoformat(), d["id"]))
    for lid, ok in okresy:
        przenies(lid, ok)
    db.commit()
    przyjete = sum(1 for w in wynik if w.get("ok"))
    return jsonify(przyjete=przyjete, odrzucone=len(wynik) - przyjete, wyniki=wynik), (200 if przyjete else 422)


# ---------- użycie w innych widokach ----------
def oczekujace_n(bid=None):
    q = "SELECT count(*) FROM pomiary p LEFT JOIN lokale l ON l.id=p.lokal_id WHERE p.status='oczekuje'"
    args = []
    if bid:
        q += " AND (l.budynek_id=? OR p.lokal_id=?)"
        args = [bid, glowny_id(bid)]
    return get_db().execute(q, args).fetchone()[0]


app.jinja_env.globals["oczekujace_odczyty"] = oczekujace_n
