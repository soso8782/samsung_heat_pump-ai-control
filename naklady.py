#!/usr/bin/env python3
"""Odhad nákladov a prehľad vykurovacej sezóny (len na čítanie): GET /api/naklady/odhad a GET /api/sezona.

Model spotreby čerpadla za deň: kWh/deň = a + b · HDD, HDD = max(0, 15 °C − priemerná vonkajšia teplota dňa). Koeficienty sa fitujú
metódou najmenších štvorcov z posledných FIT_DAYS dní (stats.db, tabuľka hourly; deň musí mať aspoň 20 hodín spotreby a 18 hodín teploty).
Člen `a` je základ nezávislý od počasia (hlavne ohrev TÚV a pohotovosť), `b` je spotreba na jeden stupňodeň; a, b >= 0.
Teplota do budúcnosti: predpoveď z Open-Meteo (denný priemer, max 16 dní; chyba/neznámo = bez predpovede), ďalej klimatologický priemer
toho istého dňa z vlastnej histórie (okno ±3 dni, predchádzajúce roky, aspoň CLIM_MIN_N dní), inak mesačné normály pre Slovensko (NORMALY).
Cena = kWh × vážená cena ha_cost.tariff_at (podiel VT z ENERGY_VT_SHARE; elektromer VT/NT po hodinách nerozlišuje), bez fixnej platby.
Interval ± je 1 sigma: nezávislá chyba modelu za deň (rezíduá fitu) + spoločná chyba teploty (predpoveď 1,5 °C, klimatológia 2 °C za mesiac).
Iba štandardná knižnica."""
import datetime as _dt, json, math, time, urllib.request

import ha_cost
import stats
from i18n import tr

BASE_C = 15.0                 # základ stupňodní (°C)
FIT_DAYS = 60
MIN_FIT_DAYS = 10
MIN_H_KWH, MIN_H_TEMP = 20, 18
CLIM_MIN_N = 5
SIGMA_PRED, SIGMA_CLIM = 1.5, 2.0   # °C
FC_CACHE_S = 3 * 3600
# mesačné normály vonkajšej teploty (°C), nížiny Slovenska, odhad z dlhodobých priemerov 1991–2020 (SHMÚ; zaokrúhlené, orientačné);
# hodnota patrí do 15. dňa mesiaca, medzi nimi sa interpoluje lineárne
NORMALY = {1: -1.5, 2: 0.5, 3: 4.5, 4: 10.0, 5: 15.0, 6: 18.5, 7: 20.5, 8: 20.0, 9: 15.0, 10: 9.5, 11: 4.5, 12: 0.5}
SEZONA_OD, SEZONA_DO, NAKLADY_DO = "09-01", "05-31", "05-15"
EVENT_KINDS = ("ventil", "krivka")
_fc_cache = {}


# ---------------------------------------------------------------- pomocné
def _md(cfg, key, default):
    """(mesiac, deň) z config.env 'MM-DD'; neplatné = predvolené."""
    s = str((cfg or {}).get(key) or default).strip()
    try:
        m, d = s.split("-")
        _dt.date(2024, int(m), int(d))
        return int(m), int(d)
    except ValueError:
        m, d = default.split("-")
        return int(m), int(d)


def _safe(y, m, d):
    try:
        return _dt.date(y, m, d)
    except ValueError:
        return _dt.date(y, m, 28)


def _r(v, n=2):
    return None if v is None else round(v, n)


def _dstr(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _rows(cfg, a, b, extra=()):
    """[(ts, čerpadlo kWh, q_kwh, vonku, *extra)] pre [a, b) jedným SELECTom; pri chýbajúcej DB/stĺpcoch prázdne."""
    try:
        con = stats._ro(stats._db_path(cfg), stats._timeout(cfg))
    except Exception:
        return []
    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(hourly)")}
        if "ts" not in have:
            return []
        sel = lambda c: c if c in have else "NULL"
        pump = "COALESCE(%s, %s)" % (sel("kwh_cal"), sel("kwh")) if ("kwh_cal" in have or "kwh" in have) else "NULL"
        cols = ", ".join([pump, sel("q_kwh"), sel("vonku")] + [sel(c) for c in extra])
        return con.execute("SELECT ts, %s FROM hourly WHERE ts>=? AND ts<? ORDER BY ts" % cols, (a, b)).fetchall()
    except Exception:
        return []
    finally:
        con.close()


def _by_day(rows):
    """{'YYYY-MM-DD': {kwh, nk, q, tsum, nt}} z hodinových riadkov."""
    out = {}
    for r in rows:
        d = out.setdefault(_dstr(r[0]), {"kwh": 0.0, "nk": 0, "q": 0.0, "tsum": 0.0, "nt": 0})
        if r[1] is not None:
            d["kwh"] += max(0.0, r[1])
            d["nk"] += 1
        if r[2] is not None:
            d["q"] += r[2]
        if r[3] is not None:
            d["tsum"] += r[3]
            d["nt"] += 1
    return out


def _hdd(t, base=BASE_C):
    return max(0.0, base - t)


def _tmean(d):
    return d["tsum"] / d["nt"] if d["nt"] >= MIN_H_TEMP else None


def _price(cfg, day):
    """Vážená cena €/kWh s DPH pre lokálny deň 'YYYY-MM-DD' (cena sa v rámci dňa nemení)."""
    y, m, d = map(int, day.split("-"))
    return ha_cost.tariff_at(cfg, int(time.mktime((y, m, d, 12, 0, 0, 0, 0, -1))))[0]


# ---------------------------------------------------------------- model
def fit(days, base=BASE_C):
    """days = {datum: deň z _by_day}; používajú sa len dni s dostatkom hodín. Vracia {a, b, sigma, n, r2, base} alebo None (< MIN_FIT_DAYS dní).
    MNŠ kWh = a + b·HDD s a >= 0 a b >= 0 (pri zápornej hodnote sa druhý koeficient preráta s pevným prvým)."""
    pts = []
    for d in days.values():
        t = _tmean(d)
        if t is not None and d["nk"] >= MIN_H_KWH:
            pts.append((_hdd(t, base), d["kwh"]))
    n = len(pts)
    if n < MIN_FIT_DAYS:
        return None
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    if sxx < 1e-9:
        a, b = my, 0.0
    else:
        b = sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx
        a = my - b * mx
        if b < 0:
            a, b = my, 0.0
        elif a < 0:
            a, b = 0.0, sum(p[0] * p[1] for p in pts) / sum(p[0] ** 2 for p in pts)
    res = [p[1] - (a + b * p[0]) for p in pts]
    sigma = math.sqrt(sum(x * x for x in res) / max(1, n - 2))
    syy = sum((p[1] - my) ** 2 for p in pts)
    r2 = 1.0 - sum(x * x for x in res) / syy if syy > 1e-9 else 0.0
    return {"a": a, "b": b, "sigma": sigma, "n": n, "r2": r2, "base": base}


def predict(model, t):
    return model["a"] + model["b"] * _hdd(t, model["base"])


def normal(d):
    """Mesačný normál teploty pre dátum d (lineárna interpolácia medzi 15. dňami mesiacov)."""
    pos = d.month + (d.day - 15) / 30.0
    m0 = int(math.floor(pos))
    f = pos - m0
    a, b = NORMALY[(m0 - 1) % 12 + 1], NORMALY[m0 % 12 + 1]
    return a + (b - a) * f


def climate(days, d, today):
    """Priemerná teplota dňa d z vlastných dní z predchádzajúcich rokov (±3 dni); None pri < CLIM_MIN_N dňoch."""
    xs = []
    for y in range(d.year - 6, d.year):
        for k in range(-3, 4):
            try:
                c = _safe(y, d.month, d.day) + _dt.timedelta(days=k)
            except ValueError:
                continue
            if c >= today - _dt.timedelta(days=FIT_DAYS):
                continue
            x = days.get(c.isoformat())
            t = _tmean(x) if x else None
            if t is not None:
                xs.append(t)
    return sum(xs) / len(xs) if len(xs) >= CLIM_MIN_N else None


def fetch_forecast(cfg):
    """{'YYYY-MM-DD': denný priemer °C} z Open-Meteo (až 16 dní); pri chybe alebo bez LAT/LON prázdny. Cache 3 h."""
    if not cfg.get("LAT") or not cfg.get("LON"):
        return {}
    key = (cfg["LAT"], cfg["LON"])
    c = _fc_cache.get(key)
    if c and time.time() - c[0] < FC_CACHE_S:
        return c[1]
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s&daily=temperature_2m_mean&forecast_days=16&timezone=auto"
           % (cfg["LAT"], cfg["LON"]))
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            j = json.loads(r.read().decode("utf-8"))["daily"]
        out = {d: float(t) for d, t in zip(j["time"], j["temperature_2m_mean"]) if t is not None}
    except Exception:
        return {}
    _fc_cache[key] = (time.time(), out)
    return out


def season_start_for(cfg, d):
    """Začiatok (date) vykurovacej sezóny, do ktorej patrí deň d, alebo None, ak d je mimo sezóny (podľa konca odhadu NAKLADY_DO)."""
    ms, ds = _md(cfg, "SEZONA_OD", SEZONA_OD)
    me, de = _md(cfg, "NAKLADY_DO", NAKLADY_DO)
    for y in (d.year, d.year - 1):
        s = _safe(y, ms, ds)
        e = _safe(y + (1 if (me, de) < (ms, ds) else 0), me, de)
        if s <= d <= e:
            return s
    return None


def _season_end(cfg, s):
    ms, ds = _md(cfg, "SEZONA_OD", SEZONA_OD)
    me, de = _md(cfg, "NAKLADY_DO", NAKLADY_DO)
    return _safe(s.year + (1 if (me, de) < (ms, ds) else 0), me, de)


def _project(cfg, model, days, fc, today, end, frac_today):
    """Odhad od dnes (len zvyšok dňa) po end vrátane: (kwh, eur, variancia kWh², dni)."""
    kwh = eur = var = 0.0
    n = 0
    groups = {}
    d = today
    while d <= end:
        t, src = (fc.get(d.isoformat()), "pred")
        if t is None:
            t, src = climate(days, d, today), "klim"
        if t is None:
            t, src = normal(d), "klim"
        w = frac_today if d == today else 1.0
        k = predict(model, t) * w
        kwh += k
        eur += k * _price(cfg, d.isoformat())
        var += (model["sigma"] * w) ** 2
        if _hdd(t, model["base"]) > 0:
            g = ("pred",) if src == "pred" else ("klim", d.year, d.month)
            groups.setdefault(g, [0.0, SIGMA_PRED if src == "pred" else SIGMA_CLIM])[0] += w
        n += 1
        d += _dt.timedelta(days=1)
    for cnt, sg in groups.values():
        var += (model["b"] * sg * cnt) ** 2
    return kwh, eur, var, n


def _period(cfg, model, days, fc, today, frac_today, od, do):
    """Obdobie od–do (date): už spotrebované (z DB do teraz) + odhad zvyšku, s ± (1 sigma)."""
    uz_k = uz_e = 0.0
    nd = 0
    d = od
    while d <= min(do, today):
        x = days.get(d.isoformat())
        if x and x["nk"]:
            uz_k += x["kwh"]
            uz_e += x["kwh"] * _price(cfg, d.isoformat())
            nd += 1
        d += _dt.timedelta(days=1)
    k, e, var, n = _project(cfg, model, days, fc, today, do, frac_today) if do >= today else (0.0, 0.0, 0.0, 0)
    sd = math.sqrt(var)
    pe = sd * (e / k) if k > 0 else 0.0
    return {"od": od.isoformat(), "do": do.isoformat(), "dni_zvysok": n, "dni_s_udajmi": nd,
            "uz_kwh": _r(uz_k, 1), "uz_eur": _r(uz_e), "zvysok_kwh": _r(k, 1), "zvysok_eur": _r(e),
            "spolu_kwh": _r(uz_k + k, 1), "spolu_eur": _r(uz_e + e), "pm_kwh": _r(sd, 1), "pm_eur": _r(pe)}


def odhad(cfg, now=None, fetch=None, lang="sk"):
    """Odhad nákladov čerpadla: zvyšok mesiaca, fakturačné obdobie a zvyšok vykurovacej sezóny (do NAKLADY_DO, predvolene 15. 5.).
    fetch(cfg) -> {datum: priemerná teplota}; predvolene Open-Meteo. Bez dosť dát vráti {"model": None, "dost_dat": False, "text": ...}."""
    import ha_pub
    now = int(now or time.time())
    today = _dt.date.fromtimestamp(now)
    days = _by_day(_rows(cfg, 0, now + 1))
    w0 = (today - _dt.timedelta(days=FIT_DAYS)).isoformat()
    m = fit({k: v for k, v in days.items() if w0 <= k < today.isoformat()})
    if m is None:
        return {"dost_dat": False, "model": None, "text": tr("srv.naklady.malo_dat", lang, n=MIN_FIT_DAYS)}
    try:
        fc = (fetch or fetch_forecast)(cfg) or {}
    except Exception:
        fc = {}
    lt = time.localtime(now)
    frac = max(0.0, 1.0 - (lt.tm_hour + lt.tm_min / 60.0) / 24.0)
    bs = _dt.date.fromtimestamp(ha_pub.billing_start(cfg, now))
    b_end = _safe(bs.year + 1, bs.month, bs.day) - _dt.timedelta(days=1)
    m_end = (today.replace(day=28) + _dt.timedelta(days=4)).replace(day=1) - _dt.timedelta(days=1)
    out = {"dost_dat": True, "datum": today.isoformat(), "predpoved_dni": sum(1 for k in fc if k >= today.isoformat()),
           "model": {"a_kwh_den": _r(m["a"], 2), "b_kwh_hdd": _r(m["b"], 3), "sigma_kwh_den": _r(m["sigma"], 2), "dni": m["n"],
                     "r2": _r(m["r2"], 2), "zaklad_c": m["base"], "okno_dni": FIT_DAYS},
           "cena_kwh": _r(_price(cfg, today.isoformat()), 4),
           "mesiac": _period(cfg, m, days, fc, today, frac, today.replace(day=1), m_end),
           "fakturacne_obdobie": _period(cfg, m, days, fc, today, frac, bs, b_end)}
    s = season_start_for(cfg, today)
    if s is None:
        ms, ds = _md(cfg, "SEZONA_OD", SEZONA_OD)
        out["sezona"] = {"mimo_sezony": True, "dalsia_od": _safe(today.year if (today.month, today.day) < (ms, ds) else today.year + 1, ms, ds).isoformat()}
    else:
        out["sezona"] = dict(_period(cfg, m, days, fc, today, frac, s, _season_end(cfg, s)), mimo_sezony=False)
    return out


# ---------------------------------------------------------------- prehľad sezóny
def season_range(cfg, rok):
    """(začiatok, koniec) sezóny, ktorá sa začína v roku rok (SEZONA_OD .. SEZONA_DO, predvolene 1. 9. – 31. 5.)."""
    ms, ds = _md(cfg, "SEZONA_OD", SEZONA_OD)
    me, de = _md(cfg, "SEZONA_DO", SEZONA_DO)
    return _safe(rok, ms, ds), _safe(rok + (1 if (me, de) < (ms, ds) else 0), me, de)


def default_rok(cfg, today):
    """Rok začiatku sezóny: prebiehajúca, inak posledná skončená."""
    s = season_range(cfg, today.year)[0]
    return today.year if today >= s else today.year - 1


def _summary(cfg, od, do, rooms):
    a, b = stats._midnight(od), stats._midnight(do + _dt.timedelta(days=1))
    rows = _rows(cfg, a, b, extra=("starty", "odmraz_min") + tuple(rooms))
    nr = len(rooms)
    days = _by_day(rows)
    pump = [r for r in rows if r[1] is not None]
    if not pump:
        return {"ma_data": False, "od": od.isoformat(), "do": do.isoformat()}
    kwh = sum(max(0.0, r[1]) for r in pump)
    q = d = 0.0
    for r in rows:
        if r[2] is not None and r[1] is not None and r[1] >= 0.05:
            q += r[2]
            d += r[1]
    cost = sum(max(0.0, r[1]) * _price(cfg, _dstr(r[0])) for r in pump)
    hdd = kwh_m = hdd_m = 0.0
    n_m = 0
    for k, x in days.items():
        t = _tmean(x)
        if t is None:
            continue
        hdd += _hdd(t)
        if x["nk"] >= MIN_H_KWH:
            kwh_m += x["kwh"]
            hdd_m += _hdd(t)
            n_m += 1
    izby, spr = [], []
    for i, k in enumerate(rooms):
        v = [r[6 + i] for r in rows if r[6 + i] is not None]
        izby.append({"kluc": k, "priemer": _r(sum(v) / len(v), 2) if v else None})
    for r in rows:
        v = [x for x in r[6:6 + nr] if x is not None]
        if len(v) >= 2:
            spr.append(max(v) - min(v))
    vt = [r[3] for r in rows if r[3] is not None]
    return {"ma_data": True, "od": od.isoformat(), "do": do.isoformat(), "dni_s_udajmi": sum(1 for x in days.values() if x["nk"] >= MIN_H_KWH),
            "kwh": _r(kwh, 1), "teplo_kwh": _r(q, 1) if d > 0 else None, "cop": _r(q / d, 2) if d > 0 else None, "cena_eur": _r(cost),
            "hdd": _r(hdd, 1), "kwh_hdd": _r(kwh_m / hdd_m, 3) if hdd_m >= 20 and n_m >= 10 else None,
            "vonku": _r(sum(vt) / len(vt), 1) if vt else None, "izby": izby, "rozptyl": _r(sum(spr) / len(spr), 2) if spr else None,
            "starty": int(sum(r[4] or 0 for r in rows)), "odmraz_min": _r(sum(r[5] or 0 for r in rows), 0)}


def _events(cfg, c, a, b, lang):
    import interventions
    own = False
    if c is None:
        try:
            import common
            c = common.adb(cfg)
            own = True
        except Exception:
            return []
    try:
        out = []
        for iv in reversed(interventions.list_items(c, 200, a, b)):
            if iv["kind"] not in EVENT_KINDS or iv["status"] == "zrusene":
                continue
            txt = interventions.valve_line(iv, lang) if iv["kind"] == "ventil" else iv["text"]
            out.append({"ts": iv["ts"], "datum": _dstr(iv["ts"]), "druh": iv["kind"], "izba": iv["room"], "text": txt})
        return out
    except Exception:
        return []
    finally:
        if own:
            c.close()


def sezona(cfg, rok=None, now=None, c=None, lang="sk"):
    """Prehľad sezóny začínajúcej v roku rok (None = prebiehajúca/posledná): súčty, normalizácia podľa HDD, porovnanie s predošlou sezónou
    a zoznam udalostí (ventily, krivka). Neplatný rok vyhodí ValueError."""
    import vonku, profil
    now = int(now or time.time())
    today = _dt.date.fromtimestamp(now)
    if rok in (None, ""):
        rok = default_rok(cfg, today)
    try:
        rok = int(rok)
        if not 2000 <= rok <= 2100:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError(tr("srv.sezona.rok", lang))
    rooms = list(profil.room_keys(vonku.P))
    od, do = season_range(cfg, rok)
    cur = _summary(cfg, od, min(do, today), rooms) if od <= today else {"ma_data": False, "od": od.isoformat(), "do": do.isoformat()}
    cur["do"] = do.isoformat()
    cur["prebieha"] = od <= today <= do
    cur["rok"] = rok
    cur["nazov"] = "%d/%d" % (od.year, do.year) if do.year != od.year else str(od.year)
    if cur["ma_data"]:
        for x in cur["izby"]:
            x["nazov"] = vonku.NAZVY.get(x["kluc"], x["kluc"])
    po, pd = season_range(cfg, rok - 1)
    prev = _summary(cfg, po, pd, rooms)
    out = {"sezona": cur, "predchadzajuca": None, "porovnanie": None, "udalosti": []}
    if prev["ma_data"]:
        prev["rok"] = rok - 1
        prev["nazov"] = "%d/%d" % (po.year, pd.year) if pd.year != po.year else str(po.year)
        out["predchadzajuca"] = prev
    if cur["ma_data"] and prev["ma_data"] and cur.get("kwh_hdd") and prev.get("kwh_hdd"):
        pc = lambda x, y: _r((x - y) / y * 100.0, 1) if y else None
        out["porovnanie"] = {"kwh_hdd_zmena_pct": pc(cur["kwh_hdd"], prev["kwh_hdd"]), "kwh_zmena_pct": pc(cur["kwh"], prev["kwh"]),
                             "hdd_zmena_pct": pc(cur["hdd"], prev["hdd"]),
                             "cop_zmena": _r(cur["cop"] - prev["cop"], 2) if cur.get("cop") is not None and prev.get("cop") is not None else None,
                             "kwh_pri_starej_ucinnosti": _r(prev["kwh_hdd"] * cur["hdd"], 1),
                             "uspora_kwh": _r(prev["kwh_hdd"] * cur["hdd"] - cur["kwh"], 1)}
    out["udalosti"] = _events(cfg, c, stats._midnight(od), stats._midnight(do + _dt.timedelta(days=1)), lang)
    return out


def sezona_suhrn(cfg, c, sun, now, lang="sk"):
    """Odstavec pre týždenný súhrn len raz: prvý súhrn po skončení sezóny (koniec sezóny <= nedeľa súhrnu, najviac 60 dní dozadu a
    ešte neodoslaný, stav `sezona_suhrn`). Vracia {rok, text} alebo None."""
    from common import get_state
    for rok in (sun.year - 1, sun.year):
        od, do = season_range(cfg, rok)
        if do <= sun and (sun - do).days < 60 and get_state(c, "sezona_suhrn") != str(rok):
            r = sezona(cfg, rok, now, c, lang)
            s = r["sezona"]
            if not s["ma_data"]:
                return None
            return {"rok": rok, "text": sezona_text(r, lang)}
    return None


def sezona_text(r, lang="sk"):
    s, n = r["sezona"], (lambda v, k=1: i18n_dec(v, k, lang))
    t = tr("srv.sezona.suhrn", lang, nazov=s["nazov"], kwh=n(s["kwh"]), cop=n(s["cop"], 2) if s.get("cop") is not None else "–",
           eur=n(s["cena_eur"], 0), hdd=n(s["hdd"], 0))
    p = r.get("porovnanie")
    if p and p.get("kwh_hdd_zmena_pct") is not None:
        t += " " + tr("srv.sezona.suhrn_por", lang, pct=n(abs(p["kwh_hdd_zmena_pct"])),
                      smer=tr("srv.sezona.menej" if p["kwh_hdd_zmena_pct"] < 0 else "srv.sezona.viac", lang))
    return t


def i18n_dec(v, k, lang):
    import i18n
    return i18n.dec(("%.*f" % (k, v)).replace(".", ","), lang)
