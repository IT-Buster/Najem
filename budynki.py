"""Budynki: lokale, faktury za media i liczniki główne należą do budynku.
Budynek może mieć własne stawki (światło, śmieci, rachunek) — puste = domyślne z Ustawień."""
import re

from flask import flash, redirect, render_template, request, url_for

from admin import Form, _get_or_404, _insert, _update, bp, zl_input
from app import get_db

POLA_STAWEK = [  # klucz, etykieta
    ("swiatlo", "Światło – klatka schodowa, podwórze"),
    ("smieci_1", "Wywóz nieczystości – 1 osoba"),
    ("smieci_2", "Wywóz nieczystości – 2 osoby"),
    ("smieci_3", "Wywóz nieczystości – 3 osoby"),
    ("smieci_4", "Wywóz nieczystości – 4 osoby"),
    ("smieci_5", "Wywóz nieczystości – 5 i więcej osób"),
]


@bp.context_processor
def _ctx_budynki():
    return {"budynki_opcje": [(b["id"], b["nazwa"]) for b in
                              get_db().execute("SELECT id, nazwa FROM budynki ORDER BY nazwa")]}


def _form(f):
    f.text("nazwa", "Nazwa", required=True, max_len=120)
    f.text("ulica", "Ulica i numer", max_len=200)
    kod = f.text("kod", "Kod pocztowy", max_len=6)
    if kod and not re.fullmatch(r"\d{2}-\d{3}", kod):
        f.error("kod", "Kod pocztowy w formacie 00-000.")
    f.text("miasto", "Miasto", max_len=100)
    f.text("nr_wodomierza", "Nr wodomierza głównego", max_len=50)
    f.text("nr_cieplomierza", "Nr ciepłomierza głównego", max_len=50)
    for k, label in POLA_STAWEK:
        pusty = not (f.raw.get(k) or "").strip()
        v = f.money(k, label, required=False)
        f.clean[k] = None if pusty else v
    f.number("lokale_cieplo", "Liczba lokali do podziału opłaty przesyłowej", 1, 1000)
    f.text("rachunek", "Rachunek bankowy", max_len=200)
    f.text("uwagi", "Uwagi", max_len=4000)
    return f


def _domyslne():
    from rozliczenia import ust
    return ust()


@bp.get("/budynki")
def budynki():
    rows = get_db().execute("""
        SELECT b.*, (SELECT count(*) FROM lokale l WHERE l.budynek_id=b.id) AS lokale_n,
               (SELECT count(*) FROM faktury f WHERE f.budynek_id=b.id) AS faktury_n
        FROM budynki b ORDER BY b.nazwa""").fetchall()
    bez = get_db().execute("SELECT * FROM lokale WHERE budynek_id IS NULL ORDER BY nazwa").fetchall()
    return render_template("admin/budynki.html", active="budynki", rows=rows, bez=bez)


@bp.route("/budynki/nowy", methods=["GET", "POST"])
def budynek_nowy():
    if request.method == "POST":
        f = _form(Form(request.form))
        if f.ok:
            bid = _insert("budynki", f.clean)
            get_db().commit()
            flash(f"Dodano budynek „{f.clean['nazwa']}”.", "ok")
            return redirect(url_for(".budynek_edycja", bid=bid))
        return render_template("admin/budynek_form.html", active="budynki", f=f, b=None, dom=_domyslne()), 422
    return render_template("admin/budynek_form.html", active="budynki", f=Form({}), b=None, dom=_domyslne())


@bp.route("/budynki/<int:bid>", methods=["GET", "POST"])
def budynek_edycja(bid):
    b = _get_or_404("budynki", bid)
    db = get_db()
    lokale = db.execute("SELECT * FROM lokale WHERE budynek_id=? ORDER BY nazwa", (bid,)).fetchall()
    ctx = dict(active="budynki", b=b, lokale=lokale, dom=_domyslne(),
               bez=db.execute("SELECT * FROM lokale WHERE budynek_id IS NULL ORDER BY nazwa").fetchall(),
               faktury_n=db.execute("SELECT count(*) FROM faktury WHERE budynek_id=?", (bid,)).fetchone()[0])
    if request.method == "POST":
        f = _form(Form(request.form))
        if f.ok:
            _update("budynki", bid, f.clean)
            db.commit()
            flash("Zapisano zmiany.", "ok")
            return redirect(url_for(".budynek_edycja", bid=bid))
        return render_template("admin/budynek_form.html", f=f, **ctx), 422
    raw = {k: ("" if b[k] is None else str(b[k])) for k in b.keys()}
    for k, _ in POLA_STAWEK:
        raw[k] = "" if b[k] is None else zl_input(b[k])
    return render_template("admin/budynek_form.html", f=Form(raw), **ctx)


@bp.post("/budynki/<int:bid>/przypisz")
def budynek_przypisz(bid):
    """Przypisanie lokali bez budynku do tego budynku."""
    _get_or_404("budynki", bid)
    ids = [int(x) for x in request.form.getlist("lokal") if x.isdigit()]
    db = get_db()
    for lid in ids:
        db.execute("UPDATE lokale SET budynek_id=? WHERE id=? AND budynek_id IS NULL", (bid, lid))
    db.commit()
    flash(f"Przypisano lokale: {len(ids)}." if ids else "Nie zaznaczono lokali.", "ok" if ids else "err")
    return redirect(url_for(".budynek_edycja", bid=bid))


@bp.post("/budynki/<int:bid>/usun")
def budynek_usun(bid):
    b = _get_or_404("budynki", bid)
    db = get_db()
    if db.execute("SELECT 1 FROM lokale WHERE budynek_id=? UNION SELECT 1 FROM faktury WHERE budynek_id=?",
                  (bid, bid)).fetchone():
        flash("Nie można usunąć budynku, który ma lokale lub faktury.", "err")
        return redirect(url_for(".budynek_edycja", bid=bid))
    db.execute("DELETE FROM odczyty WHERE lokal_id=?", (-bid,))
    db.execute("DELETE FROM wymiany WHERE lokal_id=?", (-bid,))
    db.execute("DELETE FROM budynki WHERE id=?", (bid,))
    db.commit()
    flash(f"Usunięto budynek „{b['nazwa']}”.", "ok")
    return redirect(url_for(".budynki"))
