"""Podatek od najmu prywatnego (ryczałt od przychodów ewidencjonowanych) i rejestr wydatków.

Przychód miesiąca = czynsz z zawiadomień za ten miesiąc (okres czynszu) — jak w arkuszu
(zakładki PIT / „Przychód do podatku”). Media rozliczane wg zużycia nie są przychodem;
ryczałt za opłaty z umowy (pole „Opłaty”) można wliczać w Ustawieniach.

Podatek: stawka podstawowa (8,5%) do progu rocznego przychodu (100 000 zł), powyżej — stawka
podwyższona (12,5%). Zaliczka za miesiąc = podatek od przychodu narastająco od początku roku
− zaliczki za wcześniejsze miesiące, zaokrąglona do pełnych złotych; termin: 20. dzień
następnego miesiąca.
"""
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from flask import flash, redirect, render_template, request, url_for

from admin import Form, _get_or_404, _insert, bp
from app import get_db
from rozliczenia import MIESIACE_M, budynki_lista, nastepny, ust

KATEGORIE = ["naprawa / remont", "wyposażenie", "materiały", "usługa", "opłaty i podatki", "ubezpieczenie", "inne"]


def parametry():
    s = ust()
    return {"stawka": s["podatek_stawka"] or Decimal("8.5"), "stawka2": s["podatek_stawka2"] or Decimal("12.5"),
            "prog": s["podatek_prog"] if s["podatek_prog"] is not None else 10_000_000,
            "ryczalt": bool(s["podatek_ryczalt_oplat"])}


def podatek_od(przychod_gr, p):
    """Podatek (grosze, bez zaokrągleń) od przychodu narastająco."""
    dol = min(przychod_gr, p["prog"])
    gora = max(0, przychod_gr - p["prog"])
    return Decimal(dol) * p["stawka"] / 100 + Decimal(gora) * p["stawka2"] / 100


def do_zl(gr):
    return int((Decimal(gr) / 100).quantize(Decimal(1), ROUND_HALF_UP)) * 100


def przychody(rok, p):
    """{okres: {lokal_id: grosze}} z pozycji czynszu (i ryczałtu za opłaty) wystawionych zawiadomień."""
    warunek = "p.tytul LIKE 'Czynsz%'" + (" OR p.tytul LIKE 'Opłaty eksploatacyjne%'" if p["ryczalt"] else "")
    out = {}
    for r in get_db().execute(f"""
            SELECT n.okres_czynsz AS okres, u.lokal_id, sum(p.kwota) AS kwota
            FROM naliczenia_pozycje p JOIN naliczenia n ON n.id=p.naliczenie_id JOIN umowy u ON u.id=n.umowa_id
            WHERE n.okres_czynsz LIKE ? AND ({warunek})
            GROUP BY n.okres_czynsz, u.lokal_id""", (f"{rok}-%",)):
        out.setdefault(r["okres"], {})[r["lokal_id"]] = r["kwota"]
    return out


def zestawienie(rok):
    p = parametry()
    prz = przychody(rok, p)
    db = get_db()
    wplaty = {}
    for w in db.execute("SELECT * FROM podatek_wplaty WHERE okres LIKE ? ORDER BY data", (f"{rok}-%",)):
        wplaty.setdefault(w["okres"], []).append(w)
    dzis = date.today().isoformat()
    miesiace, narast, zaliczki = [], 0, 0
    for m in range(1, 13):
        okres = f"{rok}-{m:02d}"
        przych = sum(prz.get(okres, {}).values())
        narast += przych
        nalezny = max(0, do_zl(podatek_od(narast, p)) - zaliczki)
        zaliczki += nalezny
        wpl = wplaty.get(okres, [])
        wplacono = sum(w["kwota"] for w in wpl)
        termin = nastepny(okres) + "-20"
        if not przych and not wpl:
            stan = "brak"
        elif wplacono >= nalezny:
            stan = "zaplacony"
        elif termin < dzis:
            stan = "po_terminie"
        else:
            stan = "do_zaplaty"
        miesiace.append({"okres": okres, "nazwa": MIESIACE_M[m - 1], "przychod": przych, "narast": narast,
                         "podatek": nalezny, "wplaty": wpl, "wplacono": wplacono, "termin": termin, "stan": stan,
                         "prog": narast > p["prog"] >= narast - przych})
    lokale = {}
    for okres, d in prz.items():
        for lid, kw in d.items():
            lokale[lid] = lokale.get(lid, 0) + kw
    nazwy = {r["id"]: r["nazwa"] for r in db.execute("SELECT id, nazwa FROM lokale")}
    wg_lokali = sorted(((nazwy.get(lid, f"lokal {lid}"), kw) for lid, kw in lokale.items()), key=lambda x: x[0])
    wydatki = db.execute("SELECT coalesce(sum(kwota),0) FROM wydatki WHERE data LIKE ?", (f"{rok}-%",)).fetchone()[0]
    suma = {"przychod": narast, "podatek": zaliczki, "wplacono": sum(m["wplacono"] for m in miesiace),
            "wydatki": wydatki}
    suma["wynik"] = suma["przychod"] - suma["podatek"] - wydatki
    return {"miesiace": miesiace, "wg_lokali": wg_lokali, "suma": suma, "p": p}


def _rok():
    r = request.values.get("rok", type=int)
    return r if r and 2000 <= r <= 2100 else date.today().year


def _lata(tabela_daty):
    db = get_db()
    lata = {date.today().year}
    for (y,) in db.execute("SELECT DISTINCT substr(okres_czynsz,1,4) FROM naliczenia"):
        lata.add(int(y))
    for (y,) in db.execute(f"SELECT DISTINCT substr({tabela_daty},1,4) FROM wydatki"):
        if y and y.isdigit():
            lata.add(int(y))
    return sorted(lata, reverse=True)


# ---------- podatek ----------
@bp.get("/podatek")
def podatek():
    rok = _rok()
    return render_template("admin/podatek.html", active="podatek", rok=rok, lata=_lata("data"),
                           **zestawienie(rok), dzis=date.today().isoformat())


@bp.post("/podatek/wplata")
def podatek_wplata():
    f = Form(request.form)
    okres = f.text("okres", "Miesiąc", required=True, max_len=7)
    f.date("data", "Data wpłaty")
    f.money("kwota", "Kwota", positive=True)
    f.text("uwagi", "Uwagi", max_len=300)
    if f.ok:
        _insert("podatek_wplaty", f.clean)
        get_db().commit()
        flash(f"Zapisano wpłatę podatku za {MIESIACE_M[int(okres[5:7]) - 1]} {okres[:4]}.", "ok")
    else:
        flash("Wpłata nie została zapisana: " + " ".join(f.errors.values()), "err")
    return redirect(url_for(".podatek", rok=(okres or "")[:4]))


@bp.post("/podatek/wplata/<int:wid>/usun")
def podatek_wplata_usun(wid):
    w = _get_or_404("podatek_wplaty", wid)
    get_db().execute("DELETE FROM podatek_wplaty WHERE id=?", (wid,))
    get_db().commit()
    flash("Usunięto wpłatę podatku.", "ok")
    return redirect(url_for(".podatek", rok=w["okres"][:4]))


# ---------- wydatki ----------
def _wydatek_form(f):
    f.date("data", "Data")
    f.text("opis", "Opis", required=True, max_len=300)
    f.text("firma", "Sklep / firma", max_len=120)
    f.text("dokument", "Nr dokumentu", max_len=80)
    f.money("kwota", "Kwota", positive=True)
    f.choice("kategoria", "Kategoria", KATEGORIE)
    db = get_db()
    bid = f.number("budynek_id", "Budynek", 1, 10**9)
    if bid and not db.execute("SELECT 1 FROM budynki WHERE id=?", (bid,)).fetchone():
        f.error("budynek_id", "Wybierz budynek.")
    lid = f.number("lokal_id", "Lokal", 1, 10**9)
    if lid and not db.execute("SELECT 1 FROM lokale WHERE id=?", (lid,)).fetchone():
        f.error("lokal_id", "Wybierz lokal.")
    return f


@bp.route("/wydatki", methods=["GET", "POST"])
def wydatki():
    db = get_db()
    rok = _rok()
    status = 200
    if request.method == "POST":
        f = _wydatek_form(Form(request.form))
        if f.ok:
            _insert("wydatki", f.clean)
            db.commit()
            flash(f"Dodano wydatek: {f.clean['opis']}.", "ok")
            return redirect(url_for(".wydatki", rok=f.clean["data"][:4]))
        status = 422
    else:
        f = Form({"data": date.today().isoformat(), "kategoria": "naprawa / remont"})
    rows = db.execute("""SELECT w.*, b.nazwa AS budynek, l.nazwa AS lokal FROM wydatki w
                         LEFT JOIN budynki b ON b.id=w.budynek_id LEFT JOIN lokale l ON l.id=w.lokal_id
                         WHERE w.data LIKE ? ORDER BY w.data DESC, w.id DESC""", (f"{rok}-%",)).fetchall()
    kat = {}
    for r in rows:
        kat[r["kategoria"] or "inne"] = kat.get(r["kategoria"] or "inne", 0) + r["kwota"]
    lokale = [(l["id"], l["nazwa"]) for l in db.execute("SELECT id, nazwa FROM lokale ORDER BY nazwa")]
    return render_template("admin/wydatki.html", active="wydatki", f=f, rows=rows, rok=rok, lata=_lata("data"),
                           suma=sum(r["kwota"] for r in rows), kat=sorted(kat.items(), key=lambda x: -x[1]),
                           kategorie=[(k, k) for k in KATEGORIE], lokale_opcje=lokale,
                           budynki=budynki_lista()), status


@bp.post("/wydatki/<int:wid>/usun")
def wydatek_usun(wid):
    w = _get_or_404("wydatki", wid)
    get_db().execute("DELETE FROM wydatki WHERE id=?", (wid,))
    get_db().commit()
    flash(f"Usunięto wydatek: {w['opis']}.", "ok")
    return redirect(url_for(".wydatki", rok=w["data"][:4]))

