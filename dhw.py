#!/usr/bin/env python3
"""Plánovač ohrevu vody (TÚV) - SPEC bod 6. Predvolene VYPNUTÝ (state.dhw_plan = "0"). Deterministický, bez modelu.

V najteplejšom 2-hodinovom okne dňa (10:00-16:00 podľa predpovede) zvýši "Cieľovú teplotu TÚV" o 2 °C (najviac na 52 °C),
na konci okna vráti pôvodnú hodnotu. Jediná entita, ktorú tento modul zapisuje, je "Cieľová teplota TÚV" v rozsahu 40-52 °C.
Zápis ide VÝLUČNE cez agent.esp_set_number() (ten rešpektuje DRY_RUN). Volá sa z watchdogu každú minútu: tick().
"""
import json, re, time, urllib.request
import abtest, notify, tarifa
import i18n
from i18n import tr
import profil
from common import P, S, get_state, is_dry, set_state, tcq

# TÚV logika je aktívna len ak profil hovorí, že zariadenie má TÚV a má entitu cieľa TÚV; inak ENT_TARGET = "" (tick nerobí nič)
TUV_ON = bool(P["zariadenie"].get("tuv")) and bool(profil.device_entity("tuv_ciel", P))
ENT_TARGET = profil.device_entity("tuv_ciel", P) if TUV_ON else ""
ENT_BOOSTER = "Elektrický dohrev TÚV (Booster heater)"
ENT_ERROR = S.get("chyba", "")
T_MIN, T_MAX, T_STEP = 40.0, 52.0, 2.0
DEFAULT_WINDOW = ("12:00", "14:00")
STALE_S = 600
REASON_UP = "plánovač TÚV: prihriatie v okne"
REASON_DOWN = "plánovač TÚV: návrat na pôvodnú hodnotu"
MANUAL_MAX_S = 3 * 3600      # manuálny ohrev z webu: najdlhšie 3 hodiny
MANUAL_GAP_S = 30 * 60       # odstup medzi dvoma štartmi
MANUAL_MIN, MANUAL_MAX_T = 45.0, 52.0


def _num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _agent():
    import agent   # oneskorený import (agent importuje tento modul)
    return agent


def enabled(c):
    return get_state(c, "dhw_plan", "0") == "1"


def load_window(c):
    try:
        w = json.loads(get_state(c, "dhw_window", "") or "null")
        if isinstance(w, dict) and w.get("date") and w.get("start") and w.get("end"):
            return w
    except ValueError:
        pass
    return None


def today_window(c, now=None):
    """Dnešné okno alebo None (pre web)."""
    w = load_window(c)
    return w if w and w["date"] == time.strftime("%Y-%m-%d", time.localtime(now or time.time())) else None


def in_window(w, now):
    if not w or w.get("date") != time.strftime("%Y-%m-%d", time.localtime(now)):
        return False
    hm = time.strftime("%H:%M", time.localtime(now))
    return w["start"] <= hm < w["end"]


def pick_window(temps):
    """temps: {hodina(int): teplota}. 2-hodinové okno s najvyšším priemerom medzi 10:00-16:00 (začiatok 10..14); remíza = skôr."""
    best = None
    for s in range(10, 15):
        if s in temps and s + 1 in temps and temps[s] is not None and temps[s + 1] is not None:
            avg = (temps[s] + temps[s + 1]) / 2.0
            if best is None or avg > best[0] + 1e-9:
                best = (avg, s)
    if not best:
        return None
    return "%02d:00" % best[1], "%02d:00" % (best[1] + 2)


def tuv_tarifa(cfg):
    """TÚV v lacnej tarife je zapnutý (TUV_TARIFA=1) a tarifa je známa (TARIFA_VT_INTERVALY)."""
    return str((cfg or {}).get("TUV_TARIFA", "0")).strip() == "1" and tarifa.known(cfg)


def pick_window_tarifa(cfg, temps, date):
    """Okno TÚV s preferenciou NT: 2-hodinové okno (rovnaká dĺžka a počet ohrevov ako doteraz: jedno za deň) úplne v NT s najvyšším
    priemerom predpovede (lepšie COP). Najprv štarty 10..14 (ako doteraz), potom 0..22. Bez NT okna alebo bez predpovede None.
    Vracia (start, end, dôvod) kde dôvod je "nt_teplo" (NT a najteplejšie z doterajšieho rozsahu), "nt" (NT, mimo bežného rozsahu)."""
    y, m, d = (int(x) for x in date.split("-"))

    def ts(h):
        return int(time.mktime((y, m, d, h, 0, 0, 0, 0, -1)))

    def best(rng):
        b = None
        for s in rng:
            if s in temps and s + 1 in temps and temps[s] is not None and temps[s + 1] is not None \
                    and not tarifa.overlaps_vt(cfg, ts(s), ts(s) + 7200):
                avg = (temps[s] + temps[s + 1]) / 2.0
                if b is None or avg > b[0] + 1e-9:
                    b = (avg, s)
        return b
    b = best(range(10, 15))
    reason = "nt_teplo"
    if not b:
        b = best(range(0, 23))
        reason = "nt"
    if not b:
        return None
    return "%02d:00" % b[1], "%02d:00" % (b[1] + 2), reason


def fetch_temps(cfg, date):
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s&hourly=temperature_2m&forecast_days=2&timezone=auto"
           % (cfg["LAT"], cfg["LON"]))
    with urllib.request.urlopen(url, timeout=15) as r:
        h = json.loads(r.read().decode("utf-8"))["hourly"]
    out = {}
    for t, v in zip(h["time"], h["temperature_2m"]):
        if t.startswith(date):
            out[int(t[11:13])] = v
    return out


def ensure_window(cfg, c, now=None):
    """Raz denne po 06:00 vyberie dnešné okno a uloží state.dhw_window. Vracia okno alebo None."""
    now = now or time.time()
    lt = time.localtime(now)
    today = time.strftime("%Y-%m-%d", lt)
    w = load_window(c)
    if w and w["date"] == today:
        return w
    if lt.tm_hour < 6:
        return None
    start = end = None
    reason = ""
    if cfg.get("LAT") and cfg.get("LON"):
        last_try = float(get_state(c, "dhw_fetch_try", "0") or 0)
        if now - last_try < 300:
            return None
        try:
            set_state(c, "dhw_fetch_try", now)
            temps = fetch_temps(cfg, today)
            win = pick_window_tarifa(cfg, temps, today) if tuv_tarifa(cfg) else None
            if win:
                start, end, reason = win
            else:
                win = pick_window(temps)
                if win:
                    start, end = win
                    reason = "teplo" if tuv_tarifa(cfg) else ""
        except Exception as e:
            print("plánovač TÚV: predpoveď nedostupná (%s)" % type(e).__name__, flush=True)
            if lt.tm_hour < 9:
                return None   # ešte skúsime znova; po 09:00 použijeme predvolené okno
    if not start:
        start, end = DEFAULT_WINDOW
    w = {"date": today, "start": start, "end": end}
    if reason:
        w["reason"] = reason      # nt_teplo / nt / teplo (iba pri TUV_TARIFA=1; bez NT okna sa použije pôvodný výber)
    set_state(c, "dhw_window", json.dumps(w))
    return w


def _result(cfg, good):
    """Text výsledku zápisu pre actions.result (v jazyku domácnosti)."""
    hl = i18n.home_lang(cfg)
    return (tr("srv.dhw.res_dry", hl) if is_dry(cfg) else tr("srv.run.written", hl)) if good else tr("srv.run.unconfirmed_short", hl)


def _log_action(c, old, new, reason, applied, result):
    c.execute("INSERT INTO actions(ts, entity, old, new, reason, applied, result) VALUES(?,?,?,?,?,?,?)",
              (int(time.time()), ENT_TARGET, old, new, reason, applied, result))
    c.commit()


def _write_target(cfg, value):
    """Jediný zápis tohto modulu: cieľ TÚV v rozsahu 40-52 °C cez agent.esp_set_number (DRY_RUN sa rešpektuje tam)."""
    if not ENT_TARGET:
        raise ValueError(tr("srv.dhw.err_bez_tuv", i18n.home_lang(cfg)))
    if not (T_MIN <= value <= T_MAX):
        raise ValueError(tr("srv.dhw.err_rozsah", i18n.home_lang(cfg), v=value, lo=T_MIN, hi=T_MAX))
    ag = _agent()
    ag.esp_set_number(cfg, ENT_TARGET, "%g" % value)
    if is_dry(cfg):
        return True
    time.sleep(3)
    back = _num(ag.esp_get(cfg, "number", ENT_TARGET).get("value"))
    return back is not None and abs(back - value) < 1e-6


def can_start(cfg, c, lang="sk"):
    """(ok, dôvod): agent nie je off, chybový kód 0, dáta čerstvé, booster vypnutý."""
    ag = _agent()
    if not ENT_TARGET:
        return False, tr("srv.dhw.bez_tuv", lang)
    if abtest.active(c):
        return False, tr("srv.dhw.abtest", lang)
    if get_state(c, "mode", "advise") == "off":
        return False, tr("srv.dhw.agent_off", lang)
    r = tcq(cfg, "SELECT v FROM meta WHERE k='last_sample'")
    age = int(time.time()) - int((r[0][0] if r else 0) or 0)
    if age > STALE_S:
        return False, tr("srv.dhw.stare", lang, n=age)
    if ENT_ERROR:   # bez entity chybového kódu sa táto poistka preskočí
        code = _num(ag.esp_get(cfg, "sensor", ENT_ERROR).get("value"))
        if code is None or code != 0:
            return False, tr("srv.dhw.chyba_kod", lang, code=code)
    if ag.esp_get(cfg, "binary_sensor", ENT_BOOSTER).get("value") is not False:
        return False, tr("srv.dhw.booster_on", lang)
    return True, ""


def start_boost(cfg, c, w, lang="sk"):
    """Prihriatie na začiatku okna. Vracia text výsledku."""
    ok, why = can_start(cfg, c, lang)
    if not ok:
        return tr("srv.dhw.caka", lang, why=why)
    base = _num(_agent().esp_get(cfg, "number", ENT_TARGET).get("value"))
    marker = "%s %s" % (w["date"], w["start"])
    if base is None:
        return tr("srv.dhw.caka_cielovy", lang)
    if base >= T_MAX or base < T_MIN:
        set_state(c, "dhw_done", marker)
        return tr("srv.dhw.nic_mimo", lang, base=base)
    new = min(base + T_STEP, T_MAX)
    set_state(c, "dhw_base", base)
    set_state(c, "dhw_boost_value", new)
    set_state(c, "dhw_done", marker)
    try:
        good = _write_target(cfg, new)
    except Exception as e:
        _log_action(c, base, new, tr("srv.dhw.reason_up", i18n.home_lang(cfg)), 0, tr("srv.run.write_err", i18n.home_lang(cfg), e=e))
        set_state(c, "dhw_boosted", "0")
        return tr("srv.dhw.zapis_chyba", lang, err=e)
    set_state(c, "dhw_boosted", "1")    # aj pri nepotvrdenom zápise: na konci okna sa hodnota vráti
    _log_action(c, base, new, "%s %s-%s" % (tr("srv.dhw.reason_up", i18n.home_lang(cfg)), w["start"], w["end"]), 1 if good else 0,
                _result(cfg, good))
    return tr("srv.dhw.prihriate", lang, base=base, new=new)


def restore(cfg, c, why, lang="sk"):
    """Vráti pôvodný cieľ TÚV. Ak ho medzitým niekto zmenil ručne (nie je to naša hodnota), nechá ho tak. Vracia text."""
    base = _num(get_state(c, "dhw_base"))
    bv = _num(get_state(c, "dhw_boost_value"))
    if base is None or not (T_MIN <= base <= T_MAX):
        set_state(c, "dhw_boosted", "0")
        return tr("srv.dhw.nie_je_co", lang)
    ag = _agent()
    cur = None if is_dry(cfg) else _num(ag.esp_get(cfg, "number", ENT_TARGET).get("value"))
    if not is_dry(cfg) and cur is None:
        raise RuntimeError(tr("srv.dhw.cielovy_precitat_znova", lang))
    if is_dry(cfg) or bv is None or abs(cur - bv) < 1e-6:
        good = _write_target(cfg, base)
        _log_action(c, bv, base, "%s (%s)" % (tr("srv.dhw.reason_down", i18n.home_lang(cfg)), _why(why, i18n.home_lang(cfg))), 1 if good else 0,
                    _result(cfg, good))
        res = tr("srv.dhw.vratene", lang, base=base, why=_why(why, lang))
    else:
        _log_action(c, cur, cur, "%s (%s)" % (tr("srv.dhw.reason_down", i18n.home_lang(cfg)), _why(why, i18n.home_lang(cfg))), 0,
                    tr("srv.dhw.zmenil_pouzivatel", i18n.home_lang(cfg), cur=cur))
        res = tr("srv.dhw.ponechane", lang, cur=cur)
    set_state(c, "dhw_boosted", "0")
    return res


_WHY = {"zapol sa elektrický dohrev TÚV": "booster", "koniec času ohrevu": "koniec_casu", "bojler dosiahol cieľ": "ciel",
        "plánovač vypnutý": "plan_off", "koniec okna": "koniec_okna"}
_WHY_WEB = re.compile(r"^zastavené z webu \((.*)\)$", re.S)


def _why(why, lang):
    """dôvod vrátenia cieľa (interná slovenská značka, preklad sa robí pri zápise do DB a v texte výsledku)"""
    if lang == "sk":
        return why
    m = _WHY_WEB.match(why or "")
    if m:
        return tr("srv.dhw.why_web", lang, who=m.group(1))
    if why not in _WHY:
        return why
    return tr("srv.dhw.why_" + _WHY[why], lang)


def _fmt(v):
    return ("%g" % v).replace(".", ",")


def manual_status(c, now=None):
    """Stav manuálneho ohrevu z webu (pre /api/status): active, until, target, base, last."""
    now = now or time.time()
    boosted = get_state(c, "dhw_boosted", "0") == "1"
    until = _num(get_state(c, "dhw_manual_until", ""))
    active = bool(boosted and until is not None and until > now)
    return {"active": active, "until": int(until) if active else None,
            "target": _num(get_state(c, "dhw_boost_value")) if active else None,
            "base": _num(get_state(c, "dhw_base")) if boosted else None,
            "last": (int(_num(get_state(c, "dhw_manual_last", ""))) if _num(get_state(c, "dhw_manual_last", "")) is not None else None)}


def manual_start(cfg, c, target, who, now=None, lang="sk"):
    """Manuálny ohrev z webu: dočasne zvýši cieľ TÚV (45-52, krok 1). Vracia (ok, text)."""
    now = now or time.time()
    t = _num(target)
    if isinstance(target, bool) or t is None or not (MANUAL_MIN <= t <= MANUAL_MAX_T) or abs(t - round(t)) > 1e-9:
        return False, tr("srv.dhw.teplota_rozsah", lang)
    t = float(round(t))
    ok, why = can_start(cfg, c, lang)
    if not ok:
        return False, tr("srv.dhw.nejde", lang, why=why)
    last = _num(get_state(c, "dhw_manual_last", ""))
    if last is not None and now - last < MANUAL_GAP_S:
        return False, tr("srv.dhw.dalsi", lang, n=max(1, int((MANUAL_GAP_S - (now - last) + 59) // 60)))
    was_boosted = get_state(c, "dhw_boosted", "0") == "1"
    old_until = get_state(c, "dhw_manual_until", "")
    if was_boosted and _num(get_state(c, "dhw_base")) is not None:
        base = _num(get_state(c, "dhw_base"))
    else:
        try:
            base = _num(_agent().esp_get(cfg, "number", ENT_TARGET).get("value"))
        except Exception:
            base = None
        if base is None:
            return False, tr("srv.dhw.ciel_precitat", lang)
    if t <= base:
        return False, tr("srv.dhw.vyssia", lang, base=i18n.dec(_fmt(base), lang))
    old_bv = get_state(c, "dhw_boost_value", "")
    set_state(c, "dhw_base", base)
    set_state(c, "dhw_boost_value", t)
    set_state(c, "dhw_manual_until", int(now + MANUAL_MAX_S))
    set_state(c, "dhw_manual_last", int(now))
    reason = tr("srv.dhw.reason_manual", i18n.home_lang(cfg), who=who)
    try:
        good = _write_target(cfg, t)
    except Exception as e:
        set_state(c, "dhw_boost_value", old_bv)
        set_state(c, "dhw_manual_until", old_until)
        set_state(c, "dhw_boosted", "1" if was_boosted else "0")
        _log_action(c, base, t, reason, 0, tr("srv.run.write_err", i18n.home_lang(cfg), e=e))
        return False, tr("srv.dhw.zapis_zlyhal", lang, err=e)
    set_state(c, "dhw_boosted", "1")    # aj pri nepotvrdenom zápise: na konci sa hodnota vráti
    _log_action(c, base, t, reason, 1 if good else 0, _result(cfg, good))
    text = tr("srv.dhw.spusteny", lang, t=i18n.dec(_fmt(t), lang), until=time.strftime("%H:%M", time.localtime(now + MANUAL_MAX_S)))
    if is_dry(cfg):
        text += tr("srv.dhw.testovacie", lang)
    return True, text


def manual_stop(cfg, c, who, lang="sk"):
    """Zastaví manuálny ohrev a vráti pôvodný cieľ. Vracia (ok, text)."""
    if _num(get_state(c, "dhw_manual_until", "")) is None:
        return False, tr("srv.dhw.nebezi", lang)
    base = _num(get_state(c, "dhw_base"))
    try:
        restore(cfg, c, "zastavené z webu (%s)" % who, lang)
    except RuntimeError as e:
        return False, tr("srv.dhw.stop_chyba", lang, err=e)
    set_state(c, "dhw_manual_until", "")
    return True, tr("srv.dhw.zastaveny", lang, base=i18n.dec(_fmt(base), lang) if base is not None else "?")


def set_target_manual(cfg, c, value, who, lang="sk"):
    """Trvalá zmena cieľa TÚV z webu (správca; 40-52, celé číslo, nie počas ohrevu). Vracia (ok, text)."""
    if get_state(c, "dhw_boosted", "0") == "1":
        return False, tr("srv.dhw.pocas_ohrevu", lang)
    v = _num(value)
    if isinstance(value, bool) or v is None or not (T_MIN <= v <= T_MAX) or abs(v - round(v)) > 1e-9:
        return False, tr("srv.dhw.ciel_cele", lang)
    v = float(round(v))
    old = None
    if not is_dry(cfg):
        try:
            old = _num(_agent().esp_get(cfg, "number", ENT_TARGET).get("value"))
        except Exception:
            old = None
    reason = tr("srv.dhw.reason_set", i18n.home_lang(cfg), who=who)
    try:
        good = _write_target(cfg, v)
    except Exception as e:
        _log_action(c, old, v, reason, 0, tr("srv.run.write_err", i18n.home_lang(cfg), e=e))
        return False, tr("srv.dhw.zapis_zlyhal", lang, err=e)
    _log_action(c, old, v, reason, 1 if good else 0, _result(cfg, good))
    if not good:
        return False, tr("srv.dhw.nepotvrdit", lang)
    return True, tr("srv.dhw.ciel_je", lang, v=i18n.dec(_fmt(v), lang))


def _bojler_now(cfg, now):
    """Posledná teplota bojlera zo samples (vek <= STALE_S) alebo None."""
    if not S.get("bojler"):
        return None
    try:
        r = tcq(cfg, "SELECT ts, value FROM samples WHERE entity=? AND value IS NOT NULL ORDER BY ts DESC LIMIT 1", (S["bojler"],))
    except Exception:
        return None
    if not r or now - int(r[0][0]) > STALE_S or abs(r[0][1]) > 6000:
        return None
    return float(r[0][1])


def _tick_manual(cfg, c, now, lang="sk"):
    """Vetva ticku pre manuálny ohrev z webu (dhw_manual_until je číslo)."""
    until = _num(get_state(c, "dhw_manual_until", ""))
    try:
        booster_on = _agent().esp_get(cfg, "binary_sensor", ENT_BOOSTER).get("value") is True
    except Exception:
        booster_on = False
    if booster_on:
        res = restore(cfg, c, "zapol sa elektrický dohrev TÚV", lang)
        set_state(c, "dhw_manual_until", "")
        notify.event(cfg, "tuv", tr("srv.dhw.manual_rus_t", lang), tr("srv.dhw.manual_rus", lang, res=res))
        return tr("srv.dhw.t_manual_booster", lang, res=res)
    if now >= until:
        res = restore(cfg, c, "koniec času ohrevu", lang)
        set_state(c, "dhw_manual_until", "")
        return tr("srv.dhw.t_manual_koniec", lang, res=res)
    bv = _num(get_state(c, "dhw_boost_value"))
    b = _bojler_now(cfg, now)
    if b is not None and bv is not None and b >= bv - 0.5:
        res = restore(cfg, c, "bojler dosiahol cieľ", lang)
        set_state(c, "dhw_manual_until", "")
        return tr("srv.dhw.t_manual_ciel", lang, res=res)
    return tr("srv.dhw.t_manual_bezi", lang)


def tick(cfg, c, now=None):
    """Jeden krok plánovača (z watchdogu každú minútu). Vracia krátky popis, čo spravil (pre log/testy)."""
    now = now or time.time()
    lang = i18n.home_lang(cfg)
    if not ENT_TARGET:
        return tr("srv.dhw.t_necinny_bez", lang)
    w = ensure_window(cfg, c, now)
    boosted = get_state(c, "dhw_boosted", "0") == "1"
    on = enabled(c)
    inw = in_window(w, now)
    if boosted:
        if _num(get_state(c, "dhw_manual_until", "")) is not None:
            return _tick_manual(cfg, c, now, lang)
        # poistka: zapnutý booster počas prihriatia
        try:
            booster_on = _agent().esp_get(cfg, "binary_sensor", ENT_BOOSTER).get("value") is True
        except Exception:
            booster_on = False
        if booster_on:
            res = restore(cfg, c, "zapol sa elektrický dohrev TÚV", lang)
            set_state(c, "dhw_plan", "0")
            base = _num(get_state(c, "dhw_base"))
            notify.event(cfg, "tuv", tr("srv.dhw.plan_rus_t", lang), tr("srv.dhw.plan_rus", lang, res=res))
            return tr("srv.dhw.t_booster", lang, res=res)
        if not on:
            return tr("srv.dhw.t_vypnuty_web", lang, res=restore(cfg, c, "plánovač vypnutý", lang))
        if not inw:
            return tr("srv.dhw.t_koniec_okna", lang, res=restore(cfg, c, "koniec okna", lang))
        return tr("srv.dhw.t_prihriate_caka", lang)
    if on and inw:
        marker = "%s %s" % (w["date"], w["start"])
        if get_state(c, "dhw_done", "") == marker:
            return tr("srv.dhw.t_okno_spracovane", lang)
        return start_boost(cfg, c, w, lang)
    return tr("srv.dhw.t_necinny", lang)
