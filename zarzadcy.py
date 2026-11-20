"""Konta zarządców: lista, dodawanie, hasło startowe, blokowanie.

Zarządca ma jedną rolę w aplikacji; osoba, która jest też najemcą, ma osobne konto najemcy.
Nie można zablokować własnego konta ani ostatniego aktywnego zarządcy."""
import re

from flask import abort, flash, g, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

from admin import bp
from app import get_db
from konto import HASLO_MIN


def _zarzadcy():
    return get_db().execute("""SELECT id, login, full_name, email, telefon, aktywny, last_login_at FROM users
                               WHERE role='zarzadca' ORDER BY aktywny DESC, coalesce(full_name, login)""").fetchall()


def _haslo(errors, pole="haslo", wymagane=True):
    h = request.form.get(pole, "")
    if not h and not wymagane:
        return None
    if len(h) < HASLO_MIN:
        errors[pole] = f"Hasło musi mieć co najmniej {HASLO_MIN} znaków."
    return h


@bp.route("/zarzadcy", methods=["GET", "POST"], endpoint="zarzadcy")   # CSRF sprawdza _guard blueprintu
def zarzadcy():
    errors, form = {}, {}
    if request.method == "POST":
        login = request.form.get("login", "").strip()
        nazwa = " ".join(request.form.get("full_name", "").split())
        email = request.form.get("email", "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{3,50}", login):
            errors["login"] = "Login: 3–50 znaków, litery bez polskich znaków, cyfry, . _ -"
        elif get_db().execute("SELECT 1 FROM users WHERE login=?", (login,)).fetchone():
            errors["login"] = "Ten login jest już zajęty."
        if not nazwa:
            errors["full_name"] = "Podaj imię i nazwisko."
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            errors["email"] = "Nieprawidłowy adres e-mail."
        haslo = _haslo(errors)
        if not errors:
            get_db().execute("INSERT INTO users (login, password_hash, full_name, role, email) VALUES (?,?,?, 'zarzadca', ?)",
                             (login, generate_password_hash(haslo), nazwa, email or None))
            get_db().commit()
            flash(f"Dodano zarządcę {nazwa} ({login}).", "ok")
            return redirect(url_for("zarzadca.zarzadcy"))
        form = {"login": login, "full_name": nazwa, "email": email}
    return render_template("admin/zarzadcy.html", active="zarzadcy", rows=_zarzadcy(), errors=errors, form=form,
                           haslo_min=HASLO_MIN, nowy=bool(errors))


@bp.post("/zarzadcy/<int:zid>")
def zarzadca_zmiana(zid):
    z = get_db().execute("SELECT * FROM users WHERE id=? AND role='zarzadca'", (zid,)).fetchone()
    if z is None:
        abort(404)
    akcja = request.form.get("akcja")
    if akcja == "haslo":
        errors = {}
        h = _haslo(errors)
        if errors:
            flash(errors["haslo"], "err")
        else:
            get_db().execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(h), zid))
            get_db().commit()
            flash(f"Ustawiono nowe hasło dla {z['full_name'] or z['login']}.", "ok")
    elif akcja in ("zablokuj", "odblokuj"):
        if akcja == "zablokuj":
            if zid == g.user["id"]:
                flash("Nie możesz zablokować własnego konta.", "err")
                return redirect(url_for("zarzadca.zarzadcy"))
            aktywni = get_db().execute("SELECT count(*) FROM users WHERE role='zarzadca' AND aktywny=1").fetchone()[0]
            if aktywni <= 1:
                flash("To ostatni aktywny zarządca — nie można go zablokować.", "err")
                return redirect(url_for("zarzadca.zarzadcy"))
        get_db().execute("UPDATE users SET aktywny=? WHERE id=?", (0 if akcja == "zablokuj" else 1, zid))
        get_db().commit()
        flash(("Zablokowano" if akcja == "zablokuj" else "Odblokowano") + f" konto {z['login']}.", "ok")
    else:
        abort(400)
    return redirect(url_for("zarzadca.zarzadcy"))
