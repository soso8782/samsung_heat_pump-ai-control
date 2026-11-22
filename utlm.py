#!/usr/bin/env python3
"""Nočný útlm s adaptívnym ranným dohrevom a režim „Preč z domu“ (obe funkcie predvolene vypnuté).

Jediný zápis: cieľová izbová teplota cez agent.set_room_target (rozsah, log do actions s dôvodom „utlm“ / „prec“).
Stav je v agent.db, tabuľka state: utlm_mode / prec_mode (off | advise | control), utlm_cfg (JSON), utlm_away (JSON),
utlm_run (JSON: pôvodný cieľ „base“, čo sme zapísali „applied“, kľúč fázy), prec_absent_since, utlm_ramp_key.
tick() volá strážca každú minútu. Nikdy sa nezapisuje počas porovnávacieho testu A/B. Iba štandardná knižnica."""
import datetime, json, math, re, time, urllib.request
import abtest
import i18n
from i18n import tr
from common import ROOM_NAMES, REF_ROOM, S, TARGET_ROOMS, get_state, get_targets, is_dry, set_state, tcq

MODES = ("off", "advise", "control")
DEFAULTS = {"delta": -1.0, "start": "22:00", "wake": "06:30", "wake_we": "07:30", "away_value": 19.0}
MAX_DEPTH = 2.0                 # najväčší nočný útlm (°C)
STEP = 0.5
RATE_FALLBACK = 1.0             # °C/h, keď je zo záznamu menej než MIN_EVENTS udalostí
MIN_EVENTS = 5
RATE_LIMITS = (0.2, 3.0)
LEAD_MIN_H, LEAD_MAX_H = 0.5, 4.0
AWAY_LEAD_MAX_H = 8.0           # po dlhšom pobyte bez kúrenia je dom studenší: dohrev smie trvať dlhšie
ABSENT_AFTER_S = 2 * 3600       # automatické „Preč“ po 2 h neprítomnosti podľa HA
LEARN_DAYS = 120
EVENT_WINDOW_S = 6 * 3600
RETRY_S = 300                   # najviac jeden pokus o zápis za 5 min
ENT_RE = re.compile(r"^[a-z_]+\.[A-Za-z0-9_]+$")
_cache = {}


# ---------- pomocné ----------
def _agent():
    import agent
    return agent


def _jget(c, k, default=None):
    try:
        v = json.loads(get_state(c, k, "") or "null")
        return v if isinstance(v, dict) else default
    except ValueError:
        return default


def _jset(c, k, v):
    if v is None:
        c.execute("DELETE FROM state WHERE k=?", (k,))
        c.commit()
    else:
        set_state(c, k, json.dumps(v, ensure_ascii=False))


def _half(x):
    return round(float(x) * 2) / 2


def _hm(s):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(s or "").strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return int(m.group(1)), int(m.group(2))


def _at(d, hm):
    """Unix čas lokálneho dňa d (datetime.date) a (h, m)."""
    return int(time.mktime((d.year, d.month, d.day, hm[0], hm[1], 0, 0, 0, -1)))


def _today(now):
    lt = time.localtime(now)
    return datetime.date(lt.tm_year, lt.tm_mon, lt.tm_mday)


def mode(c, key="utlm_mode"):
    v = get_state(c, key, "off")
    return v if v in MODES else "off"


def load_cfg(c):
    out = dict(DEFAULTS)
    st = _jget(c, "utlm_cfg", {}) or {}
    try:
        d = float(st.get("delta", out["delta"]))
        out["delta"] = max(-MAX_DEPTH, min(0.0, _half(d)))
    except (TypeError, ValueError):
        pass
    for k in ("start", "wake", "wake_we"):
        if _hm(st.get(k)):
            out[k] = "%02d:%02d" % _hm(st[k])
    try:
        out["away_value"] = max(15.0, min(30.0, _half(st.get("away_value", out["away_value"]))))
    except (TypeError, ValueError):
        pass
    return out


def validate_cfg(body, cur, lang="sk"):
    """Overí polia nastavení; vracia (chyba|None, nová konfigurácia)."""
    new = dict(cur)
    if body.get("delta") is not None:
        try:
            d = float(str(body["delta"]).replace(",", "."))
        except ValueError:
            return tr("srv.utlm.zly_utlm", lang), None
        if d != d or d > 1e-9 or d < -MAX_DEPTH - 1e-9 or abs(d * 2 - round(d * 2)) > 1e-9:
            return tr("srv.utlm.zly_utlm", lang), None
        new["delta"] = _half(d)
    for k in ("start", "wake", "wake_we"):
        if body.get(k) is not None:
            hm = _hm(body[k])
            if not hm:
                return tr("srv.utlm.zly_cas", lang), None
            new[k] = "%02d:%02d" % hm
    if body.get("away_value") is not None:
        try:
            v = float(str(body["away_value"]).replace(",", "."))
        except ValueError:
            return tr("srv.utlm.zla_prec", lang), None
        if v != v or not 15 <= v <= 30 or abs(v * 2 - round(v * 2)) > 1e-9:
            return tr("srv.utlm.zla_prec", lang), None
        new["away_value"] = _half(v)
    if not (_hm(new["start"]) > _hm(new["wake"]) and _hm(new["start"]) > _hm(new["wake_we"])):
        return tr("srv.utlm.zly_poradie", lang), None
    return None, new


def save_cfg_state(c, new):
    _jset(c, "utlm_cfg", new)


def wake_ts(cfg_u, d):
    return _at(d, _hm(cfg_u["wake_we"] if d.weekday() >= 5 else cfg_u["wake"]))


# ---------- učenie rýchlosti ohrevu ----------
def outdoor_now(cfg, now):
    k = S.get("vonku")
    if not k:
        return None
    try:
        r = tcq(cfg, "SELECT avg(value) FROM samples WHERE entity=? AND ts>? AND ts<=? AND value IS NOT NULL", (k, now - 3600, now))
        return None if not r or r[0][0] is None else float(r[0][0])
    except Exception:
        return None


def room_temps(cfg, now, max_age=1800):
    """{kľúč izby: posledná teplota} zo zberača (len čerstvé hodnoty)."""
    out = {}
    for r in TARGET_ROOMS:
        k = S.get(r)
        if not k:
            continue
        try:
            rows = tcq(cfg, "SELECT value FROM samples WHERE entity=? AND ts>? AND ts<=? AND value IS NOT NULL ORDER BY ts DESC LIMIT 1",
                       (k, now - max_age, now))
        except Exception:
            rows = []
        if rows:
            out[r] = float(rows[0][0])
    return out


def rate_events(cfg, now, days=LEARN_DAYS):
    """Minulé zvýšenia cieľa a nameraný nárast teploty riadiacej izby: [(ts, vonku °C|None, °C/h)]."""
    ent = _agent().ROOM_TARGET_ENT
    room, out_k = S.get(REF_ROOM), S.get("vonku")
    if not ent or not room:
        return []
    try:
        trows = tcq(cfg, "SELECT ts, value FROM samples WHERE entity=? AND ts>=? AND ts<=? AND value IS NOT NULL ORDER BY ts",
                    (ent, now - days * 86400, now))
    except Exception:
        return []
    evs, prev, last = [], None, 0
    for ts, v in trows:
        if prev is not None and v - prev >= STEP - 1e-9 and ts - last > 3 * 3600:
            evs.append((ts, float(v)))
            last = ts
        prev = v
    res = []
    for t0, tgt in evs:
        try:
            rr = tcq(cfg, "SELECT ts, value FROM samples WHERE entity=? AND ts>=? AND ts<=? AND value IS NOT NULL ORDER BY ts",
                     (room, t0 - 600, t0 + EVENT_WINDOW_S))
        except Exception:
            continue
        if len(rr) < 3:
            continue
        start = [r for r in rr if r[0] <= t0 + 300] or rr[:1]
        ts0, T0 = start[-1]
        if T0 >= tgt - 0.1:
            continue
        after = [r for r in rr if r[0] > ts0]
        hit = next((r for r in after if r[1] >= tgt - 0.1), None)
        if hit is None:      # cieľ sa nedosiahol: berieme nárast do 3 h (pomalší ohrev sa tak neignoruje)
            w = [r for r in after if r[0] <= ts0 + 3 * 3600]
            if not w:
                continue
            hit = max(w, key=lambda r: r[1])
        dt_h = (hit[0] - ts0) / 3600.0
        rise = hit[1] - T0
        if dt_h < 0.25 or rise < 0.3:
            continue
        rate = rise / dt_h
        if not 0.05 <= rate <= 4.0:
            continue
        o = None
        if out_k:
            try:
                ro = tcq(cfg, "SELECT avg(value) FROM samples WHERE entity=? AND ts>=? AND ts<=? AND value IS NOT NULL", (out_k, t0 - 1800, t0 + 7200))
                o = None if not ro or ro[0][0] is None else float(ro[0][0])
            except Exception:
                o = None
        res.append((t0, o, rate))
    return res


def _median(v):
    s = sorted(v)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def fit_rate(events, outdoor):
    """Rýchlosť ohrevu (°C/h) pri vonkajšej teplote `outdoor`: lineárny fit, inak medián, pri < MIN_EVENTS fallback 1,0.
    Vracia {"rate","n","fallback","outdoor","method"}."""
    n = len(events)
    if n < MIN_EVENTS:
        return {"rate": RATE_FALLBACK, "n": n, "fallback": True, "outdoor": outdoor, "method": "fallback"}
    rates = [e[2] for e in events]
    pts = [(e[1], e[2]) for e in events if e[1] is not None]
    rate, method = _median(rates), "median"
    if outdoor is not None and len(pts) >= MIN_EVENTS:
        mx = sum(p[0] for p in pts) / len(pts)
        my = sum(p[1] for p in pts) / len(pts)
        sxx = sum((p[0] - mx) ** 2 for p in pts)
        if sxx > 1e-6:
            b = sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx
            pred = my + b * (outdoor - mx)
            if pred == pred and pred > 0.1:
                rate, method = pred, "linear"
    lo, hi = RATE_LIMITS
    return {"rate": round(max(lo, min(hi, rate)), 2), "n": n, "fallback": False, "outdoor": outdoor, "method": method}


def learn_rate(cfg, now, outdoor="auto"):
    if outdoor == "auto":
        outdoor = outdoor_now(cfg, now)
    key = (cfg.get("TC_DB"), int(now) // 1800, None if outdoor is None else round(outdoor))
    if key not in _cache:
        if len(_cache) > 50:
            _cache.clear()
        _cache[key] = fit_rate(rate_events(cfg, now), outdoor)
    return _cache[key]


def lead_hours(rise, rate, cap=LEAD_MAX_H):
    """(lead v hodinách, bol dosiahnutý strop?) – potrebný nárast / rýchlosť, v rozsahu 0,5 – cap h."""
    raw = max(0.0, rise) / max(rate, 0.05)
    return max(LEAD_MIN_H, min(cap, raw)), raw > cap + 1e-9


def eff_depth(depth, rate, cap=LEAD_MAX_H):
    """Hĺbka útlmu, ktorú dom stihne dohriať do `cap` hodín; (hĺbka, zredukované?)."""
    if depth <= 0:
        return 0.0, False
    if depth / max(rate, 0.05) <= cap + 1e-9:
        return depth, False
    return max(0.0, math.floor(rate * cap / STEP + 1e-9) * STEP), True


# ---------- plán ----------
def load_away(c):
    a = _jget(c, "utlm_away")
    return a if a and a.get("since") else None


def load_run(c):
    return _jget(c, "utlm_run")


def plan(cfg, c, now, base=None):
    """Čistý výpočet plánu bez zápisu. Kľúče: state (normal|night|ramp|away), feature (night|away|None), key, target (žiadaný cieľ
    alebo None = pôvodný), depth, reduced, next_change, next_state, ramp_start, lead_h, rate, wake, note."""
    cu = load_cfg(c)
    nm, am = mode(c), mode(c, "prec_mode")
    run = load_run(c)
    if base is None:
        base = run.get("base") if run else None
    out = {"state": "normal", "feature": None, "key": None, "target": None, "depth": 0.0, "reduced": False, "next_change": None,
           "next_state": None, "ramp_start": None, "lead_h": None, "rate": None, "wake": None, "note": None}
    away = load_away(c) if am != "off" else None
    rate = learn_rate(cfg, now)
    out["rate"] = rate
    ramp_key = get_state(c, "utlm_ramp_key", "")
    if away:
        tgt = max(_agent().ROOM_TARGET_MIN, _half(cu["away_value"]))
        out.update(feature="away", key="a:%s" % away["since"], target=tgt)
        until = away.get("until")
        if until:
            rise = (base - tgt) if base is not None else 1.0
            lead, _ = lead_hours(rise, rate["rate"], AWAY_LEAD_MAX_H)
            out.update(ramp_start=int(until - lead * 3600), lead_h=round(lead, 2), wake=until)
            rk = "r:a:%s" % away["since"]
            if now >= out["ramp_start"] or ramp_key == rk:
                if now >= until:
                    return dict(out, state="normal", feature=None, key=None, target=None, next_change=None)
                return dict(out, state="ramp", target=None, key=rk, next_change=until, next_state="normal")
            out.update(state="away", next_change=out["ramp_start"], next_state="ramp")
        else:
            out["state"] = "away"
        return out
    if nm == "off" or cu["delta"] >= 0:
        return out
    d0 = _today(now)
    w0 = wake_ts(cu, d0)
    s0 = _at(d0, _hm(cu["start"]))
    if now < w0:
        wake, wd = w0, d0
    elif now >= s0:
        wd = d0 + datetime.timedelta(days=1)
        wake = wake_ts(cu, wd)
    else:
        out.update(next_change=s0, next_state="night")
        return out
    key = "n:%s" % wd.isoformat()
    depth = -cu["delta"]
    if run and run.get("key") == key and run.get("applied") is not None and run.get("base") is not None:
        applied_depth = run["base"] - run["applied"]
        if applied_depth > 0:
            depth = applied_depth
    eff, reduced = eff_depth(depth, rate["rate"])
    lead, _ = lead_hours(eff, rate["rate"])
    out.update(feature="night", key=key, depth=eff, reduced=reduced, wake=wake, lead_h=round(lead, 2),
               ramp_start=int(wake - lead * 3600))
    if reduced:
        out["note"] = "reduced0" if eff <= 0 else "reduced"
    if eff <= 0:
        out.update(state="normal", feature="night", next_change=None)
        return out
    if now >= out["ramp_start"] or ramp_key == "r:" + key:
        out.update(state="ramp", key="r:" + key, next_change=wake, next_state="normal")
        out["target"] = None
    else:
        out.update(state="night", target=None, next_change=out["ramp_start"], next_state="ramp")
    return out


# ---------- zápis ----------
def _log(c, cfg, text):
    c.execute("INSERT INTO runs(ts,mode,status,stav,summary,actions) VALUES(?,?,?,?,?,?)",
              (int(time.time()), get_state(c, "mode", "advise"), "info", None, text, "[]"))
    c.commit()


def _write(cfg, c, value, reason, lang):
    """Zápis cieľa cez set_room_target. Vracia None pri úspechu, inak text chyby."""
    try:
        _agent().set_room_target(cfg, c, value, reason, lang, reason=reason)
        return None
    except (ValueError, RuntimeError) as e:
        return str(e)


def rooms_guard(cfg, c, now, depth):
    """Žiadna izba nesmie klesnúť pod svoje profilové minimum. Vracia (ok, kľúč izby alebo None)."""
    mins = get_targets(c)
    for r, t in room_temps(cfg, now).items():
        mn = (mins.get(r) or {}).get("min")
        if mn is not None and t - depth < mn - 1e-9:
            return False, r
    return True, None


def tick(cfg, c, now=None):
    """Jeden krok (strážca, každú minútu). Vracia krátky popis pre log a testy."""
    now = int(now or time.time())
    lang = i18n.home_lang(cfg)
    try:
        presence_tick(cfg, c, now)
    except Exception:
        pass
    away = load_away(c)
    if away and away.get("until") and now >= away["until"] + 60:
        _jset(c, "utlm_away", None)
    nm, am = mode(c), mode(c, "prec_mode")
    run = load_run(c)
    if nm == "off" and am == "off" and not run:
        return "vypnuté"
    if abtest.active(c):
        return "abtest"
    if get_state(c, "mode", "advise") == "off":
        return "agent vypnutý"
    ag = _agent()
    cur = ag.room_target_current(cfg)
    if cur is None:
        return "bez cieľa"
    if now - float(get_state(c, "utlm_last_try", "0") or 0) < RETRY_S and get_state(c, "utlm_failed") == "1":
        return "čaká na opakovanie"
    p = plan(cfg, c, now, base=(run or {}).get("base") if run else cur)
    if p["state"] == "ramp":
        set_state(c, "utlm_ramp_key", p["key"])
    feat = p["feature"]
    ctrl = (feat == "night" and nm == "control") or (feat == "away" and am == "control")
    if p["state"] == "normal" and p.get("note") == "reduced0" and nm == "control" and not run:
        _skip(c, cfg, p["key"], tr("srv.utlm.log_bez_utlmu", lang))
    if p["state"] in ("night", "away") and ctrl:
        return _hold(cfg, c, now, lang, p, run, cur)
    return _restore(cfg, c, now, lang, p, run, cur)


def _fail(c, now, err):
    set_state(c, "utlm_last_try", str(now))
    set_state(c, "utlm_failed", "1")
    return "chyba: " + str(err)[:100]


def _restore(cfg, c, now, lang, p, run, cur):
    if not run:
        return "nič"
    base, applied = run.get("base"), run.get("applied")
    reason = "prec" if run.get("feature") == "away" else "utlm"
    if run.get("ovr") or applied is None or base is None or abs(cur - applied) > 1e-6:
        _jset(c, "utlm_run", None)      # ručná zmena alebo nič zapísané: nič nevraciame
        return "bez návratu"
    if abs(cur - base) > 1e-6:
        err = _write(cfg, c, base, reason, lang)
        if err:
            return _fail(c, now, err)
        _log(c, cfg, tr("srv.utlm.log_navrat", i18n.home_lang(cfg), v=i18n.dec("%g" % base, i18n.home_lang(cfg))))
    set_state(c, "utlm_failed", "0")
    if p["state"] == "ramp":
        run.update(applied=base, key=p["key"])
        _jset(c, "utlm_run", run)
        return "dohrev"
    _jset(c, "utlm_run", None)
    return "návrat"


def _hold(cfg, c, now, lang, p, run, cur):
    key, feat = p["key"], p["feature"]
    reason = "utlm" if feat == "night" else "prec"
    hl = i18n.home_lang(cfg)
    dec = lambda x: i18n.dec("%g" % x, hl)
    if run and run.get("key") == key:
        if run.get("ovr"):
            return "ručná zmena"
        if abs(cur - run["applied"]) > 1e-6:
            run["ovr"] = True
            _jset(c, "utlm_run", run)
            _log(c, cfg, tr("srv.utlm.log_rucne", hl))
            return "ručná zmena"
        if feat == "night":
            ok, room = rooms_guard(cfg, c, now, 0.0)
            if not ok:
                err = _write(cfg, c, run["base"], reason, lang)
                if err:
                    return _fail(c, now, err)
                run.update(applied=run["base"], ovr=True)
                _jset(c, "utlm_run", run)
                _log(c, cfg, tr("srv.utlm.log_izba_pod", hl, room=ROOM_NAMES.get(room, room)))
                return "izba pod minimom"
        return "drží"
    # nová fáza: pôvodný cieľ ostáva, ak ho už držíme zápisom z predošlej fázy
    base = run["base"] if run and run.get("applied") is not None and abs(cur - run["applied"]) < 1e-6 and run.get("base") is not None else cur
    if feat == "night":
        tgt = max(_agent().ROOM_TARGET_MIN, _half(base - p["depth"]))
    else:
        tgt = p["target"]
    depth = base - tgt
    if tgt >= base - 1e-9 or p["depth"] <= 0 and feat == "night":
        return _skip(c, cfg, key, tr("srv.utlm.log_bez_utlmu", hl) if p.get("note") == "reduced0" else tr("srv.utlm.log_uz_nizko", hl))
    if feat == "night":
        ok, room = rooms_guard(cfg, c, now, depth)
        if not ok:
            return _skip(c, cfg, key, tr("srv.utlm.log_izba_skip", hl, room=ROOM_NAMES.get(room, room)))
    err = _write(cfg, c, tgt, reason, lang)
    if err:
        return _fail(c, now, err)
    set_state(c, "utlm_failed", "0")
    _jset(c, "utlm_run", {"base": base, "applied": tgt, "key": key, "feature": feat, "ts": now})
    note = tr("srv.utlm.log_znizene", hl, v=dec(p["depth"])) if p.get("reduced") else ""
    _log(c, cfg, tr("srv.utlm.log_noc" if feat == "night" else "srv.utlm.log_prec", hl, a=dec(base), b=dec(tgt), note=note).strip())
    return "útlm" if feat == "night" else "preč"


def _skip(c, cfg, key, text):
    if get_state(c, "utlm_skip_key", "") != key:
        set_state(c, "utlm_skip_key", key)
        _log(c, cfg, text)
    return "preskočené"


# ---------- Preč z domu ----------
def parse_until(v, now):
    """'YYYY-MM-DD HH:MM' / 'YYYY-MM-DDTHH:MM' (lokálny čas) -> unix; None = bez návratu; ValueError pri zlom vstupe."""
    if v in (None, ""):
        return None
    s = str(v).strip().replace("T", " ")
    try:
        t = int(time.mktime(time.strptime(s[:16], "%Y-%m-%d %H:%M")))
    except ValueError:
        raise ValueError("format")
    if t < now + 600 or t > now + 60 * 86400:
        raise ValueError("rozsah")
    return t


def away_start(cfg, c, until, now, src="manual", lang="sk"):
    """Zaregistruje „Preč“. Vracia (ok, text)."""
    if mode(c, "prec_mode") == "off":
        return False, tr("srv.utlm.prec_vypnute", lang)
    if abtest.active(c):
        return False, tr("srv.check.abtest", lang)
    _jset(c, "utlm_away", {"src": src, "since": int(now), "until": until})
    return True, ""


def away_cancel(c):
    _jset(c, "utlm_away", None)


def ha_state(cfg, entity, timeout=8):
    """Stav entity z Home Assistanta (GET /api/states/<id>) alebo None pri chybe."""
    import contacts
    url, token = contacts.ha_conf(cfg)
    if not url or not token or not ENT_RE.match(entity or ""):
        return None
    req = urllib.request.Request("%s/api/states/%s" % (url, entity), headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return str(json.loads(r.read(200000).decode("utf-8")).get("state"))
    except Exception:
        return None


def presence_of(s):
    """'home' / 'away' / None (neznáme)."""
    s = (s or "").strip().lower()
    if s in ("", "unknown", "unavailable", "none"):
        return None
    if s in ("home", "on", "true") or (s.isdigit() and int(s) > 0):
        return "home"
    return "away"


def presence_entities(cfg):
    return [e.strip() for e in str(cfg.get("HA_PRITOMNOST") or "").split(",") if ENT_RE.match(e.strip())][:5]


def presence_tick(cfg, c, now):
    """Všetci preč > 2 h -> automatické „Preč“; návrat -> normál. Bez HA_PRITOMNOST/HA_TOKEN nič nerobí."""
    ents = presence_entities(cfg)
    if not ents or not (cfg.get("HA_TOKEN") or "").strip() or mode(c, "prec_mode") == "off":
        return None
    st = [presence_of(ha_state(cfg, e)) for e in ents]
    if "home" in st:
        set_state(c, "prec_absent_since", "")
        a = load_away(c)
        if a and a.get("src") == "auto":
            _jset(c, "utlm_away", None)
            return "návrat"
        return None
    if None in st:
        return None
    since = float(get_state(c, "prec_absent_since", "") or 0)
    if not since:
        set_state(c, "prec_absent_since", str(now))
        return None
    if now - since >= ABSENT_AFTER_S and not load_away(c) and not abtest.active(c):
        _jset(c, "utlm_away", {"src": "auto", "since": int(now), "until": None})
        return "preč"
    return None


# ---------- stav pre web ----------
def status_block(cfg, c, now=None, admin=True):
    now = int(now or time.time())
    cu = load_cfg(c)
    run = load_run(c)
    try:
        cur = _agent().room_target_current(cfg)
    except Exception:
        cur = None
    try:
        p = plan(cfg, c, now, base=(run or {}).get("base") if run else cur)
    except Exception:
        p = {"state": "normal", "feature": None, "rate": None}
    nm, am = mode(c), mode(c, "prec_mode")
    away = load_away(c)
    out = {"mode": nm, "prec_mode": am, "state": p["state"],
           "feature": p.get("feature"), "next_change": p.get("next_change"), "next_state": p.get("next_state"),
           "ramp_start": p.get("ramp_start"), "wake": p.get("wake"), "lead_h": p.get("lead_h"),
           "depth": p.get("depth"), "reduced": bool(p.get("reduced")), "note": p.get("note"),
           "rate": p.get("rate"), "away": away, "applied": bool(run and not run.get("ovr")),
           "dry": is_dry(cfg), "ab": abtest.active(c)}
    if admin:
        out["cfg"] = cu
        out["ha_pritomnost"] = str(cfg.get("HA_PRITOMNOST") or "")
        out["absent_since"] = float(get_state(c, "prec_absent_since", "") or 0) or None
        out["limits"] = {"max_depth": MAX_DEPTH, "away_min": _agent().ROOM_TARGET_MIN}
    return out
