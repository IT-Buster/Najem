"""Panel zarządcy: lokale, umowy (z aneksami) i klienci."""
import re
import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation

from flask import (Blueprint, abort, flash, g, redirect, render_template,
                   request, url_for)
from werkzeug.security import generate_password_hash

from app import check_csrf, get_db, manager_required, status_umowy

bp = Blueprint("zarzadca", __name__, url_prefix="/zarzadca")

ADMIN_MENU = [
    ("pulpit", "Pulpit"),
    ("budynki", "Budynki"),
    ("lokale", "Lokale"),
    ("umowy", "Umowy"),
    ("klienci", "Klienci"),
    ("rozliczenia", "Rozliczenia"),
    ("liczniki", "Odczyty liczników"),
    ("faktury", "Faktury mediów"),
    ("wplaty", "Wpłaty"),
    ("podatek", "Podatek"),
    ("wydatki", "Wydatki"),
    ("ustawienia", "Ustawienia"),
]
RODZAJE_LOKALI = ["mieszkanie", "kawalerka", "pokój", "dom", "lokal użytkowy", "garaż / miejsce postojowe"]
RODZAJE_UMOW = ["najem okazjonalny", "najem zwykły", "najem instytucjonalny", "najem lokalu użytkowego"]


@bp.before_request
@manager_required
def _guard():
    if request.method == "POST":
        check_csrf()


@bp.context_processor
def _ctx():
    return {"admin_menu": ADMIN_MENU, "rodzaje_lokali": RODZAJE_LOKALI,
            "rodzaje_umow": RODZAJE_UMOW, "opcje": lambda xs: [(x, x) for x in xs],
            "today": date.today().isoformat()}


@bp.app_template_filter("zl_input")
def zl_input(grosze):
    if grosze in (None, ""):
        return ""
    if isinstance(grosze, str):
        return grosze
    zl, gr = divmod(int(grosze), 100)
    return f"{zl},{gr:02d}"


# ---------- walidacja formularzy ----------
class Form:
    """Zbiera wartości i błędy pól; wartości 'surowe' wracają do formularza."""

    def __init__(self, data):
        self.raw = {k: (v.strip() if isinstance(v, str) else v) for k, v in data.items()}
        self.errors = {}
        self.clean = {}

    def text(self, name, label, required=False, max_len=500):
        v = self.raw.get(name, "") or ""
        if required and not v:
            self.errors[name] = f"Pole „{label}” jest wymagane."
        elif len(v) > max_len:
            self.errors[name] = f"Maksymalnie {max_len} znaków."
        self.clean[name] = v or None
        return self.clean[name]

    def choice(self, name, label, options):
        v = self.raw.get(name, "")
        if v not in options:
            self.errors[name] = f"Wybierz wartość pola „{label}”."
        self.clean[name] = v
        return v

    def date(self, name, label, required=True):
        v = self.raw.get(name, "") or ""
        if not v:
            if required:
                self.errors[name] = f"Podaj datę: {label.lower()}."
            self.clean[name] = None
            return None
        try:
            self.clean[name] = date.fromisoformat(v).isoformat()
        except ValueError:
            self.errors[name] = "Nieprawidłowa data."
            self.clean[name] = None
        return self.clean[name]

    def money(self, name, label, required=True, positive=False):
        v = (self.raw.get(name, "") or "").replace(" ", "").replace(" ", "")
        v = v.replace("zł", "").replace(",", ".")
        if not v:
            if required:
                self.errors[name] = f"Podaj kwotę: {label.lower()}."
            self.clean[name] = 0
            return 0
        try:
            d = Decimal(v)
            if d != d.quantize(Decimal("0.01")) or d < 0 or d > 10_000_000:
                raise InvalidOperation
            gr = int(d * 100)
            if positive and gr == 0:
                raise InvalidOperation
        except InvalidOperation:
            self.errors[name] = "Nieprawidłowa kwota (np. 3200 lub 3 200,50)."
            gr = 0
        self.clean[name] = gr
        return gr

    def number(self, name, label, lo, hi, required=False, integer=True):
        v = (self.raw.get(name, "") or "").replace(",", ".")
        if not v:
            if required:
                self.errors[name] = f"Pole „{label}” jest wymagane."
            self.clean[name] = None
            return None
        try:
            n = int(v) if integer else float(v)
            if not lo <= n <= hi:
                raise ValueError
        except ValueError:
            self.errors[name] = f"Podaj liczbę od {lo} do {hi}."
            n = None
        self.clean[name] = n
        return n

    def error(self, name, msg):
        self.errors.setdefault(name, msg)

    @property
    def ok(self):
        return not self.errors


def _insert(table, data):
    cols = ",".join(data)
    q = ",".join("?" * len(data))
    return get_db().execute(f"INSERT INTO {table} ({cols}) VALUES ({q})", list(data.values())).lastrowid


def _update(table, id_, data):
    sets = ",".join(f"{c}=?" for c in data)
    get_db().execute(f"UPDATE {table} SET {sets} WHERE id=?", [*data.values(), id_])


def _get_or_404(table, id_):
    row = get_db().execute(f"SELECT * FROM {table} WHERE id=?", (id_,)).fetchone()
    if row is None:
        abort(404)
    return row


def koniec(u):
    return u["rozwiazana_dnia"] or u["data_do"]


# ---------- pulpit ----------
UMOWY_SQL = """
SELECT u.*, l.nazwa AS lokal_nazwa, l.ulica, l.nr_lokalu, l.kod, l.miasto,
       k.login AS najemca_login, k.full_name AS najemca_nazwa
FROM umowy u
JOIN lokale l ON l.id = u.lokal_id
JOIN users k ON k.id = u.najemca_id
"""


@bp.get("/")
def pulpit():
    db = get_db()
    umowy = db.execute(UMOWY_SQL).fetchall()
    statusy = [(u, status_umowy(u)) for u in umowy]
    aktywne = [u for u, s in statusy if s[0] in ("aktywna", "konczy")]
    konczace = sorted([u for u, s in statusy if s[0] == "konczy"], key=koniec)
    przyszle = sorted([u for u, s in statusy if s[0] == "przyszla"], key=lambda u: u["data_od"])
    lokale_n = db.execute("SELECT count(*) FROM lokale").fetchone()[0]
    zajete = {u["lokal_id"] for u in aktywne}
    klienci_n = db.execute("SELECT count(*) FROM users WHERE role='klient' AND aktywny=1").fetchone()[0]
    przychod = sum(u["czynsz"] + u["oplaty"] for u in aktywne)
    return render_template(
        "admin/pulpit.html", active="pulpit",
        lokale_n=lokale_n, wolne_n=lokale_n - len(zajete), aktywne_n=len(aktywne),
        klienci_n=klienci_n, przychod=przychod, konczace=konczace, przyszle=przyszle)


# ---------- lokale ----------
@bp.get("/lokale")
def lokale():
    db = get_db()
    q = request.args.get("q", "").strip()
    sql = "SELECT * FROM lokale"
    args = []
    if q:
        sql += " WHERE nazwa LIKE ? OR ulica LIKE ? OR miasto LIKE ?"
        args = [f"%{q}%"] * 3
    rows = db.execute(sql + " ORDER BY nazwa", args).fetchall()
    umowy = db.execute(UMOWY_SQL).fetchall()
    biezaca = {}
    for u in umowy:
        s = status_umowy(u)[0]
        if s in ("aktywna", "konczy"):
            biezaca[u["lokal_id"]] = u
    return render_template("admin/lokale.html", active="lokale", rows=rows, biezaca=biezaca, q=q)


def _lokal_form(f):
    f.text("nazwa", "Nazwa", required=True, max_len=120)
    bid = f.number("budynek_id", "Budynek", 1, 10**9)
    if bid and not get_db().execute("SELECT 1 FROM budynki WHERE id=?", (bid,)).fetchone():
        f.error("budynek_id", "Wybierz budynek.")
    f.choice("rodzaj", "Rodzaj", RODZAJE_LOKALI)
    f.text("ulica", "Ulica i numer budynku", required=True, max_len=200)
    f.text("nr_lokalu", "Nr lokalu", max_len=20)
    kod = f.text("kod", "Kod pocztowy", required=True, max_len=6)
    if kod and not re.fullmatch(r"\d{2}-\d{3}", kod):
        f.error("kod", "Kod pocztowy w formacie 00-000.")
    f.text("miasto", "Miasto", required=True, max_len=100)
    f.number("powierzchnia", "Powierzchnia", 1, 10000, integer=False)
    f.number("pokoje", "Liczba pokoi", 0, 50)
    f.text("pietro", "Piętro", max_len=50)
    f.text("nr_wodomierza", "Nr wodomierza", max_len=50)
    f.text("nr_cieplomierza", "Nr ciepłomierza", max_len=50)
    f.text("opis", "Opis", max_len=4000)
    f.text("wyposazenie", "Wyposażenie", max_len=4000)
    return f


@bp.route("/lokale/nowy", methods=["GET", "POST"])
def lokal_nowy():
    if request.method == "POST":
        f = _lokal_form(Form(request.form))
        if f.ok:
            lid = _insert("lokale", f.clean)
            get_db().commit()
            flash(f"Dodano lokal „{f.clean['nazwa']}”.", "ok")
            return redirect(url_for(".lokal_edycja", lid=lid))
        return render_template("admin/lokal_form.html", active="lokale", f=f, l=None), 422
    pre = {"rodzaj": "mieszkanie", "miasto": "Warszawa"}
    b = get_db().execute("SELECT * FROM budynki WHERE id=?", (request.args.get("budynek", type=int),)).fetchone()
    if b:
        pre.update(budynek_id=str(b["id"]), ulica=b["ulica"] or "", kod=b["kod"] or "", miasto=b["miasto"] or "")
    f = Form(pre)
    return render_template("admin/lokal_form.html", active="lokale", f=f, l=None)


@bp.route("/lokale/<int:lid>", methods=["GET", "POST"])
def lokal_edycja(lid):
    l = _get_or_404("lokale", lid)
    umowy = get_db().execute(UMOWY_SQL + " WHERE u.lokal_id=? ORDER BY u.data_od DESC", (lid,)).fetchall()
    if request.method == "POST":
        f = _lokal_form(Form(request.form))
        if f.ok:
            _update("lokale", lid, f.clean)
            get_db().commit()
            flash("Zapisano zmiany.", "ok")
            return redirect(url_for(".lokal_edycja", lid=lid))
        return render_template("admin/lokal_form.html", active="lokale", f=f, l=l, umowy=umowy), 422
    f = Form({k: ("" if l[k] is None else str(l[k]) if not isinstance(l[k], float) else f"{l[k]:g}".replace(".", ","))
              for k in l.keys()})
    return render_template("admin/lokal_form.html", active="lokale", f=f, l=l, umowy=umowy)


@bp.post("/lokale/<int:lid>/usun")
def lokal_usun(lid):
    l = _get_or_404("lokale", lid)
    db = get_db()
    if db.execute("SELECT 1 FROM umowy WHERE lokal_id=?", (lid,)).fetchone():
        flash("Nie można usunąć lokalu, który ma umowy.", "err")
        return redirect(url_for(".lokal_edycja", lid=lid))
    db.execute("DELETE FROM lokale WHERE id=?", (lid,))
    db.commit()
    flash(f"Usunięto lokal „{l['nazwa']}”.", "ok")
    return redirect(url_for(".lokale"))


# ---------- umowy ----------
STATUSY = [("", "Wszystkie"), ("aktywna", "Aktywne"), ("konczy", "Kończące się"),
           ("przyszla", "Przyszłe"), ("zakonczona", "Zakończone")]


@bp.get("/umowy")
def umowy():
    st = request.args.get("status", "")
    q = request.args.get("q", "").strip()
    sql, args = UMOWY_SQL, []
    if q:
        sql += " WHERE u.numer LIKE ? OR l.nazwa LIKE ? OR k.full_name LIKE ? OR k.login LIKE ?"
        args = [f"%{q}%"] * 4
    rows = get_db().execute(sql + " ORDER BY u.data_od DESC", args).fetchall()
    if st:
        rows = [u for u in rows if status_umowy(u)[0] == st]
    return render_template("admin/umowy.html", active="umowy", rows=rows,
                           statusy=STATUSY, st=st, q=q)


def _opcje():
    db = get_db()
    return {
        "lokale_opcje": db.execute("SELECT id, nazwa, ulica, nr_lokalu FROM lokale ORDER BY nazwa").fetchall(),
        "klienci_opcje": db.execute(
            "SELECT id, login, full_name FROM users WHERE role='klient' AND aktywny=1 ORDER BY coalesce(full_name, login)"
        ).fetchall(),
    }


def _umowa_form(f, uid=None):
    db = get_db()
    numer = f.text("numer", "Numer umowy", required=True, max_len=50)
    if numer and db.execute("SELECT 1 FROM umowy WHERE numer=? AND id IS NOT ?", (numer, uid)).fetchone():
        f.error("numer", "Umowa o takim numerze już istnieje.")
    lid = f.number("lokal_id", "Lokal", 1, 10**9, required=True)
    if lid and not db.execute("SELECT 1 FROM lokale WHERE id=?", (lid,)).fetchone():
        f.error("lokal_id", "Wybierz lokal.")
    kid = f.number("najemca_id", "Najemca", 1, 10**9, required=True)
    if kid and not db.execute("SELECT 1 FROM users WHERE id=? AND role='klient'", (kid,)).fetchone():
        f.error("najemca_id", "Wybierz najemcę.")
    f.choice("rodzaj", "Rodzaj umowy", RODZAJE_UMOW)
    f.date("data_zawarcia", "Data zawarcia")
    od = f.date("data_od", "Początek najmu")
    do = f.date("data_do", "Koniec najmu", required=False)
    rozw = f.date("rozwiazana_dnia", "Data rozwiązania", required=False)
    if od and do and do < od:
        f.error("data_do", "Koniec najmu nie może być przed początkiem.")
    if od and rozw and rozw < od:
        f.error("rozwiazana_dnia", "Data rozwiązania nie może być przed początkiem najmu.")
    if rozw and do and rozw > do:
        f.error("rozwiazana_dnia", "Data rozwiązania nie może być po końcu umowy.")
    f.money("czynsz", "Czynsz", positive=True)
    f.money("oplaty", "Opłaty", required=False)
    f.money("kaucja", "Kaucja", required=False)
    f.number("dzien_platnosci", "Dzień płatności", 1, 31, required=True)
    f.number("liczba_osob", "Liczba osób", 1, 20, required=True)
    f.text("wypowiedzenie", "Okres wypowiedzenia", max_len=100)
    f.text("uwagi", "Uwagi", max_len=4000)
    # jeden lokal nie może mieć dwóch umów w tym samym czasie
    if f.ok:
        nowy_koniec = rozw or do or "9999-12-31"
        for o in db.execute("SELECT * FROM umowy WHERE lokal_id=? AND id IS NOT ?", (lid, uid)):
            o_koniec = koniec(o) or "9999-12-31"
            if od <= o_koniec and o["data_od"] <= nowy_koniec:
                f.error("data_od", f"Okres nakłada się na umowę {o['numer']} dla tego lokalu.")
                break
    return f


# ---------- podpowiedzi dla nowej umowy ----------
PREFIKSY = {"najem okazjonalny": "NO", "najem zwykły": "NZ",
            "najem instytucjonalny": "NI", "najem lokalu użytkowego": "NU"}


def nastepny_numer(rodzaj, rok=None):
    """Kolejny wolny numer w danym roku, np. NO/2026/003."""
    rok = rok or date.today().year
    pfx = f"{PREFIKSY.get(rodzaj, 'UM')}/{rok}/"
    nr = 0
    for (n,) in get_db().execute("SELECT numer FROM umowy WHERE numer LIKE ?", (pfx + "%",)):
        tail = n[len(pfx):]
        if tail.isdigit():
            nr = max(nr, int(tail))
    return f"{pfx}{nr + 1:03d}"


def rok_pozniej(iso):
    """Koniec umowy rocznej: ten sam dzień za rok minus jeden dzień (np. 1.12 → 30.11)."""
    d = date.fromisoformat(iso)
    try:
        d2 = d.replace(year=d.year + 1)
    except ValueError:              # 29 lutego
        d2 = d.replace(year=d.year + 1, day=28)
    return date.fromordinal(d2.toordinal() - 1).isoformat()


def ostatnie_kwoty(lokal_id):
    """Kwoty z ostatniej umowy tego lokalu — jako propozycja."""
    if not lokal_id:
        return None
    return get_db().execute(
        "SELECT czynsz, oplaty, kaucja, dzien_platnosci, wypowiedzenie FROM umowy"
        " WHERE lokal_id=? ORDER BY data_od DESC LIMIT 1", (lokal_id,)).fetchone()


@bp.get("/umowy/sugestie")
def umowa_sugestie():
    rodzaj = request.args.get("rodzaj", "")
    rok = request.args.get("rok", "")
    out = {"numer": nastepny_numer(rodzaj, int(rok) if rok.isdigit() else None)}
    od = request.args.get("od", "")
    try:
        out["data_do"] = rok_pozniej(od)
    except ValueError:
        pass
    k = ostatnie_kwoty(request.args.get("lokal", type=int))
    if k:
        out.update(czynsz=zl_input(k["czynsz"]), oplaty=zl_input(k["oplaty"]),
                   kaucja=zl_input(k["kaucja"]), dzien_platnosci=str(k["dzien_platnosci"]),
                   wypowiedzenie=k["wypowiedzenie"] or "")
    return out


@bp.route("/umowy/nowa", methods=["GET", "POST"])
def umowa_nowa():
    if request.method == "POST":
        form = request.form.to_dict()
        if not form.get("numer", "").strip():      # pusty numer → nadaj automatycznie
            zaw = form.get("data_zawarcia", "")
            rok = int(zaw[:4]) if zaw[:4].isdigit() else None
            form["numer"] = nastepny_numer(form.get("rodzaj", ""), rok)
        f = _umowa_form(Form(form))
        if f.ok:
            uid = _insert("umowy", f.clean)
            get_db().commit()
            flash(f"Dodano umowę {f.clean['numer']}.", "ok")
            return redirect(url_for(".umowa_edycja", uid=uid))
        return render_template("admin/umowa_form.html", active="umowy", f=f, u=None, **_opcje()), 422
    dzis = date.today().isoformat()
    pre = {"rodzaj": "najem okazjonalny", "dzien_platnosci": "10", "wypowiedzenie": "1 miesiąc", "liczba_osob": "1",
           "data_zawarcia": dzis, "data_od": dzis, "data_do": rok_pozniej(dzis),
           "numer": nastepny_numer("najem okazjonalny"),
           "lokal_id": request.args.get("lokal", ""), "najemca_id": request.args.get("klient", "")}
    k = ostatnie_kwoty(request.args.get("lokal", type=int))
    if k:
        pre.update(czynsz=zl_input(k["czynsz"]), oplaty=zl_input(k["oplaty"]), kaucja=zl_input(k["kaucja"]),
                   dzien_platnosci=str(k["dzien_platnosci"]), wypowiedzenie=k["wypowiedzenie"] or "")
    return render_template("admin/umowa_form.html", active="umowy", f=Form(pre), u=None, **_opcje())


@bp.route("/umowy/<int:uid>", methods=["GET", "POST"])
def umowa_edycja(uid):
    u = get_db().execute(UMOWY_SQL + " WHERE u.id=?", (uid,)).fetchone()
    if u is None:
        abort(404)
    aneksy = get_db().execute("SELECT * FROM aneksy WHERE umowa_id=? ORDER BY data", (uid,)).fetchall()
    if request.method == "POST":
        f = _umowa_form(Form(request.form), uid)
        if f.ok:
            _update("umowy", uid, f.clean)
            get_db().commit()
            flash("Zapisano zmiany.", "ok")
            return redirect(url_for(".umowa_edycja", uid=uid))
        return render_template("admin/umowa_form.html", active="umowy", f=f, u=u,
                               aneksy=aneksy, **_opcje()), 422
    f = Form({k: ("" if u[k] is None else u[k] if k in ("czynsz", "oplaty", "kaucja") else str(u[k]))
              for k in u.keys()})
    for k in ("czynsz", "oplaty", "kaucja"):
        f.raw[k] = zl_input(u[k])
    return render_template("admin/umowa_form.html", active="umowy", f=f, u=u, aneksy=aneksy, **_opcje())


@bp.post("/umowy/<int:uid>/usun")
def umowa_usun(uid):
    u = _get_or_404("umowy", uid)
    db = get_db()
    db.execute("DELETE FROM aneksy WHERE umowa_id=?", (uid,))
    db.execute("DELETE FROM umowy WHERE id=?", (uid,))
    db.commit()
    flash(f"Usunięto umowę {u['numer']}.", "ok")
    return redirect(url_for(".umowy"))


@bp.post("/umowy/<int:uid>/aneksy")
def aneks_dodaj(uid):
    _get_or_404("umowy", uid)
    f = Form(request.form)
    f.text("numer", "Numer aneksu", required=True, max_len=50)
    f.date("data", "Data aneksu")
    f.text("opis", "Opis zmian", required=True, max_len=4000)
    if f.ok:
        _insert("aneksy", {"umowa_id": uid, **f.clean})
        get_db().commit()
        flash(f"Dodano {f.clean['numer']}.", "ok")
    else:
        flash("Aneks nie został dodany: " + " ".join(f.errors.values()), "err")
    return redirect(url_for(".umowa_edycja", uid=uid) + "#aneksy")


@bp.post("/aneksy/<int:aid>/usun")
def aneks_usun(aid):
    a = _get_or_404("aneksy", aid)
    db = get_db()
    db.execute("DELETE FROM aneksy WHERE id=?", (aid,))
    db.commit()
    flash(f"Usunięto {a['numer']}.", "ok")
    return redirect(url_for(".umowa_edycja", uid=a["umowa_id"]) + "#aneksy")


# ---------- klienci ----------
@bp.get("/klienci")
def klienci():
    q = request.args.get("q", "").strip()
    sql = """SELECT k.*, (SELECT count(*) FROM umowy u WHERE u.najemca_id=k.id) AS umowy_n
             FROM users k WHERE k.role='klient'"""
    args = []
    if q:
        sql += " AND (k.login LIKE ? OR k.full_name LIKE ? OR k.email LIKE ? OR k.telefon LIKE ?)"
        args = [f"%{q}%"] * 4
    rows = get_db().execute(sql + " ORDER BY k.aktywny DESC, coalesce(k.full_name, k.login)", args).fetchall()
    return render_template("admin/klienci.html", active="klienci", rows=rows, q=q)


def _klient_form(f, kid=None):
    login = f.text("login", "Login", required=True, max_len=50)
    if login and not re.fullmatch(r"[A-Za-z0-9._-]{3,50}", login):
        f.error("login", "Login: 3–50 znaków, litery bez polskich znaków, cyfry, . _ -")
    if login and get_db().execute("SELECT 1 FROM users WHERE login=? AND id IS NOT ?", (login, kid)).fetchone():
        f.error("login", "Ten login jest już zajęty.")
    f.text("full_name", "Imię i nazwisko", required=True, max_len=120)
    email = f.text("email", "E-mail", max_len=200)
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        f.error("email", "Nieprawidłowy adres e-mail.")
    f.text("telefon", "Telefon", max_len=30)
    haslo = f.raw.get("haslo", "")
    if kid is None and not haslo:
        f.error("haslo", "Ustaw hasło startowe.")
    if haslo and len(haslo) < 8:
        f.error("haslo", "Hasło musi mieć co najmniej 8 znaków.")
    f.clean.pop("haslo", None)
    if haslo:
        f.clean["password_hash"] = generate_password_hash(haslo)
    return f


@bp.route("/klienci/nowy", methods=["GET", "POST"])
def klient_nowy():
    if request.method == "POST":
        f = _klient_form(Form(request.form))
        if f.ok:
            kid = _insert("users", {**f.clean, "role": "klient"})
            get_db().commit()
            flash(f"Dodano klienta {f.clean['full_name']}.", "ok")
            return redirect(url_for(".klient_edycja", kid=kid))
        f.raw["haslo"] = ""
        return render_template("admin/klient_form.html", active="klienci", f=f, k=None), 422
    return render_template("admin/klient_form.html", active="klienci", f=Form({}), k=None)


@bp.route("/klienci/<int:kid>", methods=["GET", "POST"])
def klient_edycja(kid):
    k = get_db().execute("SELECT * FROM users WHERE id=? AND role='klient'", (kid,)).fetchone()
    if k is None:
        abort(404)
    umowy = get_db().execute(UMOWY_SQL + " WHERE u.najemca_id=? ORDER BY u.data_od DESC", (kid,)).fetchall()
    if request.method == "POST":
        f = _klient_form(Form(request.form), kid)
        if f.ok:
            _update("users", kid, f.clean)
            get_db().commit()
            flash("Zapisano zmiany" + (" i ustawiono nowe hasło." if "password_hash" in f.clean else "."), "ok")
            return redirect(url_for(".klient_edycja", kid=kid))
        f.raw["haslo"] = ""
        return render_template("admin/klient_form.html", active="klienci", f=f, k=k, umowy=umowy), 422
    f = Form({key: ("" if k[key] is None else str(k[key])) for key in ("login", "full_name", "email", "telefon")})
    return render_template("admin/klient_form.html", active="klienci", f=f, k=k, umowy=umowy)


@bp.post("/klienci/<int:kid>/aktywny")
def klient_aktywny(kid):
    k = get_db().execute("SELECT * FROM users WHERE id=? AND role='klient'", (kid,)).fetchone()
    if k is None:
        abort(404)
    nowy = 0 if k["aktywny"] else 1
    get_db().execute("UPDATE users SET aktywny=? WHERE id=?", (nowy, kid))
    get_db().commit()
    flash("Konto odblokowane — klient może się logować." if nowy
          else "Konto zablokowane — klient nie może się zalogować.", "ok")
    return redirect(url_for(".klient_edycja", kid=kid))
