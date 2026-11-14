"""Najem — aplikacja do obsługi najmu (szkic)."""
import os
import secrets
import sqlite3
from datetime import date
from functools import wraps
from pathlib import Path

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

BASE = Path(__file__).resolve().parent
INSTANCE = BASE / "instance"
DB_PATH = Path(os.environ.get("NAJEM_DB", INSTANCE / "najem.sqlite3"))

# Pozycje menu — na razie tylko szkic, bez implementacji.
MENU = [
    ("pulpit", "Pulpit", "Podsumowanie: saldo, najbliższe płatności, zgłoszenia"),
    ("lokale", "Moje lokale", "Wynajmowane mieszkania / lokale i ich dane"),
    ("umowy", "Umowy", "Umowy najmu, aneksy, terminy"),
    ("platnosci", "Płatności", "Czynsz, opłaty, historia wpłat, zaległości"),
    ("media", "Liczniki i media", "Odczyty liczników, rozliczenia mediów"),
    ("zgloszenia", "Zgłoszenia i usterki", "Zgłaszanie awarii i śledzenie napraw"),
    ("dokumenty", "Dokumenty", "Faktury, protokoły zdawczo-odbiorcze, załączniki"),
    ("wiadomosci", "Wiadomości", "Korespondencja z zarządcą"),
    ("konto", "Moje konto", "Dane kontaktowe, zmiana hasła"),
]
MENU_KEYS = {k for k, _, _ in MENU}


def _secret_key() -> str:
    INSTANCE.mkdir(exist_ok=True)
    f = INSTANCE / "secret_key"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
        f.chmod(0o600)
    return f.read_text().strip()


app = Flask(__name__, instance_path=str(INSTANCE))
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SECRET_KEY=_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("NAJEM_INSECURE_COOKIES") != "1",
)


# ---------- baza danych ----------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    login         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    full_name     TEXT,
    role          TEXT NOT NULL DEFAULT 'klient',
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at TEXT
);

-- Lokale (mieszkania, pokoje, lokale użytkowe)
CREATE TABLE IF NOT EXISTS lokale (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    nazwa       TEXT NOT NULL,
    ulica       TEXT NOT NULL,          -- ulica z numerem budynku
    nr_lokalu   TEXT,
    kod         TEXT NOT NULL,
    miasto      TEXT NOT NULL,
    rodzaj      TEXT NOT NULL DEFAULT 'mieszkanie',
    powierzchnia REAL,                  -- m²
    pokoje      INTEGER,
    pietro      TEXT,
    opis        TEXT,
    wyposazenie TEXT,                   -- pozycje rozdzielone nową linią
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Umowy najmu; kwoty w groszach
CREATE TABLE IF NOT EXISTS umowy (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    numer             TEXT NOT NULL UNIQUE,
    lokal_id          INTEGER NOT NULL REFERENCES lokale(id),
    najemca_id        INTEGER NOT NULL REFERENCES users(id),
    rodzaj            TEXT NOT NULL DEFAULT 'najem okazjonalny',
    data_zawarcia     TEXT NOT NULL,
    data_od           TEXT NOT NULL,
    data_do           TEXT,             -- NULL = czas nieokreślony
    czynsz            INTEGER NOT NULL,
    oplaty            INTEGER NOT NULL DEFAULT 0,  -- opłaty eksploatacyjne / zaliczki
    kaucja            INTEGER NOT NULL DEFAULT 0,
    dzien_platnosci   INTEGER NOT NULL DEFAULT 10,
    wypowiedzenie     TEXT,             -- np. '1 miesiąc'
    rozwiazana_dnia   TEXT,             -- wcześniejsze zakończenie
    uwagi             TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_umowy_najemca ON umowy(najemca_id);
CREATE INDEX IF NOT EXISTS ix_umowy_lokal ON umowy(lokal_id);

CREATE TABLE IF NOT EXISTS login_attempts (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    login TEXT NOT NULL,
    ip    TEXT NOT NULL,
    at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_login_attempts ON login_attempts(at);

-- Budynki: lokale, faktury za media i liczniki główne należą do budynku.
-- Stawki puste (NULL) = wartość domyślna z tabeli ustawienia.
CREATE TABLE IF NOT EXISTS budynki (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    nazwa         TEXT NOT NULL,
    ulica         TEXT,
    kod           TEXT,
    miasto        TEXT,
    swiatlo       INTEGER,          -- grosze / lokal / miesiąc
    smieci_1      INTEGER,
    smieci_2      INTEGER,
    smieci_3      INTEGER,
    smieci_4      INTEGER,
    smieci_5      INTEGER,
    lokale_cieplo INTEGER,          -- na ile lokali dzielić opłatę przesyłową
    rachunek      TEXT,
    uwagi         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------- rozliczenia mediów (wzór: arkusz Najem) ----------
CREATE TABLE IF NOT EXISTS ustawienia (
    klucz   TEXT PRIMARY KEY,
    wartosc TEXT
);

-- Odczyt z początku miesiąca 'okres' (RRRR-MM). lokal_id < 0 → liczniki główne budynku o id = −lokal_id.
CREATE TABLE IF NOT EXISTS odczyty (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    lokal_id  INTEGER NOT NULL,
    okres     TEXT NOT NULL,
    data      TEXT,
    cieplo    REAL,                 -- ciepłomierz, GJ
    woda      REAL,                 -- wodomierz, m³
    UNIQUE (lokal_id, okres)
);

-- Wymiana licznika w trakcie miesiąca zużycia 'okres' (RRRR-MM). Zużycie za ten miesiąc =
-- (stan końcowy starego − odczyt z początku miesiąca) + (odczyt z początku następnego − stan początkowy nowego).
-- lokal_id < 0 → licznik główny budynku o id = −lokal_id.
CREATE TABLE IF NOT EXISTS wymiany (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lokal_id    INTEGER NOT NULL,
    rodzaj      TEXT NOT NULL CHECK (rodzaj IN ('woda', 'cieplo')),
    okres       TEXT NOT NULL,
    data        TEXT NOT NULL,
    stan_stary  REAL NOT NULL,          -- stan końcowy zdejmowanego licznika
    stan_nowy   REAL NOT NULL DEFAULT 0,-- stan początkowy nowego licznika
    nr_stary    TEXT,
    nr_nowy     TEXT,
    uwagi       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (lokal_id, rodzaj, okres)
);

-- Wpłaty podatku (ryczałt od najmu) do urzędu skarbowego; okres = miesiąc przychodu (RRRR-MM)
CREATE TABLE IF NOT EXISTS podatek_wplaty (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    okres      TEXT NOT NULL,
    data       TEXT NOT NULL,
    kwota      INTEGER NOT NULL,     -- grosze
    uwagi      TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Wydatki właściciela: zakupy, naprawy, usługi (rejestr; przy ryczałcie nie obniżają podatku)
CREATE TABLE IF NOT EXISTS wydatki (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    data        TEXT NOT NULL,
    opis        TEXT NOT NULL,
    firma       TEXT,
    dokument    TEXT,                -- nr faktury / paragonu
    kwota       INTEGER NOT NULL,    -- grosze brutto
    kategoria   TEXT,
    budynek_id  INTEGER REFERENCES budynki(id),
    lokal_id    INTEGER REFERENCES lokale(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Faktury dostawców; okres = miesiąc zużycia (RRRR-MM); kwoty brutto w groszach
CREATE TABLE IF NOT EXISTS faktury (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rodzaj        TEXT NOT NULL CHECK (rodzaj IN ('woda', 'cieplo')),
    numer         TEXT NOT NULL,
    okres         TEXT NOT NULL,
    data          TEXT,
    zuzycie       REAL NOT NULL DEFAULT 0,   -- m³ lub GJ
    kwota         INTEGER NOT NULL,
    oplata_stala  INTEGER NOT NULL DEFAULT 0,  -- ciepło: przesyłowa + moc
    odliczenie    INTEGER NOT NULL DEFAULT 0,  -- woda: np. woda ogrodowa
    uwagi         TEXT
);

-- Zawiadomienia o wysokości opłat (zamrożony stan w chwili wystawienia)
CREATE TABLE IF NOT EXISTS naliczenia (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    umowa_id         INTEGER NOT NULL REFERENCES umowy(id),
    okres_czynsz     TEXT NOT NULL,
    okres_media      TEXT NOT NULL,
    data_wystawienia TEXT NOT NULL,
    termin           TEXT,
    suma_pozycji     INTEGER NOT NULL,
    saldo            INTEGER NOT NULL DEFAULT 0,   -- + niedopłata, − nadpłata
    do_zaplaty       INTEGER NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (umowa_id, okres_czynsz)
);
CREATE TABLE IF NOT EXISTS naliczenia_pozycje (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    naliczenie_id INTEGER NOT NULL REFERENCES naliczenia(id) ON DELETE CASCADE,
    lp            INTEGER NOT NULL,
    tytul         TEXT NOT NULL,
    sposob        TEXT,
    kwota         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS wplaty (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    umowa_id   INTEGER NOT NULL REFERENCES umowy(id),
    data       TEXT NOT NULL,
    kwota      INTEGER NOT NULL,
    opis       TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_wplaty_umowa ON wplaty(umowa_id);

CREATE TABLE IF NOT EXISTS aneksy (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    umowa_id  INTEGER NOT NULL REFERENCES umowy(id) ON DELETE CASCADE,
    numer     TEXT NOT NULL,
    data      TEXT NOT NULL,
    opis      TEXT NOT NULL
);
"""


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    INSTANCE.mkdir(exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(SCHEMA)
        # migracje dla istniejących baz
        cols = {r[1] for r in db.execute("PRAGMA table_info(users)")}
        for col, ddl in [("email", "TEXT"), ("telefon", "TEXT"),
                         ("aktywny", "INTEGER NOT NULL DEFAULT 1")]:
            if col not in cols:
                db.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
        for table, col, ddl in [("lokale", "nr_wodomierza", "TEXT"),
                                ("lokale", "nr_cieplomierza", "TEXT"),
                                ("umowy", "liczba_osob", "INTEGER NOT NULL DEFAULT 1"),
                                ("lokale", "budynek_id", "INTEGER REFERENCES budynki(id)"),
                                ("faktury", "budynek_id", "INTEGER REFERENCES budynki(id)")]:
            if col not in {r[1] for r in db.execute(f"PRAGMA table_info({table})")}:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        # dane sprzed budynków: przy jednym budynku przypisz mu faktury i liczniki główne (dawniej lokal_id = 0)
        budynki = [r[0] for r in db.execute("SELECT id FROM budynki")]
        if len(budynki) == 1:
            b = budynki[0]
            db.execute("UPDATE faktury SET budynek_id=? WHERE budynek_id IS NULL", (b,))
            db.execute("UPDATE odczyty SET lokal_id=? WHERE lokal_id=0", (-b,))


def create_user(login, password, full_name=None, role="klient"):
    init_db()
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "INSERT INTO users (login, password_hash, full_name, role) VALUES (?,?,?,?)"
            " ON CONFLICT(login) DO UPDATE SET password_hash=excluded.password_hash, role=excluded.role",
            (login, generate_password_hash(password), full_name, role),
        )


@app.cli.command("init-db")
def init_db_cmd():
    init_db()
    print(f"Baza gotowa: {DB_PATH}")


@app.cli.command("create-user")
def create_user_cmd():
    import click
    login = click.prompt("Login")
    password = click.prompt("Hasło", hide_input=True, confirmation_prompt=True)
    name = click.prompt("Imię i nazwisko", default="", show_default=False) or None
    role = click.prompt("Rola", type=click.Choice(["klient", "zarzadca"]), default="klient")
    create_user(login, password, name, role)
    print(f"Użytkownik {login} zapisany.")


# ---------- CSRF + logowanie ----------
def csrf_token() -> str:
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


def static_v(filename):
    """URL pliku statycznego z wersją (mtime) — proxy cache'uje statyki na wiele godzin."""
    try:
        v = int((BASE / "static" / filename).stat().st_mtime)
    except OSError:
        v = 0
    return url_for("static", filename=filename, v=v)


app.jinja_env.globals["static_v"] = static_v


def _nazwa_serwisu():
    """Nazwa serwisu w tytułach i stopce (np. domena): NAJEM_NAZWA albo plik instance/nazwa_serwisu."""
    f = INSTANCE / "nazwa_serwisu"
    return os.environ.get("NAJEM_NAZWA") or (f.read_text().strip() if f.exists() else "Najem")


app.jinja_env.globals["nazwa_serwisu"] = _nazwa_serwisu()


def check_csrf():
    sent = request.form.get("_csrf", "")
    if not sent or not secrets.compare_digest(sent, session.get("_csrf", "")):
        abort(400)


def login_required(view):
    """Panel klienta. Zarządcę odsyła do jego panelu."""
    @wraps(view)
    def wrapped(*a, **kw):
        if g.user is None:
            return redirect(url_for("login", next=request.path))
        if g.user["role"] == "zarzadca":
            return redirect(url_for("zarzadca.pulpit"))
        return view(*a, **kw)
    return wrapped


def manager_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if g.user is None:
            return redirect(url_for("login", next=request.path))
        if g.user["role"] != "zarzadca":
            abort(403)
        return view(*a, **kw)
    return wrapped


def home_for(user):
    return url_for("zarzadca.pulpit") if user["role"] == "zarzadca" else url_for("panel")


# Limit prób logowania: maks. 10 nieudanych na login lub 30 na IP w ciągu 15 minut.
def too_many_attempts(login_, ip):
    db = get_db()
    db.execute("DELETE FROM login_attempts WHERE at < datetime('now','-1 day')")
    q = "SELECT count(*) FROM login_attempts WHERE at > datetime('now','-15 minutes') AND "
    by_login = db.execute(q + "login = ? COLLATE NOCASE", (login_,)).fetchone()[0]
    by_ip = db.execute(q + "ip = ?", (ip,)).fetchone()[0]
    return by_login >= 10 or by_ip >= 30


@app.before_request
def load_user():
    g.user = None
    uid = session.get("user_id")
    if uid:
        g.user = get_db().execute(
            "SELECT id, login, full_name, role FROM users WHERE id=? AND aktywny=1", (uid,)
        ).fetchone()
        if g.user is None:
            session.clear()


@app.context_processor
def inject():
    return {"menu": MENU, "user": g.get("user")}


# ---------- widoki ----------
@app.get("/")
def index():
    return render_template("index.html")


@app.route("/logowanie", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(home_for(g.user))
    if request.method == "POST":
        check_csrf()
        login_ = request.form.get("login", "").strip()
        password = request.form.get("password", "")
        ip = request.remote_addr or "?"
        db = get_db()
        if too_many_attempts(login_, ip):
            db.commit()
            flash("Zbyt wiele nieudanych prób. Spróbuj ponownie za 15 minut.")
            return render_template("login.html", login=login_), 429
        row = db.execute(
            "SELECT id, role, password_hash FROM users WHERE login=? AND aktywny=1", (login_,)
        ).fetchone()
        if row and check_password_hash(row["password_hash"], password):
            session.clear()
            session["user_id"] = row["id"]
            db.execute("UPDATE users SET last_login_at=datetime('now') WHERE id=?", (row["id"],))
            db.execute("DELETE FROM login_attempts WHERE login=? COLLATE NOCASE", (login_,))
            db.commit()
            nxt = request.args.get("next", "")
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = home_for(row)
            return redirect(nxt)
        db.execute("INSERT INTO login_attempts (login, ip) VALUES (?,?)", (login_, ip))
        db.commit()
        flash("Nieprawidłowy login lub hasło.")
        return render_template("login.html", login=login_), 401
    return render_template("login.html", login="")


@app.post("/wyloguj")
def logout():
    check_csrf()
    session.clear()
    return redirect(url_for("index"))


@app.errorhandler(403)
def forbidden(_e):
    return render_template("error.html", code=403, msg="Nie masz dostępu do tej strony."), 403


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404, msg="Nie znaleziono strony."), 404


@app.get("/panel")
@login_required
def panel():
    return redirect(url_for("section", key="pulpit"))


# ---------- formatowanie ----------
MIESIACE = ["stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca",
            "sierpnia", "września", "października", "listopada", "grudnia"]


@app.template_filter("zl")
def fmt_zl(grosze):
    if grosze is None:
        return "—"
    zl, gr = divmod(int(grosze), 100)
    return f"{zl:,}".replace(",", " ") + f",{gr:02d} zł"


@app.template_filter("data")
def fmt_data(iso):
    if not iso:
        return "—"
    d = date.fromisoformat(iso[:10])
    return f"{d.day} {MIESIACE[d.month - 1]} {d.year}"


@app.template_filter("m2")
def fmt_m2(v):
    if v is None:
        return "—"
    return (f"{v:g}".replace(".", ",")) + " m²"


def plural(n, one, few, many):
    if n == 1:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


def status_umowy(u, today=None):
    """Zwraca (kod, etykieta, opis) statusu umowy liczonego z dat."""
    today = today or date.today()
    od = date.fromisoformat(u["data_od"])
    koniec = u["rozwiazana_dnia"] or u["data_do"]
    do = date.fromisoformat(koniec) if koniec else None
    if today < od:
        dni = (od - today).days
        return "przyszla", "Rozpocznie się", f"za {dni} {plural(dni, 'dzień', 'dni', 'dni')}"
    if do and today > do:
        return "zakonczona", "Zakończona", f"{fmt_data(koniec)}"
    if do:
        dni = (do - today).days
        if dni <= 60:
            return "konczy", "Kończy się", f"za {dni} {plural(dni, 'dzień', 'dni', 'dni')}"
        return "aktywna", "Aktywna", f"do {fmt_data(koniec)}"
    return "aktywna", "Aktywna", "na czas nieokreślony"


app.jinja_env.globals["status_umowy"] = status_umowy


# ---------- moje lokale ----------
UMOWY_SQL = """
SELECT u.*, l.nazwa AS lokal_nazwa, l.ulica, l.nr_lokalu, l.kod, l.miasto
FROM umowy u JOIN lokale l ON l.id = u.lokal_id
WHERE u.najemca_id = ?
"""


def moje_umowy(uid):
    rows = get_db().execute(UMOWY_SQL + " ORDER BY u.data_od DESC", (uid,)).fetchall()
    order = {"konczy": 0, "aktywna": 1, "przyszla": 2, "zakonczona": 3}
    return sorted(rows, key=lambda r: order[status_umowy(r)[0]])


@app.get("/panel/lokale")
@login_required
def lokale():
    umowy = moje_umowy(g.user["id"])
    by_lokal = {}
    for u in umowy:                      # pierwsza = najważniejsza umowa lokalu
        by_lokal.setdefault(u["lokal_id"], u)
    rows = []
    if by_lokal:
        q = ",".join("?" * len(by_lokal))
        for l in get_db().execute(f"SELECT * FROM lokale WHERE id IN ({q})", list(by_lokal)):
            rows.append((l, by_lokal[l["id"]]))
    rows.sort(key=lambda r: umowy.index(r[1]))
    return render_template("lokale.html", active="lokale", rows=rows)


@app.get("/panel/lokale/<int:lid>")
@login_required
def lokal(lid):
    umowy = [u for u in moje_umowy(g.user["id"]) if u["lokal_id"] == lid]
    if not umowy:                        # klient widzi tylko swoje lokale
        abort(404)
    l = get_db().execute("SELECT * FROM lokale WHERE id=?", (lid,)).fetchone()
    return render_template("lokal.html", active="lokale", l=l, umowy=umowy)


# ---------- umowy ----------
@app.get("/panel/umowy")
@login_required
def umowy():
    return render_template("umowy.html", active="umowy", umowy=moje_umowy(g.user["id"]))


@app.get("/panel/umowy/<int:uid>")
@login_required
def umowa(uid):
    u = get_db().execute(UMOWY_SQL + " AND u.id = ?", (g.user["id"], uid)).fetchone()
    if u is None:
        abort(404)
    aneksy = get_db().execute(
        "SELECT * FROM aneksy WHERE umowa_id=? ORDER BY data", (uid,)).fetchall()
    return render_template("umowa.html", active="umowy", u=u, aneksy=aneksy)


@app.get("/panel/<key>")
@login_required
def section(key):
    if key not in MENU_KEYS:
        abort(404)
    item = next(m for m in MENU if m[0] == key)
    return render_template("section.html", active=key, title=item[1], desc=item[2])


@app.get("/zdrowie")
def health():
    return "ok"


init_db()

from admin import bp as admin_bp  # noqa: E402  (korzysta z helperów powyżej)
import rozliczenia  # noqa: E402,F401  (dokłada widoki do panelu zarządcy i klienta)
import budynki  # noqa: E402,F401
import podatek  # noqa: E402,F401
app.register_blueprint(admin_bp)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=True)
