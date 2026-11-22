#!/usr/bin/env python3
"""Zásahy (ručné aj agentove) a ich deterministické vyhodnotenie po 24 hodinách (SPEC bod 3). Iba štandardná knižnica."""
import json, re, time
from common import REF_NAME, REF_ROOM, ROOMS, ROOM_NAMES, SETTINGS, get_state, hour_stats, set_state, tcq
import contacts, notify
import i18n
from i18n import tr
from common import adb

SOURCES = ("user_message", "user_manual", "agent", "settings")
KINDS = ("ventil", "krivka", "nastavenie", "vetranie", "ine")
EVAL_AFTER_S = 24 * 3600
MIN_HOURS = 20                      # minimum hodín s dátami v každom okne
GIVE_UP_S = 72 * 3600               # ak ani 72 h po termíne nie sú dáta, zásah sa uzavrie ako "nedostatok dát"
CURVE_ENTITIES = tuple(SETTINGS[k] for k in ("fsv2031", "fsv2032") if k in SETTINGS)
EVAL2_AFTER_S = 48 * 3600           # druhé (spoľahlivejšie) vyhodnotenie ventilu
VALVE_THRESHOLD = 0.2               # °C: zmena rozptylu izieb aspoň takáto = lepšie / horšie
WEATHER_DIFF_C = 3.0                # °C: rozdiel vonkajšej teploty medzi oknami, od ktorého je porovnanie menej spoľahlivé
AGENT_MATCH_S = 180                 # zmena nastavenia do ±3 min od akcie agenta = zmena agenta


def sk(x, nd=1, sign=False):
    """Číslo so slovenskou desatinnou čiarkou; None = '–'."""
    if x is None:
        return "–"
    return (("%+.*f" % (nd, x)) if sign else ("%.*f" % (nd, x))).replace(".", ",")


def _js(x):
    try:
        return json.loads(x) if x else None
    except ValueError:
        return None


def row_dict(r, full=True):
    d = dict(zip(("id", "ts", "source", "kind", "room", "text", "status", "due_ts", "result", "notified", "detail", "result48"), r))
    d["result"] = _js(d["result"])
    d["result48"] = _js(d["result48"])
    d["detail"] = _js(d["detail"])
    d.pop("notified", None)
    return d


COLS = "id,ts,source,kind,room,text,status,due_ts,result,notified,detail,result48"


def add(c, source, kind, room, text, ts=None, detail=None):
    """Vloží zásah (status 'caka', vyhodnotenie o 24 h). ts nikdy nie v budúcnosti. Vracia id. detail = voliteľný slovník (JSON)."""
    now = int(time.time())
    ts = now if ts is None else min(int(ts), now)
    kind = kind if kind in KINDS else "ine"
    room = room if room in ROOMS else None
    source = source if source in SOURCES else "user_manual"
    cur = c.execute("INSERT INTO interventions(ts,source,kind,room,text,status,due_ts,notified,detail) VALUES(?,?,?,?,?,?,?,0,?)",
                    (ts, source, kind, room, str(text or "").strip()[:500], "caka", ts + EVAL_AFTER_S,
                     json.dumps(detail, ensure_ascii=False) if detail else None))
    c.commit()
    return cur.lastrowid


def cancel(c, iid):
    """Zruší čakajúci zásah. Vracia True ak sa zmenil."""
    cur = c.execute("UPDATE interventions SET status='zrusene' WHERE id=? AND status='caka'", (int(iid),))
    c.commit()
    return cur.rowcount > 0


def get(c, iid):
    r = c.execute("SELECT %s FROM interventions WHERE id=?" % COLS, (int(iid),)).fetchone()
    return row_dict(r) if r else None


def list_items(c, limit=20, a=None, b=None, kind=None):
    """Najnovšie prvé; a/b = filter podľa ts zásahu [a, b); kind = len daný druh."""
    w, args = [], []
    if a is not None:
        w.append("ts>=? AND ts<?")
        args += [a, b]
    if kind:
        w.append("kind=?")
        args.append(kind)
    rows = c.execute("SELECT %s FROM interventions %s ORDER BY ts DESC, id DESC LIMIT ?" % (COLS, ("WHERE " + " AND ".join(w)) if w else ""),
                     (*args, limit)).fetchall()
    return [row_dict(r) for r in rows]


def has_pending_ventil(c):
    return c.execute("SELECT count(*) FROM interventions WHERE status='caka' AND kind='ventil'").fetchone()[0] > 0


def done_count(c):
    return c.execute("SELECT count(*) FROM interventions WHERE status='hotove'").fetchone()[0]


def max_id(c):
    return c.execute("SELECT coalesce(max(id),0) FROM interventions").fetchone()[0]


# ---------------------------------------------------------------- vyhodnotenie

def okna_min(cfg, c, a, b):
    """Minúty otvorenia okien (celý dom, zapnuté senzory) v [a, b); None = bez dát zo senzorov."""
    own = c is None
    try:
        if own:
            c = adb(cfg)
        m = contacts.window_minutes(c, a, b)
        return None if m is None else round(m)
    except Exception:
        return None
    finally:
        if own and c is not None:
            c.close()


def hourly_spread(ok):
    """Priemer hodinového rozptylu izieb (max − min cez izby s dátami; hodiny s < 2 izbami sa vynechajú); None bez dát."""
    spr = [max(v) - min(v) for v in ([h[r] for r in ROOMS if h.get(r) is not None] for h in ok) if len(v) >= 2]
    return round(sum(spr) / len(spr), 2) if spr else None


def window_stats(cfg, a, b, c=None, min_hours=MIN_HOURS):
    """Súhrn okna [a, b) z hodinových štatistík. Vracia (štruktúra podľa kontraktu, počet hodín s dátami)."""
    hours = hour_stats(cfg, a, b)
    ok = [h for h in hours if h.get("n")]

    def avg(k):
        xs = [h[k] for h in ok if h.get(k) is not None]
        return round(sum(xs) / len(xs), 2) if xs else None

    rooms = {r: avg(r) for r in ROOMS}
    diff = {r: (None if rooms[r] is None or rooms[REF_ROOM] is None else round(rooms[r] - rooms[REF_ROOM], 2)) for r in ROOMS[1:]}
    vonku = avg("vonku")
    kwh = round(sum(h.get("kwh") or 0 for h in ok), 2)
    kns = round(kwh / (23 - vonku), 3) if len(ok) >= min_hours and vonku is not None and 23 - vonku > 3 and kwh > 0 else None
    out = {"a": a, "b": b, "vonku": vonku, "rooms": rooms, "diff": diff, "kwh": kwh, "kwh_na_stupen": kns,
           "dt_beh": avg("dt_beh"), "spread": hourly_spread(ok), "starty": sum(h.get("starty") or 0 for h in ok), "beh": avg("beh"),
           "okna_min": okna_min(cfg, c, a, b)}
    if out["beh"] is not None:
        out["beh"] = round(out["beh"])
    return out, len(ok)


def _d(x, y, nd=2):
    return None if x is None or y is None else round(y - x, nd)


def make_delta(before, after):
    k0, k1 = before["kwh_na_stupen"], after["kwh_na_stupen"]
    return {"vonku": _d(before["vonku"], after["vonku"]),
            "rooms": {r: _d(before["rooms"].get(r), after["rooms"].get(r)) for r in ROOMS},
            "diff": {r: _d(before["diff"].get(r), after["diff"].get(r)) for r in ROOMS[1:]},
            "kwh_na_stupen": _d(k0, k1, 3),
            "kwh_na_stupen_pct": round((k1 - k0) / k0 * 100, 1) if k0 and k1 is not None else None,
            "dt_beh": _d(before["dt_beh"], after["dt_beh"], 1),
            "spread": _d(before.get("spread"), after.get("spread")),
            "okna_min": _d(before.get("okna_min"), after.get("okna_min"), 0)}


def direction(text):
    """+1 / -1 / 0: kam sa podľa textu zásahu očakáva pohyb teploty (viac / menej tepla)."""
    t = (text or "").lower()
    down = ("priškrt", "priskrt", "zavr", "ubral", "zníž", "zniz", "menej", "znížil", "prikr",
            "throttl", "closed", "reduc", "lowered", "turned down", "decreas", "less", "shut")      # + angličtina (UI_LANG=en)
    up = ("pootvor", "otvor", "zvýš", "zvys", "pridal", "viac", "zapol",
          "open", "increas", "raised", "turned up", "added", "more", "switched on", "turned on")
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:°c|c)?\s*(?:->|→|na)\s*(\d+(?:[.,]\d+)?)", t) or \
        re.search(r"(\d+(?:[.,]\d+)?)\s*(?:°c|c)?\s+to\s+(\d+(?:[.,]\d+)?)", t)
    if m:
        a, b = float(m.group(1).replace(",", ".")), float(m.group(2).replace(",", "."))
        if b != a:
            return 1 if b > a else -1
    for w in down:
        if w in t:
            return -1
    for w in up:
        if w in t:
            return 1
    return 0


def verdict(iv, before, after, delta, lang="sk"):
    """Deterministický slovenský verdikt, 2-3 vety."""
    parts = []
    n = lambda x, nd=1, sign=False: i18n.dec(sk(x, nd, sign), lang)
    dd = {r: v for r, v in delta["diff"].items() if v is not None}
    if dd:
        r, v = max(dd.items(), key=lambda kv: abs(kv[1]))
        if abs(v) >= 0.2:
            parts.append(tr("srv.interventions.v_najviac", lang, ref=REF_NAME, room=ROOM_NAMES.get(r, r).lower(), a=n(before["diff"].get(r), 1, True), b=n(after["diff"].get(r), 1, True), v=n(v, 1, True)))
        else:
            parts.append(tr("srv.interventions.v_ziadna", lang, ref=REF_NAME))
    else:
        parts.append(tr("srv.interventions.v_nedali", lang, ref=REF_NAME))
    room, dr = iv.get("room"), direction(iv.get("text"))
    if room in ROOMS:
        v = delta["rooms"].get(REF_ROOM) if room == REF_ROOM else delta["diff"].get(room)
        what = tr("srv.interventions.w_teplota" if room == REF_ROOM else "srv.interventions.w_rozdiel", lang)
        if v is None:
            s = tr("srv.interventions.s_nepodarilo", lang, room=ROOM_NAMES[room].lower())
        elif dr and abs(v) >= 0.2:
            s = tr("srv.interventions.s_smer", lang, room=ROOM_NAMES[room].lower(), what=what, v=n(v, 1, True), smer=tr("srv.interventions.zodpoveda" if v * dr > 0 else "srv.interventions.nezodpoveda", lang))
        elif dr:
            s = tr("srv.interventions.s_nepohla", lang, room=ROOM_NAMES[room].lower(), what=what, v=n(v, 1, True), smer=tr("srv.interventions.hore" if dr > 0 else "srv.interventions.dole", lang))
        else:
            s = tr("srv.interventions.s_zmenil", lang, room=ROOM_NAMES[room].lower(), what=what, v=n(v, 1, True))
        parts.append(s)
    pct = delta.get("kwh_na_stupen_pct")
    if pct is not None:
        parts.append(tr("srv.interventions.spotreba_pct", lang, pct=n(pct, 0, True), a=n(before["kwh_na_stupen"], 2), b=n(after["kwh_na_stupen"], 2)))
    else:
        parts.append(tr("srv.interventions.spotreba_nie", lang))
    warn = []
    if delta.get("vonku") is not None and abs(delta["vonku"]) > 3:
        warn.append(tr("srv.interventions.warn_vonku", lang, v=n(abs(delta["vonku"]), 1)))
    if after.get("dt_beh") is not None and after["dt_beh"] > 6.5:
        warn.append(tr("srv.interventions.warn_dt", lang, v=n(after["dt_beh"], 1)))
    d_ok = delta.get("okna_min")
    if d_ok is not None and abs(d_ok) > 60:
        parts.append(tr("srv.interventions.vetralo", lang))
    text = " ".join(parts)
    if warn:
        text += " " + tr("srv.interventions.upozornenie", lang, w="; ".join(warn))
    return text


def valve_eval(iv, before, after, delta):
    """Vyhodnotenie ventilu: rozptyl izieb pred/po, zmena teploty dotknutej izby voči ostatným, počasie, verdikt (lepsie/horsie/bez_zmeny)."""
    def vs_others(w):
        rooms, r = w["rooms"], iv.get("room")
        oth = [v for k, v in rooms.items() if k != r and v is not None]
        return None if r not in ROOMS or rooms.get(r) is None or not oth else round(rooms[r] - sum(oth) / len(oth), 2)
    rb, ra = vs_others(before), vs_others(after)
    sd = delta.get("spread")
    vd = delta.get("vonku")
    return {"spread_before": before.get("spread"), "spread_after": after.get("spread"), "spread_delta": sd,
            "room_vs_others_before": rb, "room_vs_others_after": ra, "room_vs_others_delta": _d(rb, ra),
            "vonku_delta": vd, "weather_differs": vd is not None and abs(vd) > WEATHER_DIFF_C,
            "verdict": None if sd is None else ("lepsie" if sd <= -VALVE_THRESHOLD else "horsie" if sd >= VALVE_THRESHOLD else "bez_zmeny")}


def valve_text(v, lang="sk", hours=24):
    n = lambda x, nd=2, sign=False: i18n.dec(sk(x, nd, sign), lang)
    vt = v.get("verdict")
    if vt is None:
        return ""
    t = tr("srv.interventions.valve_text", lang, h=hours, verdict=tr("srv.interventions.valve_" + vt, lang),
           a=n(v["spread_before"]), b=n(v["spread_after"]), d=n(v["spread_delta"], 2, True), r=n(v.get("room_vs_others_delta"), 2, True))
    if v.get("weather_differs"):
        t += " " + tr("srv.interventions.valve_pocasie", lang, v=n(abs(v["vonku_delta"]), 1))
    return t


def evaluate(cfg, iv, c=None, lang=None, hours=24):
    """Vyhodnotenie jedného zásahu v oknách ±`hours` h. Vracia (result_dict, dosť_dát)."""
    lang = lang or i18n.home_lang(cfg)
    ts, win = iv["ts"], hours * 3600
    mh = MIN_HOURS * hours // 24
    before, nb = window_stats(cfg, ts - win, ts, c, mh)
    after, na = window_stats(cfg, ts, ts + win, c, mh)
    delta = make_delta(before, after)
    enough = nb >= mh and na >= mh
    if enough:
        vt = verdict(iv, before, after, delta, lang)
    else:
        vt = tr("srv.interventions.nedostatok", lang, nb=nb, na=na, min=mh)
    res = {"before": before, "after": after, "delta": delta, "verdict_text": vt, "hours": hours}
    if iv.get("kind") == "ventil":
        res["valve"] = valve_eval(iv, before, after, delta)
        if enough:
            res["verdict_text"] = (vt + " " + valve_text(res["valve"], lang, hours)).strip()
    return res, enough


def evaluate_due(cfg, c, now=None):
    """Vyhodnotí čakajúce zásahy po termíne. Vracia zoznam id vyhodnotených zásahov."""
    now = int(now or time.time())
    done = []
    rows = c.execute("SELECT %s FROM interventions WHERE status='caka' AND due_ts<=? ORDER BY due_ts" % COLS, (now,)).fetchall()
    for r in rows:
        iv = row_dict(r)
        try:
            res, enough = evaluate(cfg, iv, c)
        except Exception as e:
            print("vyhodnotenie zásahu %s zlyhalo: %s" % (iv["id"], e), flush=True)
            continue
        if not enough and now - iv["due_ts"] < GIVE_UP_S:
            continue   # dáta ešte môžu dôjsť; počkáme
        cur = c.execute("UPDATE interventions SET status='hotove', result=?, notified=1 WHERE id=? AND status='caka'",
                        (json.dumps(res, ensure_ascii=False), iv["id"]))
        c.commit()
        if cur.rowcount != 1:
            continue   # medzitým ho vyhodnotilo iné vlákno
        done.append(iv["id"])
        if enough:
            try:
                notify.event(cfg, "vyhodnotenie", tr("srv.interventions.notify_t", i18n.home_lang(cfg)),
                             tr("srv.interventions.notify_text", i18n.home_lang(cfg), t=iv["text"][:120], v=res["verdict_text"]))
            except Exception:
                pass
    evaluate_due48(cfg, c, now)
    return done


def evaluate_due48(cfg, c, now=None):
    """Druhé vyhodnotenie ventilov (±48 h) pre už vyhodnotené (24 h) záznamy denníka (so štruktúrovaným detailom). Vracia zoznam id."""
    now = int(now or time.time())
    done = []
    rows = c.execute("SELECT %s FROM interventions WHERE kind='ventil' AND detail IS NOT NULL AND status='hotove' AND result48 IS NULL AND ts+?<=? ORDER BY ts"
                     % COLS, (EVAL2_AFTER_S, now)).fetchall()
    for r in rows:
        iv = row_dict(r)
        try:
            res, enough = evaluate(cfg, iv, c, hours=48)
        except Exception as e:
            print("vyhodnotenie (48 h) zásahu %s zlyhalo: %s" % (iv["id"], e), flush=True)
            continue
        if not enough and now - iv["ts"] - EVAL2_AFTER_S < GIVE_UP_S:
            continue
        c.execute("UPDATE interventions SET result48=? WHERE id=? AND result48 IS NULL", (json.dumps(res, ensure_ascii=False), iv["id"]))
        c.commit()
        done.append(iv["id"])
    return done


# ---------------------------------------------------------------- denník ventilov

def fmt_turns(t):
    """−0,25 → '−¼ ot.'; +0,5 → '+½ ot.'; ostatné desatinne."""
    fr = {0.25: "¼", 0.5: "½", 0.75: "¾"}
    a = abs(t)
    whole, rest = int(a), round(a - int(a), 2)
    if rest in fr or not rest:
        body = (str(whole) if whole else "") + fr.get(rest, "")
    else:
        body = ("%g" % round(a, 2)).replace(".", ",")
    return ("−" if t < 0 else "+") + (body or "0") + " ot."


def add_valve(c, room, turns, label=None, note=None, ts=None):
    """Záznam do denníka vyregulovania: izba (kľúč), zmena v otáčkach (záporná = privierané), štítok ventilu, poznámka.
    Vracia (id, varovanie|None); varovanie = predchádzajúca zmena ventilu ešte nie je vyhodnotená."""
    turns = float(turns)
    label = str(label or "").strip()[:40] or "VK 33"
    note = str(note or "").strip()[:200]
    text = "%s %s: %s %s" % (label, ROOM_NAMES.get(room, room).lower(), "priškrtený" if turns < 0 else "pootvorený", fmt_turns(turns))
    if note:
        text += " – " + note
    now = int(time.time())
    when = now if ts is None else min(int(ts), now)
    warn = pending_valve_warning(c, when)
    iid = add(c, "user_manual", "ventil", room, text, ts=ts, detail={"label": label, "turns": turns, "change": fmt_turns(turns), "note": note})
    return iid, warn


def pending_valve_warning(c, ts, lang="sk"):
    """Text varovania, ak je do 24 h pred `ts` (alebo neskôr) nevyhodnotená zmena ventilu; inak None."""
    n = c.execute("SELECT count(*) FROM interventions WHERE kind='ventil' AND status='caka' AND ts>?", (ts - EVAL_AFTER_S,)).fetchone()[0]
    return tr("srv.interventions.valve_predchadzajuca", lang) if n else None


def valve_line(iv, lang="sk"):
    """Jednoriadkový popis zmeny ventilu s najlepším dostupným výsledkom (48 h, inak 24 h) pre súhrn a vstup modelu."""
    d = iv.get("detail") or {}
    res = iv.get("result48") or iv.get("result")
    h = (res or {}).get("hours", 24) if res else None
    base = "%s %s: %s" % (d.get("label") or "VK 33", ROOM_NAMES.get(iv.get("room"), iv.get("room") or "?"), d.get("change") or "")
    v = (res or {}).get("valve")
    if not v or v.get("verdict") is None:
        return base + " – " + tr("srv.interventions.valve_caka", lang)
    return base + " – " + valve_text(v, lang, h)


# ---------------------------------------------------------------- ručné zmeny nastavení

def manual_setting_events(cfg, c, since):
    """Zmeny sledovaných nastavení (old IS NOT NULL) od `since`, ktoré NEpochádzajú od agenta (±3 min od actions.applied=1)."""
    names = list(SETTINGS.values())
    ev = tcq(cfg, "SELECT ts, entity, old, new FROM events WHERE ts>? AND old IS NOT NULL AND entity IN (%s) ORDER BY ts"
             % ",".join("?" * len(names)), (int(since), *names))
    if not ev:
        return []
    acts = c.execute("SELECT ts, entity FROM actions WHERE applied=1 AND ts>=?", (int(since) - 600,)).fetchall()
    out = []
    for t, e, o, n in ev:
        if any(ae == e and abs(at - t) <= AGENT_MATCH_S for at, ae in acts):
            continue
        out.append((t, e, o, n))
    return out


def scan_settings(cfg, c, now=None):
    """Vloží zásahy pre ručné zmeny nastavení od state.last_settings_scan (zlúčené v okne 30 min). Vracia počet nových zásahov."""
    now = int(now or time.time())
    last = get_state(c, "last_settings_scan")
    if last is None:
        set_state(c, "last_settings_scan", now)   # prvé spustenie: staršiu históriu nespätne neimportujeme
        return 0
    last = int(float(last))
    evs = manual_setting_events(cfg, c, last)
    created, new_last = 0, now
    group = []

    def flush(g):
        kind = "krivka" if any(e in CURVE_ENTITIES for _t, e, _o, _n in g) else "nastavenie"
        text = tr("srv.interventions.zmena_nastaveni", i18n.home_lang(cfg), v="; ".join("%s: %s → %s" % (e, o, n) for _t, e, o, n in g))
        add(c, "settings", kind, None, text, ts=g[0][0])

    i = 0
    while i < len(evs):
        g = [evs[i]]
        j = i + 1
        while j < len(evs) and evs[j][0] - g[0][0] <= 1800:
            g.append(evs[j])
            j += 1
        if now - g[-1][0] < 300 and j >= len(evs):
            new_last = g[0][0] - 1   # skupina ešte môže rásť; spracujeme pri ďalšom behu
            break
        flush(g)
        created += 1
        i = j
    set_state(c, "last_settings_scan", new_last)
    return created


# ---------------------------------------------------------------- vstup pre model

def kind_label(kind, lang="sk"):
    """Druh zásahu (ventil, krivka, ...) pre vstup modelu / zobrazenie; sk = pôvodný interný názov."""
    key = "srv.interventions.kind_" + str(kind)
    v = tr(key, lang)
    return str(kind) if v == key else v


def status_label(status, lang="sk"):
    key = "srv.interventions.status_" + str(status)
    v = tr(key, lang)
    return str(status) if v == key else v


def llm_text(c, now=None, lang="sk"):
    """Sekcia „ZÁSAHY A ICH VÝSLEDKY“: čakajúce zásahy a hotové vyhodnotenia za 14 dní (v jazyku modelu `lang`)."""
    now = int(now or time.time())
    out = []
    for r in c.execute("SELECT %s FROM interventions WHERE status='caka' ORDER BY ts" % COLS).fetchall():
        iv = row_dict(r)
        out.append(tr("srv.prompt.iv_pending", lang, due=time.strftime("%d.%m. %H:%M", time.localtime(iv["due_ts"])),
                      ts=time.strftime("%d.%m. %H:%M", time.localtime(iv["ts"])), kind=kind_label(iv["kind"], lang),
                      room=(" (" + ROOM_NAMES.get(iv["room"], iv["room"]) + ")") if iv["room"] else "", text=iv["text"], src=iv["source"]))
    for r in c.execute("SELECT %s FROM interventions WHERE status='hotove' AND due_ts>=? ORDER BY ts" % COLS, (now - 14 * 86400,)).fetchall():
        iv = row_dict(r)
        vt = (iv["result"] or {}).get("verdict_text", "")
        v48 = ((iv.get("result48") or {}).get("valve")) or {}
        if v48.get("verdict"):
            vt += " | " + valve_text(v48, lang, 48)
        out.append(tr("srv.prompt.iv_done", lang, ts=time.strftime("%d.%m. %H:%M", time.localtime(iv["ts"])), kind=kind_label(iv["kind"], lang),
                      room=(" (" + ROOM_NAMES.get(iv["room"], iv["room"]) + ")") if iv["room"] else "", text=iv["text"], verdict=vt))
    return "\n".join(out) or tr("srv.prompt.ziadne", lang)
