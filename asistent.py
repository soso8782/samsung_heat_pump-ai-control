#!/usr/bin/env python3
"""Asistent vyregulovania ventilov: deterministicky odporučí ďalší JEDEN krok na ventiloch VK 33 (SPEC, denník ventilov).
Vstup: priemery izieb za 7 dní (len kúrivé hodiny, vonku < 15 °C), denník ventilov s 48 h výsledkami (empirický účinok otáčky),
minimá izieb z profilu. Iba štandardná knižnica."""
import time
from common import REF_ROOM, ROOMS, ROOM_NAMES, get_targets, hour_stats
import i18n
from i18n import tr
import interventions as iv

DAYS = 7
HEAT_BELOW_C = 15.0           # kúrivé hodiny: vonkajšia teplota pod touto hranicou
MIN_HEAT_HOURS = 24           # minimum kúrivých hodín s dátami
BALANCED_SPREAD = 0.5         # °C: rozptyl priemerov izieb pod týmto = vyregulované
DEV_MIN = 0.25                # °C: odchýlka od priemeru izieb, od ktorej izbu riešime
PRIOR_PER_QUARTER = 0.3       # °C na ¼ otáčky, kým nie je žiadny vyhodnotený krok
SMALL_EFFECT = 0.15           # °C na ¼ otáčky: menší účinok = „malý“
BIG_DEV = 1.0                 # °C: odchýlka, od ktorej sa pri malom účinku odporučí ½ otáčky


def _n(x, nd=1, sign=False, lang="sk"):
    return i18n.dec(iv.sk(x, nd, sign), lang)


def room_means(cfg, now):
    """(priemery izieb za 7 dní v kúrivých hodinách, počet hodín)"""
    hours = [h for h in hour_stats(cfg, now - DAYS * 86400, now) if h.get("n") and h.get("vonku") is not None and h["vonku"] < HEAT_BELOW_C]
    means = {}
    for r in ROOMS:
        xs = [h[r] for h in hours if h.get(r) is not None]
        if len(xs) >= MIN_HEAT_HOURS // 2:
            means[r] = sum(xs) / len(xs)
    return means, len(hours)


def diary(c):
    """Platné záznamy denníka ventilov (so štruktúrovaným detailom), od najstaršieho."""
    return [x for x in reversed(iv.list_items(c, 500, kind="ventil")) if x["status"] != "zrusene" and x.get("detail") and x["room"] in ROOMS]


def best_valve(x):
    """Najlepší dostupný výsledok kroku: valve blok z 48 h, inak z 24 h; None ak nie je."""
    for res in (x.get("result48"), x.get("result")):
        v = (res or {}).get("valve")
        if v and v.get("verdict"):
            return v
    return None


def learn(items):
    """Empirický účinok (°C na otáčku otvorenia) po izbách a spolu z vyhodnotených krokov: izba − priemer ostatných / otáčky."""
    per, allv = {}, []
    for x in items:
        v, t = best_valve(x), x["detail"].get("turns")
        if not v or not t or v.get("room_vs_others_delta") is None:
            continue
        g = v["room_vs_others_delta"] / t
        per.setdefault(x["room"], []).append(g)
        allv.append(g)
    mean = lambda a: sum(a) / len(a)
    return {r: mean(g) for r, g in per.items()}, (mean(allv) if allv else None)


def pending(items, now):
    """Posledná zmena, ktorá ešte nie je vyhodnotená (čaká / < 48 h / bez 48 h výsledku); None ak žiadna."""
    cand = [x for x in items if (x["status"] == "caka" or x.get("result48") is None)
            and now < x["ts"] + iv.EVAL2_AFTER_S + iv.GIVE_UP_S]
    return cand[-1] if cand else None


def _last_step(items, room):
    """Posledný vyhodnotený krok v izbe: (otáčky, verdikt, zmena izby voči ostatným) alebo None."""
    for x in reversed(items):
        v = best_valve(x)
        if x["room"] == room and v:
            return x["detail"]["turns"], v["verdict"], v.get("room_vs_others_delta")
    return None


def recommend(cfg, c, now=None, lang=None):
    now = int(now or time.time())
    lang = lang or i18n.home_lang(cfg)
    n = lambda x, nd=1, sign=False: _n(x, nd, sign, lang)
    items = diary(c)
    out = {"ts": now, "status": None, "text": "", "headline": "", "reason": "", "effect": "", "notes": [], "step": None,
           "wait_until": None, "spread": None, "rooms": [], "controlling": REF_ROOM}
    p = pending(items, now)
    means, nh = room_means(cfg, now)
    mn = get_targets(c)
    cum = {r: round(sum(x["detail"]["turns"] for x in items if x["room"] == r), 2) for r in ROOMS}
    if len(means) >= 2:
        avg = sum(means.values()) / len(means)
        out["spread"] = round(max(means.values()) - min(means.values()), 2)
        out["rooms"] = [{"key": r, "name": ROOM_NAMES.get(r, r), "mean": round(m, 2), "dev": round(m - avg, 2),
                         "min": (mn.get(r) or {}).get("min"), "cum_turns": cum[r]} for r, m in means.items()]
    if p:
        out["status"] = "caka"
        due = p["ts"] + iv.EVAL2_AFTER_S
        out["wait_until"] = due
        when = time.strftime("%d.%m. %H:%M", time.localtime(due)) if due > now else tr("srv.asistent.po_vyhodnoteni", lang)
        out["reason"] = tr("srv.asistent.cakaj", lang, room=ROOM_NAMES.get(p["room"], p["room"]), change=p["detail"].get("change") or "", when=when)
        out["headline"] = tr("srv.asistent.h_cakaj", lang)
    elif len(means) < 2 or nh < MIN_HEAT_HOURS:
        out["status"] = "malo_dat"
        out["headline"] = tr("srv.asistent.h_malo", lang)
        out["reason"] = tr("srv.asistent.malo_dat", lang, h=nh, min=MIN_HEAT_HOURS)
    elif out["spread"] < BALANCED_SPREAD:
        out["status"] = "vyregulovane"
        out["headline"] = tr("srv.asistent.h_ok", lang)
        out["reason"] = tr("srv.asistent.vyregulovane", lang, spread=n(out["spread"], 2), lim=n(BALANCED_SPREAD, 1))
    else:
        _step(out, items, means, mn, cum, lang)
    out["text"] = " ".join(x for x in [out["headline"], out["reason"], out["effect"]] + out["notes"] if x)
    return out


def _step(out, items, means, mn, cum, lang):
    n = lambda x, nd=1, sign=False: _n(x, nd, sign, lang)
    avg = sum(means.values()) / len(means)
    per, glob = learn(items)
    notes, skipped = [], []

    def gain(r):                                  # °C na otáčku
        g = per.get(r, glob)
        if g is None or g <= 0.05:                # bez dát alebo nezmyselné znamienko: predpoklad
            return PRIOR_PER_QUARTER * 4, False
        return g, True

    def usable(r, direction):
        """(smie sa, násobok kroku, poznámky) podľa posledného vyhodnoteného kroku v izbe"""
        last = _last_step(items, r)
        mult, nt = 1, []
        if last:
            t, verdict, rd = last
            same = (t < 0) == (direction < 0)
            if same and verdict == "horsie":
                skipped.append(tr("srv.asistent.vynechane", lang, room=ROOM_NAMES.get(r, r), smer=tr("srv.asistent.smer_" + ("zatvor" if direction < 0 else "otvor"), lang), v=tr("srv.asistent.horsie", lang)))
                return False, 1, nt
            if same and verdict == "bez_zmeny":
                if rd is not None and abs(rd) < 0.1:
                    mult = 2
                    nt.append(tr("srv.asistent.vacsi", lang))
                else:
                    skipped.append(tr("srv.asistent.vynechane", lang, room=ROOM_NAMES.get(r, r), smer=tr("srv.asistent.smer_" + ("zatvor" if direction < 0 else "otvor"), lang), v=tr("srv.asistent.bez_zmeny", lang)))
                    return False, 1, nt
        return True, mult, nt

    warm = sorted(((m - avg, r) for r, m in means.items() if m - avg >= DEV_MIN), reverse=True)
    cold = sorted(((m - avg, r) for r, m in means.items() if m - avg <= -DEV_MIN))
    pick = None
    for dev, r in warm:
        ok, mult, nt = usable(r, -1)
        if not ok:
            continue
        g, emp = gain(r)
        steps = mult
        if abs(dev) > BIG_DEV and abs(g) * 0.25 < SMALL_EFFECT and mult == 1:
            steps = 2
            nt = nt + [tr("srv.asistent.vacsi", lang)]
        m = mn.get(r, {}).get("min")
        drop = abs(g) * 0.25 * steps
        if m is not None and means[r] - drop < m:        # po kroku by izba klesla pod minimum
            skipped.append(tr("srv.asistent.pod_min", lang, room=ROOM_NAMES.get(r, r), min=n(m, 1)))
            continue
        pick = (r, -1, steps, dev, g, emp, nt)
        break
    # studenú izbu otvárame, len ak nie je teplá izba na priškrtenie, alebo je omnoho výraznejšia
    if cold and (pick is None or abs(cold[0][0]) > 2 * pick[3]):
        for dev, r in cold:
            ok, mult, nt = usable(r, 1)
            if not ok:
                continue
            g, emp = gain(r)
            steps = mult
            if abs(dev) > BIG_DEV and abs(g) * 0.25 < SMALL_EFFECT and mult == 1:
                steps = 2
                nt = nt + [tr("srv.asistent.vacsi", lang)]
            pick = (r, 1, steps, dev, g, emp, nt)
            break
    out["notes"] = skipped
    if pick is None:
        out["status"] = "ziadny"
        out["headline"] = tr("srv.asistent.h_ziadny", lang)
        out["reason"] = tr("srv.asistent.ziadny", lang, spread=n(out["spread"], 2))
        return
    r, d, steps, dev, g, emp, nt = pick
    turns = d * 0.25 * steps
    eff = g * turns
    new = dict(means)
    new[r] = means[r] + eff
    new_spread = max(new.values()) - min(new.values())
    name = ROOM_NAMES.get(r, r)
    change = iv.fmt_turns(turns)
    out["status"] = "krok"
    out["step"] = {"room": r, "turns": turns, "change": change, "label": "VK 33", "note": tr("srv.asistent.poznamka", lang)}
    out["headline"] = tr("srv.asistent.h_krok", lang, room=name, change=change)
    out["reason"] = tr("srv.asistent.dovod_" + ("teplo" if d < 0 else "zima"), lang, room=name, dev=n(abs(dev), 1), mean=n(means[r], 1))
    if d < 0 and r == REF_ROOM:
        out["reason"] += " " + tr("srv.asistent.riadiaca", lang)
    out["effect"] = tr("srv.asistent.ucinok", lang, room=name, eff=n(eff, 2, True), a=n(out["spread"], 2), b=n(new_spread, 2),
                       zdroj=tr("srv.asistent.z_krokov" if emp else "srv.asistent.z_odhadu", lang, g=n(abs(g) * 0.25, 2)))
    if d > 0 and cum[r] >= 0:
        nt = nt + [tr("srv.asistent.neznama_poloha", lang)]
    elif d > 0:
        nt = nt + [tr("srv.asistent.vratenie", lang, v=n(cum[r], 2, True))]
    out["notes"] = nt + skipped


def llm_line(cfg, c, now=None, lang="sk"):
    """Jeden riadok pre týždenný súhrn a vstup modelu."""
    try:
        if not diary(c):       # asistent je nepovinný: do súhrnu a vstupu modelu ide, až keď majiteľ vedie denník ventilov
            return None
        r = recommend(cfg, c, now, lang)
    except Exception:
        return None
    return tr("srv.asistent.riadok", lang, v=r["text"]) if r["text"] else None


def with_line(cfg, c, text, now=None, lang="sk"):
    """`text` doplnený o riadok asistenta (ak je k dispozícii)."""
    line = llm_line(cfg, c, now, lang)
    return text + "\n" + line if line else text
