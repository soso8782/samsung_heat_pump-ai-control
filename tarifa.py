#!/usr/bin/env python3
"""Kúrenie podľa tarify (SPEC.md, "Kúrenie podľa tarify"). Predvolene VYPNUTÉ (config TARIFA_KURENIE=0). Deterministické, bez modelu.

Ak má tarifa drahé hodiny (VT; TARIFA_VT_INTERVALY, napr. "06:00-08:00,17:00-19:00"; prázdne = tarifa neznáma = nič sa nerobí),
tak TARIFA_PREDSTIH_H hodín pred blokom VT mierne zvýši cieľovú izbovú teplotu (+TARIFA_PREDKURENIE_C, najviac +1,0 °C) a počas VT
ju mierne zníži (-TARIFA_UTLM_C, najviac -1,0 °C); po VT vráti pôvodnú hodnotu. Predhrev sa preskočí, keď je podľa predpovede vonku
teplo (priemer okna >= TARIFA_BEZ_KURENIA_C, predvolene 12 °C) alebo je leto. Útlm sa nikdy nerobí (a prebiehajúci sa ruší),
ak je niektorá izba pod svojím minimom + 0,3 °C alebo nemáme žiadnu čerstvú teplotu izieb.
Režim agenta "advise" len uloží odporúčanie (state.tarifa_advice, /api/status, /api/tarifa); "control" zapisuje cieľ izby cez
agent.esp_set_number (rozsah profilu ROOM_TARGET_MIN..MAX, krok 0,5) a každú zmenu zapíše do actions s dôvodom "tarifa".
Pri A/B teste (abtest.active) sa nič nemení. Vyhodnotenie: kWh čerpadla vo VT a NT po dňoch a odhad úspory voči obdobiu pred zapnutím."""
import datetime as _dt, json, time, traceback, urllib.request

import abtest, contacts
import i18n
import ha_cost
from i18n import tr
from common import S, get_state, get_targets, is_dry, set_state, tcq

STEP = 0.5
MAX_DELTA = 1.0
GUARD_MARGIN = 0.3           # útlm sa zruší, ak je izba pod minimom + toto
ROOM_FRESH_S = 900
RETRY_S = 300                # po neúspešnom zápise ďalší pokus najskôr o 5 min
FC_TTL_S = 3600
MIN_DAYS = 3                 # najmenej dní pred / po na odhad úspory
MIN_HOURS_DAY = 20


def _agent():
    import agent
    return agent


def _f(cfg, key, default, lo, hi):
    return ha_cost._f(cfg, key, default, lo, hi)


def _q(x):
    return round(x / STEP) * STEP


def enabled(cfg):
    return str(cfg.get("TARIFA_KURENIE", "0")).strip() == "1"


def lead_s(cfg):
    return int(_f(cfg, "TARIFA_PREDSTIH_H", 2.0, 0.0, 6.0) * 3600)


def _delta(cfg, key, default):
    """Hodnota z configu orezaná na 0..1,0 °C a zaokrúhlená na krok 0,5 (neplatná = predvolená)."""
    try:
        v = float(str(cfg.get(key) if cfg.get(key) not in (None, "") else default).replace(",", "."))
    except ValueError:
        v = default
    return _q(min(max(v, 0.0), MAX_DELTA))


def pre_c(cfg):
    return _delta(cfg, "TARIFA_PREDKURENIE_C", 0.5)


def cut_c(cfg):
    return _delta(cfg, "TARIFA_UTLM_C", 0.5)


# ---------------------------------------------------------------- tarifa: bloky VT
def parse_intervals(txt):
    """"06:00-08:00,17:00-19:00" -> [(min_od, min_do)] (po minútach dňa); neplatné časti sa preskočia; do <= od = cez polnoc."""
    out = []
    for part in str(txt or "").replace(";", ",").split(","):
        try:
            a, b = part.strip().split("-")
            ah, am = a.strip().split(":")
            bh, bm = b.strip().split(":")
            s, e = int(ah) * 60 + int(am), int(bh) * 60 + int(bm)
        except ValueError:
            continue
        if 0 <= s < 1440 and 0 <= e <= 1440 and s != e:
            out.append((s, e))
    return out


def known(cfg):
    return bool(parse_intervals(cfg.get("TARIFA_VT_INTERVALY")))


def vt_blocks(cfg, now, back_days=1, fwd_days=2):
    """Zoznam [(start_ts, end_ts)] blokov VT okolo `now` (zlúčené, zoradené)."""
    iv = parse_intervals(cfg.get("TARIFA_VT_INTERVALY"))
    if not iv:
        return []
    d0 = _dt.date.fromtimestamp(now)
    raw = []
    for k in range(-back_days, fwd_days + 1):
        d = d0 + _dt.timedelta(days=k)
        for s, e in iv:
            a = int(time.mktime((d.year, d.month, d.day, s // 60, s % 60, 0, 0, 0, -1)))
            dd = d + _dt.timedelta(days=1) if e <= s else d
            b = int(time.mktime((dd.year, dd.month, dd.day, (e % 1440) // 60, e % 60, 0, 0, 0, -1)))
            if e == 1440 and e > s:
                nd = d + _dt.timedelta(days=1)
                b = int(time.mktime((nd.year, nd.month, nd.day, 0, 0, 0, 0, 0, -1)))
            raw.append((a, b))
    raw.sort()
    out = []
    for a, b in raw:
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def is_vt(cfg, ts):
    return any(a <= ts < b for a, b in vt_blocks(cfg, ts, 1, 1))


def overlaps_vt(cfg, a, b):
    """True, ak sa [a, b) prekrýva s nejakým blokom VT."""
    return any(x < b and y > a for x, y in vt_blocks(cfg, a, 1, 2))


def phase_at(cfg, now):
    """("vt"|"pred"|"norm", blok) – blok je (start, end) aktuálneho / najbližšieho VT bloku alebo None."""
    lead = lead_s(cfg)
    blocks = vt_blocks(cfg, now)
    for a, b in blocks:
        if a <= now < b:
            return "vt", (a, b)
    for a, b in blocks:
        if a - lead <= now < a and lead > 0:
            return "pred", (a, b)
    nxt = next(((a, b) for a, b in blocks if a > now), None)
    return "norm", nxt


# ---------------------------------------------------------------- predpoveď a izby
def fetch_forecast(cfg):
    """[(ts, teplota)] hodinová predpoveď vonku (open-meteo); výnimka pri chybe."""
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s&hourly=temperature_2m&forecast_days=2&timezone=auto"
           % (cfg["LAT"], cfg["LON"]))
    with urllib.request.urlopen(url, timeout=15) as r:
        h = json.loads(r.read().decode("utf-8"))["hourly"]
    return [(int(time.mktime(time.strptime(t, "%Y-%m-%dT%H:%M"))), v) for t, v in zip(h["time"], h["temperature_2m"]) if v is not None]


def forecast(cfg, c, now):
    """Predpoveď z cache v state (1 h) alebo nová; None, ak nie je dostupná."""
    try:
        d = json.loads(get_state(c, "tarifa_fc", "") or "null")
        if d and now - d["ts"] < FC_TTL_S:
            return [tuple(x) for x in d["v"]]
    except (ValueError, KeyError, TypeError):
        pass
    if not (cfg.get("LAT") and cfg.get("LON")):
        return None
    try:
        v = fetch_forecast(cfg)
    except Exception as e:
        print("tarifa: predpoveď nedostupná (%s)" % type(e).__name__, flush=True)
        return None
    set_state(c, "tarifa_fc", json.dumps({"ts": int(now), "v": v}))
    return v


def mild(cfg, fc, a, b):
    """True, ak je podľa predpovede v okne [a, b) priemer vonku >= TARIFA_BEZ_KURENIA_C (netreba kúriť). Bez predpovede None."""
    if not fc:
        return None
    vals = [t for ts, t in fc if a - 1800 <= ts < b]
    if not vals:
        return None
    return sum(vals) / len(vals) >= _f(cfg, "TARIFA_BEZ_KURENIA_C", 12.0, -10.0, 30.0)


def room_guard(cfg, c, now):
    """(ok, dôvod): každá izba s čerstvými dátami je >= minimum + 0,3 °C a aspoň jedna izba má dáta."""
    tg = get_targets(c)
    names = {k: S[k] for k in tg if k in S}
    if not names:
        return False, "nemame izby"
    rows = dict(tcq(cfg, "SELECT entity, avg(value) FROM samples WHERE ts>=? AND entity IN (%s) GROUP BY 1"
                    % ",".join("?" * len(names)), (int(now) - ROOM_FRESH_S, *names.values())))
    have = 0
    for k, n in names.items():
        v = rows.get(n)
        if v is None:
            continue
        have += 1
        if v < tg[k]["min"] + GUARD_MARGIN:
            return False, "%s %.1f < %.1f" % (k, v, tg[k]["min"] + GUARD_MARGIN)
    if not have:
        return False, "bez teplôt izieb"
    return True, ""


# ---------------------------------------------------------------- plán
def plan(cfg, c, now):
    """Čistý plán bez zápisu: {phase, delta, why, block, mild}. phase: vt/pred/norm; delta v °C (0 = nič)."""
    ph, blk = phase_at(cfg, now)
    out = {"phase": ph, "delta": 0.0, "why": "", "block": blk, "mild": None}
    if not known(cfg):
        out["why"] = "tarifa"
        return out
    if ph == "norm":
        return out
    if contacts.season(c) == "leto":
        out["why"] = "leto"
        return out
    a, b = blk
    out["mild"] = mild(cfg, forecast(cfg, c, now), (a - lead_s(cfg)) if ph == "pred" else a, b)
    if out["mild"]:
        out["why"] = "teplo"
        return out
    if ph == "pred":
        if out["mild"] is None:
            out["why"] = "predpoved"      # bez predpovede sa nepredhrieva
            return out
        out["delta"] = pre_c(cfg)
    else:
        ok, why = room_guard(cfg, c, now)
        if not ok:
            out["why"] = "izba: " + why
            return out
        out["delta"] = -cut_c(cfg)
    return out


def _hm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def _advice_text(cfg, p, hl):
    if not p["delta"]:
        return ""
    a, b = p["block"]
    key = "srv.tarifa.advice_pred" if p["phase"] == "pred" else "srv.tarifa.advice_vt"
    return tr(key, hl, d=i18n.dec("%+g" % p["delta"], hl), a=_hm(a), b=_hm(b))


# ---------------------------------------------------------------- zápis
def _state_active(c):
    try:
        v = float(get_state(c, "tarifa_base", ""))
        return v, float(get_state(c, "tarifa_value", ""))
    except (TypeError, ValueError):
        return None


def _clear(c):
    set_state(c, "tarifa_base", "")
    set_state(c, "tarifa_value", "")


def _write(cfg, c, old, new, reason):
    """Zápis cieľa izby cez agent.esp_set_number + riadok v actions. Vracia True pri potvrdenom zápise."""
    ag = _agent()
    hl = i18n.home_lang(cfg)
    err = None
    try:
        ag.esp_set_number(cfg, ag.ROOM_TARGET_ENT, "%g" % new)
        if not is_dry(cfg):
            time.sleep(3)
            back = ag.num(ag.esp_get(cfg, "number", ag.ROOM_TARGET_ENT).get("value"))
            if back is None or abs(back - new) > 1e-6:
                err = tr("srv.run.unconfirmed", hl, back=back)
    except Exception as e:
        err = tr("srv.run.write_err", hl, e=e)
    ok = err is None
    if ok and not is_dry(cfg):
        ag._live_value(cfg, ag.ROOM_TARGET_ENT, new)
    c.execute("INSERT INTO actions(ts,entity,old,new,reason,applied,result) VALUES(?,?,?,?,?,?,?)",
              (int(time.time()), ag.ROOM_TARGET_ENT, old, new, reason, 1 if ok else 0,
               (tr("srv.run.dry", hl) if is_dry(cfg) else tr("srv.run.written", hl)) if ok else err))
    c.commit()
    return ok


def _reason(cfg, p, restore_why=None):
    hl = i18n.home_lang(cfg)
    if restore_why:
        return "%s: %s" % (tr("srv.tarifa.reason", hl), restore_why)
    return "%s: %s" % (tr("srv.tarifa.reason", hl), tr("srv.tarifa.r_" + p["phase"], hl, a=_hm(p["block"][0]), b=_hm(p["block"][1])))


def restore(cfg, c, why):
    """Vráti pôvodný cieľ izby, ak ho zmenila tarifa a medzitým ho nikto neprepísal. Vracia text."""
    st = _state_active(c)
    if not st:
        return ""
    base, val = st
    ag = _agent()
    hl = i18n.home_lang(cfg)
    cur = val if is_dry(cfg) else ag.room_target_current(cfg)
    if cur is None:
        return tr("srv.tarifa.t_necita", hl)
    if abs(cur - val) > 1e-6:
        _clear(c)                       # niekto ho zmenil ručne: nechať tak
        return tr("srv.tarifa.t_rucne", hl)
    if now_try_blocked(c):
        return tr("srv.tarifa.t_pocka", hl)
    if _write(cfg, c, cur, base, _reason(cfg, None, why)):
        _clear(c)
        return tr("srv.tarifa.t_vratene", hl, v=i18n.dec("%g" % base, hl))
    set_state(c, "tarifa_try", int(time.time()))
    return tr("srv.tarifa.t_zlyhalo", hl)


def now_try_blocked(c):
    try:
        return time.time() - float(get_state(c, "tarifa_try", "0") or 0) < RETRY_S
    except ValueError:
        return False


def tick(cfg, c, now=None):
    """Jeden krok (z watchdogu každú minútu). Vracia krátky popis pre log/testy."""
    now = now or time.time()
    hl = i18n.home_lang(cfg)
    mode = get_state(c, "mode", "advise")
    active = _state_active(c)
    if not enabled(cfg):
        return restore(cfg, c, tr("srv.tarifa.w_vypnute", hl)) if active else ""
    if mode == "off":
        return restore(cfg, c, tr("srv.tarifa.w_agent_off", hl)) if active else tr("srv.tarifa.t_off", hl)
    if abtest.active(c):
        return restore(cfg, c, tr("srv.tarifa.w_abtest", hl)) if active else tr("srv.tarifa.t_abtest", hl)
    try:    # nočný útlm / ranný nábeh / Preč z domu majú prednosť (menia ten istý cieľ izby; inak by sa navzájom prepisovali)
        import utlm
        if utlm.load_run(c) or utlm.load_away(c):
            return restore(cfg, c, tr("srv.tarifa.w_dovod", hl, why="utlm")) if active else "utlm"
    except Exception:
        pass
    if not get_state(c, "tarifa_od", ""):
        set_state(c, "tarifa_od", time.strftime("%Y-%m-%d", time.localtime(now)))
    p = plan(cfg, c, now)
    advice = _advice_text(cfg, p, hl)
    prev = get_state(c, "tarifa_advice", "")
    try:
        prev_ph = json.loads(prev or "{}").get("phase")
    except ValueError:
        prev_ph = None
    set_state(c, "tarifa_advice", json.dumps({"ts": int(now), "phase": p["phase"], "delta": p["delta"], "text": advice,
                                              "why": p["why"]}, ensure_ascii=False))
    if mode != "control":
        if active:
            return restore(cfg, c, tr("srv.tarifa.w_advise", hl))
        if advice and prev_ph != p["phase"]:
            c.execute("INSERT INTO runs(ts,mode,status,stav,summary,actions) VALUES(?,?,?,?,?,?)",
                      (int(now), mode, "info", None, tr("srv.tarifa.log_advise", hl, text=advice), "[]"))
            c.commit()
        return tr("srv.tarifa.t_advise", hl, text=advice or "-")
    # control
    if p["delta"] == 0:
        if active:
            why = (tr("srv.tarifa.w_koniec", hl) if p["phase"] == "norm" else tr("srv.tarifa.w_dovod", hl, why=p["why"]))
            return restore(cfg, c, why)
        return tr("srv.tarifa.t_nic", hl)
    ag = _agent()
    if active:
        base, val = active
        cur = val if is_dry(cfg) else ag.room_target_current(cfg)
        if cur is not None and abs(cur - val) > 1e-6:
            _clear(c)
            return tr("srv.tarifa.t_rucne", hl)
    else:
        cur = ag.room_target_current(cfg)
        base = cur
    if cur is None:
        return tr("srv.tarifa.t_necita", hl)
    want = _q(base + p["delta"])
    want = min(max(want, ag.ROOM_TARGET_MIN), ag.ROOM_TARGET_MAX)
    if abs(want - base) > MAX_DELTA + 1e-9:
        want = base + (MAX_DELTA if want > base else -MAX_DELTA)
    if abs(want - cur) < 1e-6:
        return tr("srv.tarifa.t_nic", hl)
    if now_try_blocked(c):
        return tr("srv.tarifa.t_pocka", hl)
    if _write(cfg, c, cur, want, _reason(cfg, p)):
        if abs(want - base) < 1e-6:
            _clear(c)
        else:
            set_state(c, "tarifa_base", base)
            set_state(c, "tarifa_value", want)
        return tr("srv.tarifa.t_zapisane", hl, old=i18n.dec("%g" % cur, hl), new=i18n.dec("%g" % want, hl))
    set_state(c, "tarifa_try", int(time.time()))
    return tr("srv.tarifa.t_zlyhalo", hl)


# ---------------------------------------------------------------- stav a vyhodnotenie
def status(cfg, c, now=None):
    """Stav pre /api/status (lacné: bez databázy štatistík)."""
    now = now or time.time()
    act = _state_active(c)
    try:
        adv = json.loads(get_state(c, "tarifa_advice", "") or "null")
    except ValueError:
        adv = None
    ph, blk = phase_at(cfg, now) if known(cfg) else ("norm", None)
    return {"enabled": enabled(cfg), "known": known(cfg), "mode": get_state(c, "mode", "advise"),
            "phase": ph, "next_block": {"start": blk[0], "end": blk[1]} if blk else None,
            "offset": {"base": act[0], "value": act[1]} if act else None,
            "advice": adv if enabled(cfg) else None, "od": get_state(c, "tarifa_od", "") or None,
            "tuv": bool(str(cfg.get("TUV_TARIFA", "0")).strip() == "1")}


def day_split(cfg, d):
    """(kWh VT, kWh NT, hodín s údajmi) čerpadla za lokálny deň d (date) zo stats.db."""
    import spotreba, stats
    da, db = stats._bounds(d)
    st = spotreba._hourly(cfg, da, db)
    vt = nt = 0.0
    n = 0
    for h in range(da, db, 3600):
        r = st.get(h)
        if not r or r[1] is None:
            continue
        n += 1
        if is_vt(cfg, h + 1800):
            vt += max(0.0, r[1])
        else:
            nt += max(0.0, r[1])
    return vt, nt, n


def refresh_days(cfg, c, now=None, max_days=120):
    """Doplní state.tarifa_dni {datum: [vt, nt]} o uzavreté dni (s aspoň 20 hodinami údajov). Vracia slovník."""
    now = now or time.time()
    try:
        dni = json.loads(get_state(c, "tarifa_dni", "") or "{}")
    except ValueError:
        dni = {}
    today = _dt.date.fromtimestamp(now)
    changed = False
    for k in range(1, max_days + 1):
        d = today - _dt.timedelta(days=k)
        key = d.isoformat()
        if key in dni:
            break          # starším dňom sa už nič nemení (dni sa dopĺňajú od najnovšieho)
        try:
            vt, nt, n = day_split(cfg, d)
        except Exception:
            traceback.print_exc()
            break
        if n >= MIN_HOURS_DAY:
            dni[key] = [round(vt, 3), round(nt, 3)]
            changed = True
    if changed:
        set_state(c, "tarifa_dni", json.dumps(dni, sort_keys=True))
    return dni


def _price_gap(cfg, day):
    kind = "s" if str(cfg.get("ENERGY_ENERGOPOMOC", "0")).strip() == "1" else "bez"
    row = ha_cost.TARIFF[kind][0]
    for r in ha_cost.TARIFF[kind]:
        if r[0] <= day:
            row = r
    return row[1] - row[2]


def evaluate(cfg, c, now=None):
    """Vyhodnotenie: {od, podiel_pred, podiel_po, dni_pred, dni_po, kwh_vt_po, kwh_nt_po, uspora_eur, dni:[...]}.
    Úspora = (podiel VT pred - po) * kWh po zapnutí * (cena VT - cena NT); bez aspoň 3 dní pred aj po je None."""
    now = now or time.time()
    dni = refresh_days(cfg, c, now) if known(cfg) else {}
    od = get_state(c, "tarifa_od", "") or None
    pred = {k: v for k, v in dni.items() if od and k < od}
    po = {k: v for k, v in dni.items() if od and k >= od}

    def agg(x):
        vt, nt = sum(v[0] for v in x.values()), sum(v[1] for v in x.values())
        return vt, nt, (vt / (vt + nt) if vt + nt > 0 else None)

    vt0, nt0, s0 = agg(pred)
    vt1, nt1, s1 = agg(po)
    uspora = None
    if s0 is not None and s1 is not None and len(pred) >= MIN_DAYS and len(po) >= MIN_DAYS:
        uspora = (s0 - s1) * (vt1 + nt1) * _price_gap(cfg, od)
    r = lambda v, n=3: None if v is None else round(v, n)
    return {"od": od, "podiel_pred": r(s0), "podiel_po": r(s1), "dni_pred": len(pred), "dni_po": len(po),
            "kwh_vt_po": r(vt1), "kwh_nt_po": r(nt1), "uspora_eur": r(uspora, 2),
            "dni": [{"datum": k, "vt_kwh": v[0], "nt_kwh": v[1]} for k, v in sorted(dni.items())[-30:]]}


def report(cfg, c, now=None):
    return {"stav": status(cfg, c, now), "vyhodnotenie": evaluate(cfg, c, now)}
