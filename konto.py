"""Moje konto: zmiana hasła (zarządca i najemca) oraz dane kontaktowe najemcy."""
import re

from flask import flash, g, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from admin import bp
from app import app, check_csrf, get_db, login_required

HASLO_MIN = 10


def _zmien_haslo(errors):
    """Sprawdza formularz zmiany hasła; zwraca True, gdy hasło zmieniono."""
    stare, nowe, powt = (request.form.get(k, "") for k in ("haslo", "nowe", "powtorz"))
    row = get_db().execute("SELECT login, password_hash FROM users WHERE id=?", (g.user["id"],)).fetchone()
    if not check_password_hash(row["password_hash"], stare):
        errors["haslo"] = "Obecne hasło jest nieprawidłowe."
    if len(nowe) < HASLO_MIN:
        errors["nowe"] = f"Nowe hasło musi mieć co najmniej {HASLO_MIN} znaków."
    elif nowe.lower() == row["login"].lower() or nowe == stare:
        errors["nowe"] = "Nowe hasło musi się różnić od loginu i od obecnego hasła."
    if not errors.get("nowe") and nowe != powt:
        errors["powtorz"] = "Hasła się nie zgadzają."
    if errors:
        return False
    get_db().execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(nowe), g.user["id"]))
    get_db().commit()
    return True


def _kontakt(errors):
    email = request.form.get("email", "").strip()
    tel = " ".join(request.form.get("telefon", "").split())
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        errors["email"] = "Nieprawidłowy adres e-mail."
    if tel and not re.fullmatch(r"[+\d][\d\s()-]{6,20}", tel):
        errors["telefon"] = "Nieprawidłowy numer telefonu."
    if not errors:
        get_db().execute("UPDATE users SET email=?, telefon=? WHERE id=?", (email or None, tel or None, g.user["id"]))
        get_db().commit()
    return email, tel


def _konto(szablon, wroc):
    errors, form = {}, {}
    if request.method == "POST":
        if request.form.get("akcja") == "haslo":
            if _zmien_haslo(errors):
                flash("Hasło zostało zmienione.", "ok")
                return redirect(wroc)
        else:
            form["email"], form["telefon"] = _kontakt(errors)
            if not errors:
                flash("Dane kontaktowe zapisane.", "ok")
                return redirect(wroc)
    u = get_db().execute("SELECT login, full_name, email, telefon, last_login_at FROM users WHERE id=?",
                         (g.user["id"],)).fetchone()
    return render_template(szablon, active="konto", u=u, errors=errors, form=form, haslo_min=HASLO_MIN)


@app.route("/panel/konto", methods=["GET", "POST"])
@login_required
def konto():
    if request.method == "POST":
        check_csrf()
    return _konto("konto.html", url_for("konto"))


@bp.route("/konto", methods=["GET", "POST"], endpoint="konto")     # CSRF sprawdza _guard blueprintu
def zarzadca_konto():
    return _konto("admin/konto.html", url_for("zarzadca.konto"))
