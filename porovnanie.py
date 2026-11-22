"""Porovnanie riadiacich čidiel: ako sa kúrilo pri jednotlivých možnostiach selectu „Riadiaca izba“ (len čítanie, nič nezapisuje).

Obdobie sa rozdelí podľa prepnutí (riadiaca_prepnutia), úseky kratšie ako MIN_PERIOD_H sa vynechajú, hodiny bez kúrenia
(vonku >= WARM_OUT) tiež; úseky rovnakej možnosti sa spočítajú dokopy. Hlavné kritérium je rozptyl izbových teplôt."""
import sqlite3, time

import agent as _agent
import profil
from common import S, ROOMS, adb, get_targets, tcq

MIN_PERIOD_H = 6        # kratšie úseky sa preskočia
WARM_OUT = 18.0         # hodina s vonkajšou teplotou nad touto hodnotou = kúrenie vypnuté, nepočíta sa
SHORT_MIN = 10.0        # cyklus kompresora kratší ako toto (min) je krátky
MIN_DATA_H = 48         # pod týmto počtom hodín je možnosť „málo dát“
OUT_DIFF = 3.0          # rozdiel vonkajších teplôt možností (°C), od ktorého sa podmienky považujú za rôzne
CACHE_S = 600
_cache = {}


# jediná definícia je v agent.py (používa ju aj týždenný súhrn); tu len odkaz kvôli spätnej kompatibilite
RIADIACA_MATCH_S = _agent.RIADIACA_MATCH_S
riadiaca_prepnutia = _agent.riadiaca_prepnutia


def split_periods(zac, zmeny, a, b, min_h=MIN_PERIOD_H):
    """[(od, do, možnosť)] z počiatočnej možnosti a prepnutí; úseky kratšie ako min_h hodín a úseky bez známej možnosti sa vynechajú."""
    pts, cur = [], zac
    t0 = a
    for z in zmeny:
        if z["ts"] <= a:
            cur = z["novy"]
            continue
        pts.append((t0, z["ts"], cur)); t0, cur = z["ts"], z["novy"]
    pts.append((t0, b, cur))
    return [(s, e, o) for s, e, o in pts if o and e - s >= min_h * 3600]


def _hourly(cfg, a, b):
    """{hodina: {kľúč: priemer}} z tc.db pre izby, cieľ a vonkajšiu teplotu (neplatné hodnoty mimo 0–45 °C sa zahodia)."""
    ents = {S[k]: k for k in list(ROOMS) + ["ciel", "vonku"] if k in S}
    if not ents:
        return {}
    rows = tcq(cfg, "SELECT entity, ts/3600, avg(value) FROM samples WHERE ts>=? AND ts<? AND entity IN (%s) GROUP BY 1,2" % ",".join("?" * len(ents)),
               (a, b) + tuple(ents))
    out = {}
    for e, h, v in rows:
        if v is not None and -40 <= v <= 45 and (ents[e] == "vonku" or 0 < v):
            out.setdefault(int(h), {})[ents[e]] = v
    return out


def _cycles(cfg, a, now):
    try:
        import stats
        return [c for c in stats.compute_cycles(cfg, a, now) if c.get("mode") in ("kurenie", "zmiesane") and a <= c["ts_start"]]
    except Exception:
        return None


def _energia(cfg, a, b):
    """{hodina: (kwh, q_kwh)} z hodinových štatistík (stats.db); bez nich prázdne."""
    try:
        import stats
        con = stats._ro(stats._db_path(cfg))
        try:
            return {int(t) // 3600: (kc if kc is not None else k, q) for t, k, kc, q in
                    con.execute("SELECT ts, kwh, kwh_cal, q_kwh FROM hourly WHERE ts>=? AND ts<?", (a, b))}
        finally:
            con.close()
    except Exception:
        return {}


def compute(cfg, a, b, prep=None, now=None):
    """Porovnanie možností za [a, b): {od, do, moznosti:[...], poznamka}; `prep` = výsledok riadiaca_prepnutia."""
    now = int(now or time.time())
    if prep is None:
        c = adb(cfg)
        try:
            prep = riadiaca_prepnutia(cfg, c, a, b)
        finally:
            c.close()
    izby = profil.load()["izby"]
    periods = split_periods(prep.get("zaciatok"), prep.get("zmeny") or [], a, b)
    try:
        hodiny = _hourly(cfg, a, b)
    except Exception:
        hodiny = {}
    cyc = _cycles(cfg, a, now)
    en = _energia(cfg, a, b)
    try:
        c = adb(cfg)
        try:
            tg = get_targets(c)
        finally:
            c.close()
    except Exception:
        tg = {}
    agg = {}
    for s, e, opt in periods:
        m = _agent.riadiaca_mapa([opt], izby)
        kl, cid = (m[0]["kluc"], m[0]["cidlo"]) if m else (None, "")
        g = agg.setdefault(opt, {"kluc": kl, "cidlo": cid, "h": [], "spread": [], "dev": [], "out": [], "starty": 0, "kratke": 0,
                                  "kwh": 0.0, "q": 0.0, "kwh_h": 0, "kwh_q": 0.0})
        hs = set()
        for h in range((s + 3599) // 3600, e // 3600):
            d = hodiny.get(h)
            if not d:
                continue
            temps = [d[k] for k in ROOMS if k in d]
            if not temps or (d.get("vonku") is not None and d["vonku"] >= WARM_OUT):
                continue
            hs.add(h)
            g["h"].append(h)
            if len(temps) >= 2:
                g["spread"].append(max(temps) - min(temps))
            if d.get("vonku") is not None:
                g["out"].append(d["vonku"])
            tgt = d.get("ciel")
            if kl in d and tgt is not None:
                g["dev"].append(abs(d[kl] - tgt))
            elif kl in d and kl in tg:
                g["dev"].append(abs(d[kl] - tg[kl]["min"]))
            if h in en and en[h][0] is not None:
                g["kwh"] += en[h][0]; g["kwh_h"] += 1
                if en[h][1] is not None and en[h][0] >= 0.05:
                    g["q"] += en[h][1]; g["kwh_q"] += en[h][0]
        if cyc:
            for c_ in cyc:
                if s <= c_["ts_start"] < e and c_["ts_start"] // 3600 in hs:
                    g["starty"] += 1
                    if (c_.get("minutes") or 0) < SHORT_MIN:
                        g["kratke"] += 1

    def avg(xs, nd):
        return round(sum(xs) / len(xs), nd) if xs else None
    out = []
    for opt, g in agg.items():
        n = len(g["h"])
        out.append({"moznost": opt, "kluc": g["kluc"], "cidlo": g["cidlo"], "hodin": n, "rozptyl": avg(g["spread"], 2), "odchylka": avg(g["dev"], 2),
                    "starty_h": round(g["starty"] / n, 2) if n and cyc is not None else None, "kratke": g["kratke"] if cyc is not None else None,
                    "kwh_den": round(g["kwh"] / g["kwh_h"] * 24, 1) if g["kwh_h"] else None,
                    "cop": round(g["q"] / g["kwh_q"], 2) if g["kwh_q"] else None, "vonku": avg(g["out"], 1)})
    out = [x for x in out if x["hodin"]]
    out.sort(key=lambda x: (x["rozptyl"] is None, x["rozptyl"] or 0))
    return {"od": a, "do": b, "moznosti": out, "poznamka": ""}


def get(cfg, days=30, now=None, lang="sk"):
    """Cache ~10 min podľa (dni, DB)."""
    now = int(now or time.time())
    days = max(1, min(180, int(days)))
    key = (days, cfg.get("TC_DB"), lang)
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_S:
        return hit[1]
    r = compute(cfg, now - days * 86400, now, now=now)
    from i18n import tr
    r["poznamka"] = tr("srv.porovnanie.poznamka", lang)
    _cache[key] = (now, r)
    return r


def agent_line(cfg, lang="sk", now=None):
    """Jeden riadok pre vstup agenta, ak aspoň 2 možnosti majú >= MIN_DATA_H hodín; inak None."""
    from i18n import tr
    r = get(cfg, 30, now, lang)
    ok = [x for x in r["moznosti"] if x["hodin"] >= MIN_DATA_H and x["rozptyl"] is not None]
    if len(ok) < 2:
        return None
    parts = ["%s: %s K, %s h" % (x["moznost"], x["rozptyl"], x["hodin"]) for x in ok]
    return tr("srv.prompt.porovnanie_riad", lang, items="; ".join(parts), best=ok[0]["moznost"])
