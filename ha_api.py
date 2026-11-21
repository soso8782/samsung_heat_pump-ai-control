#!/usr/bin/env python3
"""API pre stroje (integrácia Home Assistant „Samsung Heat Pump AI Control“), verzia v1.

Modul nemení stav aplikácie sám od seba: poskytuje správu kľúča integrácie (v tabuľke `state` databázy agenta ukladá
len SHA-256 kľúča), zostavenie stavu `build_state` z výstupu `app.status(cfg)` a overenie/vykonanie akcií.
Napojenie na HTTP server robí app.py (pozri odovzdanie v SPEC_ha_integracia.md).
"""
import hashlib, hmac, json, os, secrets, time
import i18n
from i18n import tr

API_VERSION = 1
PATH_STATE = "/api/ha/v1/state"
KEY_HASH = "ha_api_key_hash"
KEY_CREATED = "ha_api_key_created"
MODES = ("off", "advise", "control")
ROOM_MIN, ROOM_MAX = 15.0, 26.0          # hrubý mantinel; presný rozsah vynucuje app.parse_room_target
TUV_DEFAULT_TARGET = 50
FRESH_S = 300                            # staršie vzorky zberača = ESP „offline“

# predvolené názvy entít v ESP (ak app nepošle profil: pozri build_state(names=...))
DEFAULT_NAMES = {"vonku": "Vonkajšia teplota", "ciel_izby": "Cieľová izbová teplota", "posun": "Posun ekvitermickej krivky",
                 "wl": "Cieľová ekvitermická voda (WL)", "tw2": "Výstupná teplota vody (Tw2)", "tw1": "Spiatočka (Tw1)",
                 "dt": "Delta T vykurovacej vody", "freq": "Frekvencia kompresora", "chyba": "Chybový kód TČ",
                 "odmrazovanie": "Odmrazovanie", "tuv": "", "tuv_ciel": ""}


# ---------------------------------------------------------------- kľúč integrácie
def _get(c, k):
    r = c.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def _set(c, k, v):
    c.execute("INSERT OR REPLACE INTO state(k, v) VALUES(?, ?)", (k, str(v)))
    c.commit()


def _hash(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def new_key(c, now=None):
    """Vytvorí nový kľúč (starý prestane platiť), uloží jeho SHA-256 a vráti kľúč. Zobrazí sa len raz."""
    key = secrets.token_urlsafe(32)
    _set(c, KEY_HASH, _hash(key))
    _set(c, KEY_CREATED, int(now if now is not None else time.time()))
    return key


def revoke(c):
    c.execute("DELETE FROM state WHERE k IN (?, ?)", (KEY_HASH, KEY_CREATED))
    c.commit()


def key_info(c):
    """Pre kartu v nastaveniach: {"set": bool, "created": ts|None} (nikdy nie samotný kľúč ani hash)."""
    h = _get(c, KEY_HASH)
    try:
        created = int(_get(c, KEY_CREATED) or 0) or None
    except ValueError:
        created = None
    return {"set": bool(h), "created": created if h else None}


def check(c, header):
    """True, ak hlavička `Authorization: Bearer <kľúč>` zodpovedá uloženému kľúču. Bez kľúča vždy False."""
    stored = _get(c, KEY_HASH)
    if not stored or not isinstance(header, str):
        return False
    h = header.strip()
    if h[:7].lower() != "bearer ":
        return False
    tok = h[7:].strip()
    if not tok:
        return False
    return hmac.compare_digest(_hash(tok), stored)


# ---------------------------------------------------------------- stav
def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def _round(v, n=2):
    x = _num(v)
    if x is None:
        return None
    x = round(x, n)
    return int(x) if x == int(x) and n == 0 else x


def _truthy(v):
    if isinstance(v, str):
        return v.strip().lower() in ("on", "true", "1", "yes", "zapnuté", "áno")
    x = _num(v)
    return None if x is None and v is None else bool(x)


def _version():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def build_state(st, now=None, names=None, version=None):
    """Z výstupu app.status(cfg) (slovník `st`) zostaví JSON pre integráciu. Chýbajúce údaje = None, nikdy výnimka.

    names: slovník kľúč -> názov entity v ESP, t. j. profil.load()["esp"]["entity"] (kľúče vonku, ciel_izby, posun, wl, tw2,
    tw1, dt, freq, chyba, odmrazovanie, tuv, tuv_ciel); bez neho sa použijú predvolené názvy."""
    st = st if isinstance(st, dict) else {}
    now = int(now if now is not None else time.time())
    nm = dict(DEFAULT_NAMES)
    nm.update({k: v for k, v in (names or {}).items() if isinstance(v, str)})
    cur = st.get("current") if isinstance(st.get("current"), dict) else {}
    pi = st.get("profil_info") if isinstance(st.get("profil_info"), dict) else {}
    zi = pi.get("zariadenie_info") if isinstance(pi.get("zariadenie_info"), dict) else {}
    fn = pi.get("funkcie") if isinstance(pi.get("funkcie"), dict) else {}

    def val(key):
        e = nm.get(key)
        return _num(cur.get(e)) if e else None

    targets = st.get("targets") if isinstance(st.get("targets"), dict) else {}
    izby = []
    for r in pi.get("izby") or []:
        if not isinstance(r, dict):
            continue
        k = r.get("kluc")
        tmin = None
        if isinstance(targets.get(k), dict):
            tmin = _num(targets[k].get("min"))
        ref = bool(r.get("referencna"))
        izby.append({"kluc": k, "nazov": r.get("nazov") or k, "teplota": _round(cur.get(r.get("esp"))) if r.get("esp") else None,
                     "min": None if ref else tmin, "referencna": ref})

    freq = val("freq")
    defr = cur.get(nm["odmrazovanie"]) if nm.get("odmrazovanie") else None
    tuv_on = bool(fn.get("tuv"))
    tuv = None
    if tuv_on:
        ts = st.get("tuv") if isinstance(st.get("tuv"), dict) else {}
        dh = st.get("dhw") if isinstance(st.get("dhw"), dict) else {}
        ciel = val("tuv_ciel")
        ts_ent = (pi.get("tuv_entity") or {}) if isinstance(pi.get("tuv_entity"), dict) else {}
        if ciel is None and ts_ent.get("ciel"):
            ciel = _num(cur.get(ts_ent["ciel"]))
        teplota = val("tuv")
        if teplota is None and ts_ent.get("teplota"):
            teplota = _num(cur.get(ts_ent["teplota"]))
        tuv = {"teplota": _round(teplota), "ciel": _round(ciel), "ohrev_bezi": bool(ts.get("active") or dh.get("boosted"))}

    lr = st.get("last_run") if isinstance(st.get("last_run"), dict) else {}
    # Stav a zhrnutie agenta berieme z posledného hodnotenia; informačný záznam (napr. zmena cieľa z HA) ho nemaže.
    la = st.get("last_assessment") if isinstance(st.get("last_assessment"), dict) else None
    if la is None:
        la = lr
    err = val("chyba")
    poplachy = []
    for a in st.get("alerts") or []:
        if isinstance(a, dict):
            poplachy.append({"druh": str(a.get("kind") or ""), "text": str(a.get("text") or "")})

    age = _num(st.get("collector_age"))
    ha = st.get("ha") if isinstance(st.get("ha"), dict) else {}
    rng = pi.get("ciel_rozsah")
    if not (isinstance(rng, (list, tuple)) and len(rng) == 2 and _num(rng[0]) is not None and _num(rng[1]) is not None):
        rng = None
    nxt = _num(st.get("next_run"))
    return {
        "api": API_VERSION, "verzia": version or _version(), "ts": now,
        "zariadenie": {"nazov": pi.get("zariadenie") or None, "model": zi.get("model") or None, "typ": zi.get("typ"),
                       "vykon_kw": _num(zi.get("vykon_kw")), "kurenie": zi.get("kurenie") or pi.get("kurenie")},
        "izby": izby,
        "vonku": _round(val("vonku")), "ciel_izba": _round(val("ciel_izby")), "ciel_rozsah": list(rng) if rng else None,
        "posun": _round(val("posun")),
        "voda": {"vystup": _round(val("tw2")), "spiatocka": _round(val("tw1")), "ciel_krivky": _round(val("wl")),
                 "delta_t": _round(val("dt"))},
        "kompresor": {"frekvencia": _round(freq, 0), "bezi": None if freq is None else freq > 0,
                      "odmrazovanie": _truthy(defr)},
        "tuv": tuv,
        "agent": {"rezim": st.get("mode") if st.get("mode") in MODES else None, "posledny_beh": la.get("ts"),
                  "stav": la.get("stav"), "zhrnutie": la.get("summary"),
                  "dalsi_beh": int(nxt) if nxt else None},
        "chyba_tc": None if err is None else int(err),
        "poplachy": poplachy,
        "online": {"esp": None if age is None else age <= FRESH_S,
                   "zberac": "collector_error" not in st and age is not None,
                   "ha": bool(ha.get("ok")) if ha.get("configured") else None},
    }


# ---------------------------------------------------------------- akcie
# cesta -> názov funkcie. Názov bez podčiarkovníka = funkcia (cfg, body, who) v app.py (vracia (status, dict));
# názov so začiatočným podčiarkovníkom = funkcia (cfg, body, who) v tomto module.
ACTIONS = {
    "/api/ha/v1/mode": "_mode",
    "/api/ha/v1/room_target": "room_target_post",
    "/api/ha/v1/tuv_boost": "tuv_boost_post",
    "/api/ha/v1/run_now": "_run_now",
}


def validate_action(path, body, lang="sk"):
    """(ok, chyba) – validácia vstupov akcie; text chyby v jazyku `lang`."""
    if path not in ACTIONS:
        return False, tr("srv.ha_api.neznama_akcia", lang)
    if not isinstance(body, dict):
        return False, tr("srv.ha_api.neplatne_telo", lang)
    if path.endswith("/mode"):
        if body.get("mode") not in MODES:
            return False, tr("srv.ha_api.neplatny_rezim", lang)
    elif path.endswith("/room_target"):
        v = body.get("value")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return False, tr("srv.ha_api.neplatna_teplota", lang)
        if v != v or not (ROOM_MIN <= v <= ROOM_MAX):
            return False, tr("srv.ha_api.teplota_rozsah", lang, lo="%g" % ROOM_MIN, hi="%g" % ROOM_MAX)
    elif path.endswith("/tuv_boost"):
        t = body.get("target", TUV_DEFAULT_TARGET)
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not (45 <= t <= 52):
            return False, tr("srv.ha_api.tuv_rozsah", lang)
    return True, None


def _mode(cfg, body, who, adb=None):
    """Rovnaká logika ako POST /api/mode."""
    if adb is None:
        from common import adb
    c = adb(cfg)
    try:
        m = body.get("mode")
        old = _get(c, "mode") or "advise"
        _set(c, "mode", m)
        c.execute("INSERT INTO runs(ts,mode,status,stav,summary,actions) VALUES(?,?,?,?,?,?)",
                  (int(time.time()), m, "info", None, tr("srv.ha_api.log_rezim", i18n.home_lang(cfg), old=old, new=m), "[]"))
        c.commit()
        return 200, {"mode": m}
    finally:
        c.close()


def _run_now(cfg, body, who, adb=None):
    """Rovnaká logika ako POST /api/run_now."""
    if adb is None:
        from common import adb
    c = adb(cfg)
    try:
        _set(c, "run_now", "1")
        return 200, {"ok": True}
    finally:
        c.close()


def to_app_body(path, body):
    """Prevod tela HA akcie na telo funkcie v app.py (tuv_boost_post chce {action, target})."""
    if path.endswith("/tuv_boost"):
        return {"action": "start", "target": body.get("target", TUV_DEFAULT_TARGET)}
    return body


def dispatch(cfg, path, body, app_module, who="home-assistant"):
    """Overí vstup a vykoná akciu. Vracia (http_status, dict). `app_module` = modul app (kvôli room_target_post, tuv_boost_post)."""
    ok, err = validate_action(path, body, i18n.home_lang(cfg))
    if not ok:
        return (404 if path not in ACTIONS else 400), {"error": err}
    name = ACTIONS[path]
    fn = globals()[name] if name.startswith("_") else getattr(app_module, name)
    return fn(cfg, to_app_body(path, body), who)
