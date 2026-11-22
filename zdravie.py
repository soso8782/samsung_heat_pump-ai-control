#!/usr/bin/env python3
"""Zdravie čerpadla (len čítanie, nič sa nezapisuje do čerpadla): ukazovatele očistené od počasia, porovnané so základom.

Okná: „nedávne“ = posledných 7 uzavretých dní, „základ“ = predchádzajúcich až 60 dní (aspoň ZAKLAD_MIN_DNI dní s dátami).
Všetky porovnania sú po zhodných „priehradkách“ (vonkajšia teplota po 2 °C, pri prietoku a ΔT frekvencia po 10 Hz), vážené
hodinami nedávneho okna; priehradka s menej dátami ako minimum sa nepočíta. Zistenia (nálezy) sa ukladajú do stats.db
(tabuľka zdravie_nalezy, história) a pošlú sa ako upozornenie druhu „zdravie“ (správca); po návrate do normy sa zrušia.
Iba štandardná knižnica."""
import datetime as _dt, json, math, os, sqlite3, time

import stats
from common import get_state, set_state
import i18n
from i18n import tr

KODY = ("cop", "odmrazovanie", "prietok", "dt", "cyklovanie")
NEDAVNE_DNI = 7
ZAKLAD_DNI = 60
ZAKLAD_MIN_DNI = 21         # menej dní základu = málo dát (žiadne hodnotenie)
TYZDNE = 12
BIN_T = 2.0                 # °C
BIN_F = 10                  # Hz
MIN_ODMRAZ = 3              # najmenej odmrazovaní (očakávaných aj nameraných), aby sa ich počet hodnotil
MIN_CYKLOV = 10             # najmenej cyklov v priehradke pre podiel krátkych cyklov
KRATKY_CYKLUS_MIN = 10.0
BACKFILL_DNI = 90           # dokiaľ dozadu sa denné súčty počítajú z tc.db
HYST = 0.7                  # zistenie sa ruší, keď zhoršenie klesne pod 70 % prahu
DAILY_AT = (4, 10)          # denná úloha po 04:10 (po prepočte hodín v stats_tick)
FREQ_MIN = 20.0             # prietok a ΔT sa berú len pri chode kompresora aspoň touto frekvenciou
DEFAULTS = {"ZDRAVIE": "1", "ZDRAVIE_COP_PCT": "10", "ZDRAVIE_ODMRAZ_PCT": "50", "ZDRAVIE_PRIETOK_PCT": "15",
            "ZDRAVIE_DT_PCT": "20", "ZDRAVIE_CYKLY_PCT": "50", "ZDRAVIE_CYKLY_PP": "20", "ZDRAVIE_MIN_HODIN": "6",
            "ZDRAVIE_MIN_BINOV": "2"}

DDL = """
CREATE TABLE IF NOT EXISTS zdravie_den(den TEXT, ind TEXT, bin INTEGER, n REAL, s REAL, PRIMARY KEY(den, ind, bin));
CREATE TABLE IF NOT EXISTS zdravie_nalezy(id INTEGER PRIMARY KEY, kod TEXT, od INTEGER, do INTEGER, hodnota REAL, prah REAL,
  notified INTEGER DEFAULT 0, clear_notified INTEGER DEFAULT 0);
"""


# ---------------------------------------------------------------- nastavenia
def _f(cfg, key):
    try:
        return float(str(cfg.get(key) if cfg.get(key) not in (None, "") else DEFAULTS[key]).replace(",", "."))
    except ValueError:
        return float(DEFAULTS[key])


def enabled(cfg):
    return str(cfg.get("ZDRAVIE") if cfg.get("ZDRAVIE") not in (None, "") else DEFAULTS["ZDRAVIE"]).strip().lower() not in ("0", "false", "no", "off", "nie")


def prahy(cfg):
    """Prahy zhoršenia v percentách (kladné číslo; COP/prietok/ΔT = pokles, odmrazovanie/cyklovanie = nárast)."""
    return {"cop": _f(cfg, "ZDRAVIE_COP_PCT"), "odmrazovanie": _f(cfg, "ZDRAVIE_ODMRAZ_PCT"), "prietok": _f(cfg, "ZDRAVIE_PRIETOK_PCT"),
            "dt": _f(cfg, "ZDRAVIE_DT_PCT"), "cyklovanie": _f(cfg, "ZDRAVIE_CYKLY_PCT")}


def _con(cfg):
    con = stats.connect(cfg)
    con.executescript(DDL)
    return con


def _bin_t(t):
    return int(math.floor(float(t) / BIN_T)) * int(BIN_T)


# ---------------------------------------------------------------- denné súčty z tc.db (odmrazovanie, prietok, ΔT)
def day_totals(cfg, day):
    """Súčty za lokálny deň `day` (date) z tc.db: {("odm", bin_teploty): (počet, minút), ("prietok", bin_Hz): (minút, súčet l/min),
    ("dt", bin_Hz): (minút, súčet K)}. Prietok a ΔT len v ustálenom kúrení (kompresor >= FREQ_MIN, ventil na kúrení, mimo odmrazovania)."""
    a, b = stats._bounds(day)
    T = stats.TC
    tc = stats._tc(cfg)
    out = {}
    try:
        valve, defr = stats._tracks(tc, a, b)
        r = tc.execute("SELECT max(ts) FROM samples WHERE ts>=? AND ts<?", (a, b)).fetchone()
        last = (r[0] if r and r[0] else b)
        divs = defr.intervals(a, b, last)
        ent = T.get("vonku", "")
        for s, e in divs:
            r = tc.execute("SELECT value FROM samples WHERE entity=? AND ts<=? AND value IS NOT NULL ORDER BY ts DESC LIMIT 1",
                           (ent, s)).fetchone()
            if r is None:
                continue
            k = ("odm", _bin_t(r[0]))
            x = out.get(k, (0, 0.0))
            out[k] = (x[0] + 1, x[1] + (e - s) / 60.0)

        def ser(key):
            d = {}
            for ts, v in tc.execute("SELECT ts, value FROM samples WHERE entity=? AND ts>=? AND ts<? AND value IS NOT NULL",
                                    (T.get(key, ""), a, b)):
                d[ts - ts % 60] = v
            return d
        fr, fl, dt = ser("freq"), ser("flow"), ser("dt")
        for t, f in fr.items():
            if f < FREQ_MIN or valve.at(t) != "room":
                continue
            if any(s - 600 <= t <= e + 900 for s, e in divs):
                continue
            kb = int(f // BIN_F) * BIN_F
            v = fl.get(t)
            if stats.flow_ok(v):
                x = out.get(("prietok", kb), (0, 0.0))
                out[("prietok", kb)] = (x[0] + 1, x[1] + v)
            v = dt.get(t)
            if v is not None and 0.5 <= v <= 20:
                x = out.get(("dt", kb), (0, 0.0))
                out[("dt", kb)] = (x[0] + 1, x[1] + v)
    finally:
        tc.close()
    return out


def update_days(cfg, now=None, con=None):
    """Doplní zdravie_den za dni, ktoré tam chýbajú (najviac BACKFILL_DNI dozadu), a prepočíta posledné 2 dni. Vracia počet dní."""
    now = int(now or time.time())
    own = con is None
    con = con or _con(cfg)
    n = 0
    try:
        today = _dt.date.fromtimestamp(now)
        r = con.execute("SELECT min(ts) FROM hourly").fetchone()
        if not r or r[0] is None:
            return 0
        first = max(_dt.date.fromtimestamp(r[0]), today - _dt.timedelta(days=BACKFILL_DNI))
        have = {x[0] for x in con.execute("SELECT DISTINCT den FROM zdravie_den WHERE ind='_'")}
        d = first
        while d < today:
            ds = d.isoformat()
            if ds not in have or d >= today - _dt.timedelta(days=2):
                try:
                    tot = day_totals(cfg, d)
                except sqlite3.Error:
                    d += _dt.timedelta(days=1)
                    continue
                con.execute("DELETE FROM zdravie_den WHERE den=?", (ds,))
                con.execute("INSERT INTO zdravie_den(den, ind, bin, n, s) VALUES(?,?,?,?,?)", (ds, "_", 0, 1, 0))
                con.executemany("INSERT INTO zdravie_den(den, ind, bin, n, s) VALUES(?,?,?,?,?)",
                                [(ds, k[0], k[1], v[0], v[1]) for k, v in tot.items()])
                n += 1
            d += _dt.timedelta(days=1)
        con.commit()
    finally:
        if own:
            con.close()
    return n


# ---------------------------------------------------------------- načítanie okien
def _load(con, d0, d1):
    """Dáta pre dni [d0, d1) (date): hodiny, cykly a denné súčty."""
    a, b = stats._midnight(d0), stats._midnight(d1)
    hours = [dict(zip(("ts", "vonku", "beh", "kur", "odm", "starty", "cop"), r)) for r in con.execute(
        "SELECT ts, vonku, beh_min, kurenie_min, odmraz_min, starty, cop FROM hourly WHERE ts>=? AND ts<? AND n>0", (a, b))]
    cyc = [dict(zip(("ts", "min", "mode", "vonku"), r)) for r in con.execute(
        "SELECT ts_start, minutes, mode, vonku FROM cycles WHERE ts_start>=? AND ts_start<? AND ts_stop IS NOT NULL", (a, b))]
    den = {}
    for ds, ind, bn, n, s in con.execute("SELECT den, ind, bin, n, s FROM zdravie_den WHERE den>=? AND den<? AND ind!='_'",
                                         (d0.isoformat(), d1.isoformat())):
        den.setdefault((ind, bn), [0.0, 0.0])
        den[(ind, bn)][0] += n
        den[(ind, bn)][1] += s
    ndays = len({stats._dstr(h["ts"]) for h in hours})
    return {"hours": hours, "cyc": cyc, "den": den, "dni": ndays}


def _cop_bins(w):
    acc = {}
    for h in w["hours"]:
        beh, kur = h.get("beh") or 0, h.get("kur") or 0
        if h.get("cop") is None or h.get("vonku") is None or beh < 30 or kur < 0.9 * beh or (h.get("odm") or 0) > 0:
            continue
        x = acc.setdefault(_bin_t(h["vonku"]), [0, 0.0])
        x[0] += 1
        x[1] += h["cop"]
    return {k: (n, s / n) for k, (n, s) in acc.items()}


def _run_hours(w):
    acc = {}
    for h in w["hours"]:
        if h.get("vonku") is None or not h.get("beh"):
            continue
        x = acc.setdefault(_bin_t(h["vonku"]), [0.0, 0])
        x[0] += h["beh"] / 60.0
        x[1] += h.get("starty") or 0
    return acc          # bin -> [hodín behu, štartov]


def _den_bins(w, ind):
    return {bn: (v[0], v[1]) for (i, bn), v in w["den"].items() if i == ind}


def _weighted(base, rec, minn, nmin):
    """Vážená relatívna zmena rec/base − 1 po priehradkách; váha = n nedávneho okna. -> (zmena | None, počet priehradok)."""
    num = den = 0.0
    nb = 0
    for k, (n_r, v_r) in rec.items():
        b = base.get(k)
        if not b or n_r < minn or b[0] < minn or not b[1]:
            continue
        num += n_r * (v_r / b[1] - 1.0)
        den += n_r
        nb += 1
    if nb < nmin or den <= 0:
        return None, nb
    return num / den, nb


def _cmp(cfg, kod, base, rec):
    """Porovnanie okna `rec` so základom `base` pre ukazovateľ `kod`: {"zmena": zlomok|None, "biny": n, + podrobnosti}."""
    mh = _f(cfg, "ZDRAVIE_MIN_HODIN")
    nmin = int(_f(cfg, "ZDRAVIE_MIN_BINOV"))
    out = {"zmena": None, "biny": 0}
    if kod == "cop":
        cb, cr = _cop_bins(base), _cop_bins(rec)
        out["zmena"], out["biny"] = _weighted(cb, cr, mh, nmin)
        ok = [k for k in cr if k in cb and cr[k][0] >= mh and cb[k][0] >= mh]
        sw = sum(cr[k][0] for k in ok)
        if sw:
            out["nedavne"] = round(sum(cr[k][0] * cr[k][1] for k in ok) / sw, 2)
            out["zaklad"] = round(sum(cr[k][0] * cb[k][1] for k in ok) / sw, 2)
    elif kod in ("prietok", "dt"):
        mm = mh * 20
        cb, cr = _den_bins(base, kod), _den_bins(rec, kod)
        cb = {k: (n, s / n) for k, (n, s) in cb.items() if n}
        cr = {k: (n, s / n) for k, (n, s) in cr.items() if n}
        out["zmena"], out["biny"] = _weighted(cb, cr, mm, nmin)
        ok = [k for k in cr if k in cb and cr[k][0] >= mm and cb[k][0] >= mm]
        sw = sum(cr[k][0] for k in ok)
        if sw:
            out["nedavne"] = round(sum(cr[k][0] * cr[k][1] for k in ok) / sw, 2)
            out["zaklad"] = round(sum(cr[k][0] * cb[k][1] for k in ok) / sw, 2)
    elif kod == "odmrazovanie":
        rb, rr = _run_hours(base), _run_hours(rec)
        db, dr = _den_bins(base, "odm"), _den_bins(rec, "odm")
        exp = act = 0.0
        nb = 0
        dsum = dw = 0.0
        for k, (h_r, _s) in rr.items():
            h_b = (rb.get(k) or (0, 0))[0]
            if h_r < mh or h_b < mh:
                continue
            nb += 1
            cb_n, cb_s = db.get(k, (0, 0.0))
            cr_n, cr_s = dr.get(k, (0, 0.0))
            exp += cb_n / h_b * h_r
            act += cr_n
            if cb_n >= MIN_ODMRAZ and cr_n >= MIN_ODMRAZ:
                dsum += cr_n * ((cr_s / cr_n) / (cb_s / cb_n) - 1.0)
                dw += cr_n
        out["biny"] = nb
        if nb >= nmin and exp >= MIN_ODMRAZ:
            out["pocet_zmena"] = act / exp - 1.0
            out["pocet_den"] = round(act / NEDAVNE_DNI, 2)
            out["pocet_den_zaklad"] = round(exp / NEDAVNE_DNI, 2)
        if nb >= nmin and dw > 0:
            out["trvanie_zmena"] = dsum / dw
        ch = [x for x in (out.get("pocet_zmena"), out.get("trvanie_zmena")) if x is not None]
        out["zmena"] = max(ch) if ch else None
    elif kod == "cyklovanie":
        rb, rr = _run_hours(base), _run_hours(rec)
        cb = {k: (v[0], v[1] / v[0]) for k, v in rb.items() if v[0] and v[1]}
        cr = {k: (v[0], v[1] / v[0]) for k, v in rr.items() if v[0]}
        out["zmena"], out["biny"] = _weighted(cb, cr, mh, nmin)
        ok = [k for k in cr if k in cb and cr[k][0] >= mh and cb[k][0] >= mh]
        sw = sum(cr[k][0] for k in ok)
        if sw:
            out["nedavne"] = round(sum(cr[k][0] * cr[k][1] for k in ok) / sw, 2)
            out["zaklad"] = round(sum(cr[k][0] * cb[k][1] for k in ok) / sw, 2)

        def shares(w):
            acc = {}
            for c in w["cyc"]:
                if c["mode"] != "kurenie" or c.get("vonku") is None or c.get("min") is None:
                    continue
                x = acc.setdefault(_bin_t(c["vonku"]), [0, 0])
                x[0] += 1
                x[1] += 1 if c["min"] < KRATKY_CYKLUS_MIN else 0
            return acc
        sb, sr = shares(base), shares(rec)
        num = den = 0.0
        for k, (n_r, s_r) in sr.items():
            b = sb.get(k)
            if b and n_r >= MIN_CYKLOV and b[0] >= MIN_CYKLOV:
                num += n_r * (s_r / n_r - b[1] / b[0])
                den += n_r
        if den:
            out["kratke_pp"] = num / den * 100.0
            out["kratke_nedavne"] = round(100.0 * sum(v[1] for v in sr.values()) / max(1, sum(v[0] for v in sr.values())), 1)
            out["kratke_zaklad"] = round(100.0 * sum(v[1] for v in sb.values()) / max(1, sum(v[0] for v in sb.values())), 1)
    return out


SMER = {"cop": -1, "prietok": -1, "dt": -1, "odmrazovanie": 1, "cyklovanie": 1}      # smer zhoršenia


def _horsie(kod, c):
    """Zhoršenie v zlomku (kladné = horšie) alebo None."""
    if c.get("zmena") is None:
        return None
    return c["zmena"] * SMER[kod]


def _stav(kod, c, thr):
    h = _horsie(kod, c)
    if kod == "cyklovanie" and c.get("kratke_pp") is not None and c["kratke_pp"] >= thr["pp"]:
        return "crit"
    if h is None:
        return "nodata"
    p = thr[kod] / 100.0
    return "crit" if h >= p else ("warn" if h >= 0.5 * p else "ok")


def _errors(con, a, b):
    out = {}
    for code, ts in con.execute("SELECT code, ts_start FROM faults WHERE source='tc_error' AND ts_start>=? AND ts_start<? ORDER BY ts_start", (a, b)):
        x = out.setdefault(code, [0, 0])
        x[0] += 1
        x[1] = max(x[1], ts)
    return [{"kod": k, "pocet": v[0], "posledne": v[1]} for k, v in sorted(out.items())]


# ---------------------------------------------------------------- správa
def report(cfg, now=None, con=None, lang="sk"):
    """Ukazovatele, základ a týždenný trend (bez zápisu do DB, okrem vytvorenia tabuliek)."""
    now = int(now or time.time())
    own = con is None
    con = con or _con(cfg)
    try:
        thr = prahy(cfg)
        thr["pp"] = _f(cfg, "ZDRAVIE_CYKLY_PP")
        today = _dt.date.fromtimestamp(now)
        r0 = _dt.timedelta(days=NEDAVNE_DNI)
        rec_a, rec_b = today - r0, today
        r = con.execute("SELECT min(ts) FROM hourly").fetchone()
        first = _dt.date.fromtimestamp(r[0]) if r and r[0] is not None else today
        base_a = max(first, rec_a - _dt.timedelta(days=ZAKLAD_DNI))
        base_b = rec_a
        load_a = min(base_a, today - _dt.timedelta(days=NEDAVNE_DNI * TYZDNE))
        base = _load(con, base_a, base_b) if base_b > base_a else {"hours": [], "cyc": [], "den": {}, "dni": 0}
        full = _load(con, load_a, today)
        a_ts = lambda d: stats._midnight(d)

        def window(d0, d1):
            x0, x1 = a_ts(d0), a_ts(d1)
            return {"hours": [h for h in full["hours"] if x0 <= h["ts"] < x1], "cyc": [c for c in full["cyc"] if x0 <= c["ts"] < x1],
                    "den": _den_window(con, d0, d1), "dni": 0}
        rec = window(rec_a, rec_b)
        dost = base["dni"] >= ZAKLAD_MIN_DNI
        weeks = []
        for i in range(TYZDNE - 1, -1, -1):
            weeks.append((today - _dt.timedelta(days=NEDAVNE_DNI * (i + 1)), today - _dt.timedelta(days=NEDAVNE_DNI * i)))
        wins = [window(x, y) for x, y in weeks] if dost else []
        inds = []
        for kod in KODY:
            c = _cmp(cfg, kod, base, rec) if dost else {"zmena": None, "biny": 0}
            st = _stav(kod, c, thr) if dost else "nodata"
            trend = []
            for w in wins:
                tc = _cmp(cfg, kod, base, w)
                trend.append(None if tc["zmena"] is None else round(tc["zmena"] * 100.0, 1))
            d = {"kod": kod, "nazov": tr("srv.zdravie.nazov_" + kod, lang), "popis": tr("srv.zdravie.popis_" + kod, lang), "stav": st,
                 "zmena_pct": None if c.get("zmena") is None else round(c["zmena"] * 100.0, 1),
                 "horsie_pct": None if _horsie(kod, c) is None else round(_horsie(kod, c) * 100.0, 1),
                 "prah_pct": thr[kod], "smer": "pokles" if SMER[kod] < 0 else "narast", "biny": c.get("biny", 0), "trend": trend}
            for k in ("nedavne", "zaklad", "pocet_den", "pocet_den_zaklad", "kratke_nedavne", "kratke_zaklad"):
                if k in c:
                    d[k] = c[k]
            for k in ("pocet_zmena", "trvanie_zmena", "kratke_pp"):
                if c.get(k) is not None:
                    d[k] = round(c[k] * 100.0, 1) if k != "kratke_pp" else round(c[k], 1)
            inds.append(d)
        chyby = _errors(con, a_ts(rec_a), a_ts(rec_b))
        stavy = [i["stav"] for i in inds]
        celk = "nodata" if not dost else ("crit" if "crit" in stavy else ("warn" if "warn" in stavy or chyby else "ok"))
        return {"ts": now, "dost_dat": dost, "stav": celk,
                "okno": {"nedavne": [rec_a.isoformat(), rec_b.isoformat()], "zaklad": [base_a.isoformat(), base_b.isoformat()],
                         "zaklad_dni": base["dni"], "zaklad_min_dni": ZAKLAD_MIN_DNI},
                "tyzdne": [x.isoformat() for x, _y in weeks], "indikatory": inds,
                "chyby": {"kody": chyby, "stav": "warn" if chyby else "ok"},
                "prahy": {k: thr[k] for k in KODY},
                "min_hodin": _f(cfg, "ZDRAVIE_MIN_HODIN")}
    finally:
        if own:
            con.close()


def _den_window(con, d0, d1):
    den = {}
    for ind, bn, n, s in con.execute("SELECT ind, bin, n, s FROM zdravie_den WHERE den>=? AND den<? AND ind!='_'", (d0.isoformat(), d1.isoformat())):
        den.setdefault((ind, bn), [0.0, 0.0])
        den[(ind, bn)][0] += n
        den[(ind, bn)][1] += s
    return den


# ---------------------------------------------------------------- nálezy
def _nalez_dict(r, lang):
    d = dict(zip(("id", "kod", "od", "do", "hodnota", "prah", "notified"), r))
    d["nazov"] = tr("srv.zdravie.nazov_" + d["kod"], lang) if d["kod"] in KODY else d["kod"]
    d["text"] = nalez_text(d["kod"], d["hodnota"], lang)
    d["aktivny"] = d["do"] is None
    return d


def nalez_text(kod, hodnota, lang="sk"):
    v = i18n.dec(("%.0f" % abs(hodnota or 0)), lang)
    return tr("srv.zdravie.nalez_" + kod, lang, v=v) if kod in KODY else str(kod)


_SEL = "SELECT id, kod, od, do, hodnota, prah, notified FROM zdravie_nalezy"


def nalezy(cfg, lang="sk", limit=30, con=None):
    own = con is None
    con = con or _con(cfg)
    try:
        akt = [_nalez_dict(r, lang) for r in con.execute(_SEL + " WHERE do IS NULL ORDER BY od")]
        his = [_nalez_dict(r, lang) for r in con.execute(_SEL + " WHERE do IS NOT NULL ORDER BY do DESC LIMIT ?", (limit,))]
        return {"aktivne": akt, "historia": his}
    finally:
        if own:
            con.close()


def active_texts(cfg, lang="sk"):
    """Texty aktívnych zistení (pre súhrn a vstup agenta); bez vytvárania stats.db, pri chybe prázdny zoznam."""
    try:
        if not enabled(cfg) or not os.path.exists(stats._db_path(cfg)):
            return []
        con = _con(cfg)
        try:
            return [x["text"] for x in nalezy(cfg, lang, con=con)["aktivne"]]
        finally:
            con.close()
    except Exception:
        return []


def apply_findings(cfg, rep, notify_fn=None, now=None, con=None):
    """Zosúladí tabuľku nálezov s ukazovateľmi: nový červený ukazovateľ = nový nález + jedno upozornenie, návrat do normy = zrušenie
    (+ upozornenie). notify_fn(kind, title, message) -> (ok, err) ako notify.event. Vracia {"nove": [...], "zrusene": [...]}."""
    now = int(now or time.time())
    lang = i18n.home_lang(cfg)
    own = con is None
    con = con or _con(cfg)
    res = {"nove": [], "zrusene": []}

    def send(title, msg):
        if notify_fn is None:
            return False
        try:
            ok, e = notify_fn("zdravie", title, msg)
        except Exception:
            return False
        return bool(ok or e == "nenastavené")
    try:
        if rep.get("dost_dat"):
            for ind in rep["indikatory"]:
                kod = ind["kod"]
                op = con.execute(_SEL + " WHERE kod=? AND do IS NULL", (kod,)).fetchone()
                h = ind.get("horsie_pct")
                if ind["stav"] == "crit":
                    val = h if h is not None else ind.get("kratke_pp") or 0.0
                    if kod == "cyklovanie" and (h is None or h < ind["prah_pct"]):
                        val = ind.get("kratke_pp") or val
                    if op is None:
                        cur = con.execute("INSERT INTO zdravie_nalezy(kod, od, hodnota, prah, notified) VALUES(?,?,?,?,0)",
                                          (kod, now, val, ind["prah_pct"]))
                        res["nove"].append(kod)
                        if send(tr("srv.zdravie.titul", lang), nalez_text(kod, val, lang)):
                            con.execute("UPDATE zdravie_nalezy SET notified=1 WHERE id=?", (cur.lastrowid,))
                    else:
                        con.execute("UPDATE zdravie_nalezy SET hodnota=? WHERE id=?", (val, op[0]))
                        if not op[6] and send(tr("srv.zdravie.titul", lang), nalez_text(kod, val, lang)):   # predošlé odoslanie zlyhalo
                            con.execute("UPDATE zdravie_nalezy SET notified=1 WHERE id=?", (op[0],))
                elif op is not None and h is not None and (h < HYST * ind["prah_pct"]) and ind["stav"] != "crit" and \
                        not (kod == "cyklovanie" and (ind.get("kratke_pp") or 0) >= HYST * _f(cfg, "ZDRAVIE_CYKLY_PP")):
                    con.execute("UPDATE zdravie_nalezy SET do=? WHERE id=?", (now, op[0]))
                    res["zrusene"].append(kod)
                    if op[6] and send(tr("srv.zdravie.ok_titul", lang), tr("srv.zdravie.ok_text", lang, nazov=tr("srv.zdravie.nazov_" + kod, lang))):
                        con.execute("UPDATE zdravie_nalezy SET clear_notified=1 WHERE id=?", (op[0],))
            # dosiahnutie zrušenia pri neodoslanom upozornení na zrušenie (najviac 3 dni staré)
            for rid, kod in con.execute("SELECT id, kod FROM zdravie_nalezy WHERE do IS NOT NULL AND notified=1 AND clear_notified=0 AND do>=?",
                                        (now - 3 * 86400,)).fetchall():
                if kod in res["zrusene"]:
                    continue
                if send(tr("srv.zdravie.ok_titul", lang), tr("srv.zdravie.ok_text", lang, nazov=tr("srv.zdravie.nazov_" + kod, lang))):
                    con.execute("UPDATE zdravie_nalezy SET clear_notified=1 WHERE id=?", (rid,))
        con.commit()
    finally:
        if own:
            con.close()
    return res


def run(cfg, now=None, notify_fn=None):
    """Celá denná úloha: denné súčty, ukazovatele, nálezy a upozornenia. Vracia {"report", "nalezy"}."""
    now = int(now or time.time())
    con = _con(cfg)
    try:
        update_days(cfg, now, con)
        rep = report(cfg, now, con, i18n.home_lang(cfg))
        r = apply_findings(cfg, rep, notify_fn, now, con)
        return {"report": rep, "zmeny": r}
    finally:
        con.close()


def tick(cfg, c, now=None, notify_fn=None):
    """Volá sa z watchdog_tick (každú minútu); raz denne po DAILY_AT. `c` = spojenie na agent.db (state)."""
    if not enabled(cfg) or not os.path.exists(stats._db_path(cfg)):      # stats.db vzniká v stats_loop; tu ju nezakladáme
        return None
    now = int(now or time.time())
    lt = time.localtime(now)
    today = time.strftime("%Y-%m-%d", lt)
    if (lt.tm_hour, lt.tm_min) < DAILY_AT or get_state(c, "zdravie_den") == today:
        return None
    set_state(c, "zdravie_den", today)
    return run(cfg, now, notify_fn)


def api(cfg, lang="sk", now=None):
    """GET /api/zdravie: ukazovatele, základ, týždenný trend a nálezy."""
    if not enabled(cfg):
        return {"enabled": False}
    con = _con(cfg)
    try:
        rep = report(cfg, now, con, lang)
        rep["enabled"] = True
        rep["nalezy"] = nalezy(cfg, lang, con=con)
        return rep
    finally:
        con.close()
