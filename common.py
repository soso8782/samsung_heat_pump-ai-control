#!/usr/bin/env python3
"""Spoločné funkcie pre agenta kúrenia a web (iba štandardná knižnica)."""
import json, os, sqlite3, time, urllib.request

import profil

ROOT = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    "OPENROUTER_API_KEY": "",
    "OPENROUTER_MODEL": "anthropic/claude-haiku-5.5",
    "OPENROUTER_FALLBACK_MODEL": "google/gemini-2.5-flash-lite",   # prázdne = záložný model vypnutý
    "ESP_URL": "http://192.168.1.199",
    "LAT": "", "LON": "",
    "INTERVAL_MIN": "60",
    "LLM_MAX_GAP_H": "4",
    "RETENTION_FULL_DAYS": "60",
    "NOTIFY_URL": "",
    "UI_LANG": "sk",             # jazyk upozornení, agenta a HA senzorov (sk / en); jazyk webu si volí každý v prehliadači
    "HA_URL": "http://homeassistant.local:8123",
    "HA_TOKEN": "",
    "HA_PRITOMNOST": "",         # entity_id prítomnosti v HA (group/person/zone, čiarkou oddelené); prázdne = bez automatiky „Preč z domu“
    "DRY_RUN": "0",
    "TC_DB": os.path.join(ROOT, "tc.db"),
    "AGENT_DB": os.path.join(ROOT, "agent.db"),
    "WEB_PORT": "80",
}


def load_cfg():
    cfg = dict(DEFAULTS)
    try:
        with open(os.path.join(ROOT, "config.env"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    if os.environ.get("DRY_RUN") not in (None, ""):   # premenná prostredia má prednosť pred config.env
        cfg["DRY_RUN"] = os.environ["DRY_RUN"]
    return cfg


def is_dry(cfg):
    """Skúšobný režim: do ESP ani na webhook sa nič neodosiela."""
    return str(cfg.get("DRY_RUN", "")).strip().lower() in ("1", "true", "yes", "on")


def cfg_int(cfg, key, default, lo, hi):
    try:
        v = int(float(cfg.get(key) or default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def save_cfg(updates):
    """Prepíše vybrané kľúče v config.env (ostatné riadky a komentáre ponechá). Súbor ostáva čitateľný len pre roota."""
    path = os.path.join(ROOT, "config.env")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().split("\n")
    except FileNotFoundError:
        lines = []
    left = dict(updates)
    for i, line in enumerate(lines):
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in left:
                lines[i] = "%s=%s" % (k, left.pop(k))
    for k, v in left.items():
        lines.append("%s=%s" % (k, v))
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip("\n") + "\n")
    os.replace(tmp, path)


def adb(cfg):
    c = sqlite3.connect(cfg["AGENT_DB"], timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, ts INTEGER, mode TEXT, status TEXT, stav TEXT,
        summary TEXT, advice TEXT, model TEXT, input TEXT, output TEXT, actions TEXT, error TEXT,
        tok_in INTEGER, tok_out INTEGER, cost REAL);
    CREATE INDEX IF NOT EXISTS runs_ts ON runs(ts);
    CREATE TABLE IF NOT EXISTS actions(ts INTEGER, entity TEXT, old REAL, new REAL, reason TEXT,
        applied INTEGER, result TEXT);
    CREATE INDEX IF NOT EXISTS actions_ts ON actions(ts);
    CREATE TABLE IF NOT EXISTS riadiaca_log(ts INTEGER, stary TEXT, novy TEXT, kluc TEXT, kto TEXT);
    CREATE INDEX IF NOT EXISTS riadiaca_log_ts ON riadiaca_log(ts);
    CREATE TABLE IF NOT EXISTS days(date TEXT PRIMARY KEY, data TEXT);
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, ts INTEGER, role TEXT, text TEXT, cost REAL);
    CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);
    CREATE TABLE IF NOT EXISTS lessons(id INTEGER PRIMARY KEY, ts INTEGER, date TEXT, lessons TEXT, summary TEXT,
        status TEXT, error TEXT, cost REAL);
    CREATE TABLE IF NOT EXISTS notify_log(id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, title TEXT, message TEXT, ok INTEGER, error TEXT);
    CREATE INDEX IF NOT EXISTS notify_log_ts ON notify_log(ts);
    CREATE TABLE IF NOT EXISTS interventions(id INTEGER PRIMARY KEY, ts INTEGER, source TEXT, kind TEXT, room TEXT, text TEXT,
        status TEXT, due_ts INTEGER, result TEXT, notified INTEGER DEFAULT 0);
    CREATE INDEX IF NOT EXISTS interventions_ts ON interventions(ts);
    CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY, ts INTEGER, week_from TEXT, week_to TEXT, text TEXT, cost REAL);
    CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, created INTEGER, expires INTEGER);
    CREATE TABLE IF NOT EXISTS contact_groups(id INTEGER PRIMARY KEY, name TEXT, room TEXT, notify_min INTEGER, sort INTEGER);
    CREATE TABLE IF NOT EXISTS contacts(entity_id TEXT PRIMARY KEY, ha_name TEXT, alias TEXT, group_id INTEGER, enabled INTEGER,
        device_class TEXT, state TEXT, since INTEGER, area TEXT, note TEXT, first_seen INTEGER, last_seen INTEGER);
    CREATE TABLE IF NOT EXISTS contact_events(id INTEGER PRIMARY KEY, ts INTEGER, entity_id TEXT, state TEXT);
    CREATE INDEX IF NOT EXISTS contact_events_ts ON contact_events(ts);
    CREATE INDEX IF NOT EXISTS contact_events_ent ON contact_events(entity_id, ts);
    CREATE TABLE IF NOT EXISTS temp_sensors(entity_id TEXT PRIMARY KEY, ha_name TEXT, alias TEXT, group_id INTEGER, enabled INTEGER,
        kind TEXT, value REAL, updated INTEGER, area TEXT, note TEXT, alert_min REAL, alert_max REAL, first_seen INTEGER, last_seen INTEGER);
    CREATE TABLE IF NOT EXISTS temp_samples(ts INTEGER, entity_id TEXT, value REAL);
    CREATE INDEX IF NOT EXISTS temp_samples_ent ON temp_samples(entity_id, ts);
    CREATE INDEX IF NOT EXISTS temp_samples_ts ON temp_samples(ts);
    CREATE TABLE IF NOT EXISTS users(email TEXT PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('admin','user')), added_ts INTEGER);
    CREATE TABLE IF NOT EXISTS push_subs(endpoint TEXT PRIMARY KEY, email TEXT, role TEXT, p256dh TEXT, auth TEXT, ua TEXT,
        created INTEGER, last_ok INTEGER, last_err TEXT);
    """)
    # rozšírenie staršej schémy (ALTER ... ADD COLUMN len ak stĺpec chýba)
    for tb, col, ddl in (("actions", "undone", "INTEGER DEFAULT 0"), ("sessions", "role", "TEXT"), ("sessions", "email", "TEXT"),
                         ("messages", "author", "TEXT"),
                         ("reports", "kind", "TEXT"), ("reports", "data", "TEXT"),
                         ("interventions", "detail", "TEXT"), ("interventions", "result48", "TEXT")):
        if col not in [r[1] for r in c.execute("PRAGMA table_info(%s)" % tb)]:
            try:
                c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (tb, col, ddl))
                c.commit()
            except sqlite3.OperationalError:
                pass
    return c


def get_state(c, k, default=None):
    r = c.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def set_state(c, k, v):
    c.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (k, str(v)))
    c.commit()


def tcq(cfg, sql, args=()):
    c = sqlite3.connect("file:%s?mode=ro" % cfg["TC_DB"], uri=True, timeout=30)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


# kľúč -> názov entity v ESP (tabuľka samples)
P = profil.load()                 # profil inštalácie (bez profil.json = pôvodný dom, správanie ako v1.x)
S = profil.esp_s(P)               # chýbajúca entita v profile = kľúč v S chýba
REF_ROOM = profil.reference_room(P)   # referenčná izba (podľa nej firmvér posúva krivku)
REF_NAME = profil.room_names(P)[REF_ROOM]
INV = {v: k for k, v in S.items()}


def hour_stats(cfg, a, b):
    """Hodinové štatistiky v intervale [a, b). Vracia zoznam slovníkov po hodinách od a."""
    n = max(1, int((b - a + 3599) // 3600))
    hours = [{"ts": a + i * 3600} for i in range(n)]
    names = list(S.values())
    rows = tcq(cfg, "SELECT entity, (ts-?)/3600, avg(value), min(value), max(value), count(*) FROM samples "
                    "WHERE ts>=? AND ts<? AND entity IN (%s) GROUP BY 1,2" % ",".join("?" * len(names)),
               (a, a, b, *names))
    for ent, h, avg, mn, mx, cnt in rows:
        h = int(h)
        if not (0 <= h < n):
            continue
        k = INV[ent]
        d = hours[h]
        d[k] = None if avg is None else round(avg, 2)
        d[k + "_min"] = mn
        d[k + "_max"] = mx
        d["n"] = max(d.get("n", 0), cnt)
    # spotreba za hodinu = súčet kladných prírastkov denného počítadla (reset o polnoci / po reštarte ESP sa ignoruje)
    sp = tcq(cfg, "SELECT ts, value FROM samples WHERE entity=? AND ts>=? AND ts<? ORDER BY ts", (S.get("spotreba", ""), a - 120, b))
    pv = None
    for ts, v in sp:
        if pv is not None and v is not None and ts >= a:
            dv = v - pv
            if 0 < dv < 2:   # za minútu reálne najviac ~0,15 kWh; väčší skok = chyba dát
                h = int((ts - a) // 3600)
                if 0 <= h < n:
                    hours[h]["kwh"] = hours[h].get("kwh", 0.0) + dv
        pv = v
    for d in hours:
        if "kwh" in d:
            d["kwh"] = round(d["kwh"], 2)
    # kompresor: podiel behu, štarty, priemerná frekvencia počas behu, delta T iba počas behu
    fr = tcq(cfg, "SELECT ts, value FROM samples WHERE entity=? AND ts>=? AND ts<? ORDER BY ts", (S.get("freq", ""), a - 120, b))
    dts = dict(tcq(cfg, "SELECT ts, value FROM samples WHERE entity=? AND ts>=? AND ts<?", (S.get("dt", ""), a, b)))
    prev = None
    acc = {}
    for ts, v in fr:
        on = v is not None and v > 0
        if ts >= a:
            h = int((ts - a) // 3600)
            x = acc.setdefault(h, {"n": 0, "run": 0, "f": 0.0, "starts": 0, "dt": [], })
            x["n"] += 1
            if on:
                x["run"] += 1
                x["f"] += v
                if ts in dts and v >= 20:
                    x["dt"].append(dts[ts])
            if prev is False and on:
                x["starts"] += 1
        prev = on
    for h, x in acc.items():
        if 0 <= h < n and x["n"]:
            d = hours[h]
            d["beh"] = round(100.0 * x["run"] / x["n"])
            d["starty"] = x["starts"]
            d["f_avg"] = round(x["f"] / x["run"], 1) if x["run"] else 0
            d["dt_beh"] = round(sum(x["dt"]) / len(x["dt"]), 1) if x["dt"] else None
    # ohrev TÚV: minúty, keď bol 3-cestný ventil na bojleri (Tank); vtedy beh a kWh nepatria kúreniu
    for s, e in dhw_intervals(cfg, a, b):
        for h in range(max(0, int((s - a) // 3600)), min(n, int((e - 1 - a) // 3600) + 1)):
            lo, hi = max(s, a + h * 3600), min(e, a + (h + 1) * 3600)
            if hi > lo:
                hours[h]["tuv_min"] = hours[h].get("tuv_min", 0) + (hi - lo) / 60.0
    for d in hours:
        if d.get("n"):
            d["tuv_min"] = int(round(d.get("tuv_min", 0)))
    return hours


DHW_MIN_S = 120   # kratšie prepnutie ventilu na bojler (test ventilu, zákmit) sa nepočíta ako ohrev


def dhw_intervals(cfg, a, b):
    """Ohrevy TÚV v [a, b) z udalostí 3-cestného ventilu: [(od, do)], ventil na bojleri (hodnota obsahuje „Tank“).
    Prebiehajúci ohrev končí poslednou vzorkou + 60 s; úseky kratšie ako DHW_MIN_S sa vynechajú. Bez entity ventilu []."""
    ent = profil.device_entity("ventil", P)
    if not ent:
        return []
    try:
        r = tcq(cfg, "SELECT new FROM events WHERE entity=? AND ts<? ORDER BY ts DESC LIMIT 1", (ent, a))
        evs = tcq(cfg, "SELECT ts, new FROM events WHERE entity=? AND ts>=? AND ts<? ORDER BY ts", (ent, a, b))
        last = int((tcq(cfg, "SELECT v FROM meta WHERE k='last_sample'") or [[0]])[0][0] or 0)
    except Exception:
        return []
    out, start = [], (a if r and "Tank" in str(r[0][0] or "") else None)
    for ts, new in evs:
        tank = "Tank" in str(new or "")
        if tank and start is None:
            start = ts
        elif not tank and start is not None:
            out.append((start, ts)); start = None
    if start is not None:
        out.append((start, min(b, max(start, last + 60))))
    return [(s, e) for s, e in out if e - s >= DHW_MIN_S]


SETTINGS = profil.settings_map(P)
ROOMS = profil.room_keys(P)       # referenčná izba prvá


def day_bounds(dstr=None):
    """(a, b) = lokálna polnoc dňa a nasledujúceho dňa; dstr 'YYYY-MM-DD' alebo None = dnes."""
    try:
        tm = time.strptime(dstr, "%Y-%m-%d")
    except (ValueError, TypeError):
        tm = time.localtime()
    a = int(time.mktime((tm.tm_year, tm.tm_mon, tm.tm_mday, 0, 0, 0, 0, 0, -1)))
    b = int(time.mktime((tm.tm_year, tm.tm_mon, tm.tm_mday + 1, 0, 0, 0, 0, 0, -1)))
    return a, b


def day_summary(cfg, a, b, hours=None):
    """Denný súhrn z hodinových štatistík - základ pre učenie agenta aj pre web."""
    hours = hours if hours is not None else hour_stats(cfg, a, b)

    def avg(k):
        xs = [h[k] for h in hours if h.get(k) is not None]
        return round(sum(xs) / len(xs), 2) if xs else None

    def ext(k, fn):
        xs = [h[k] for h in hours if h.get(k) is not None]
        return fn(xs) if xs else None

    hh = [h for h in hours if h.get("beh") is not None]
    s = {k: avg(k) for k in ROOMS + ("vonku", "wl", "tw2", "dt_beh", "posun", "bojler")}
    s.update(date=time.strftime("%Y-%m-%d", time.localtime(a)), vonku_min=ext("vonku_min", min), vonku_max=ext("vonku_max", max),
             **{REF_ROOM + "_min": ext(REF_ROOM + "_min", min), REF_ROOM + "_max": ext(REF_ROOM + "_max", max)},
             kwh=round(sum(h.get("kwh") or 0 for h in hours), 2),
             beh=round(sum(h["beh"] for h in hh) / len(hh)) if hh else None,
             starty=sum(h.get("starty") or 0 for h in hours), hodin_s_datami=len([h for h in hours if h.get("n")]))
    tv = dhw_intervals(cfg, a, b)
    s["tuv_n"], s["tuv_min"] = len(tv), int(round(sum(e - x for x, e in tv) / 60.0))
    names = list(SETTINGS.values())
    # nastavenia: hodnota na KONCI dňa (priemer by pri zmene počas dňa dal neexistujúcu hodnotu) + počet zmien v dni
    for k, nme in SETTINGS.items():
        r = tcq(cfg, "SELECT value FROM samples WHERE entity=? AND ts>=? AND ts<? ORDER BY ts DESC LIMIT 1", (nme, a, b))
        s[k] = None if not r or r[0][0] is None else round(r[0][0], 1)
    s["zmien_nastaveni"] = tcq(cfg, "SELECT count(*) FROM events WHERE ts>=? AND ts<? AND old IS NOT NULL AND entity IN (%s)"
                               % ",".join("?" * len(names)), (a, b, *names))[0][0]
    ciel, mp = s.get("ciel_izba") or 23.5, s.get("max_posun")
    s["hod_pod_cielom"] = len([h for h in hours if h.get(REF_ROOM) is not None and h[REF_ROOM] < ciel - 0.4])
    s["hod_nad_cielom"] = len([h for h in hours if h.get(REF_ROOM) is not None and h[REF_ROOM] > ciel + 0.4])
    s["hod_posun_na_maxime"] = len([h for h in hours if mp is not None and h.get("posun") is not None and h["posun"] >= mp - 0.05])
    s["hod_dt_vysoka"] = len([h for h in hours if (h.get("dt_beh") or 0) > 6.5])
    # spotreba prepočítaná na 1 °C rozdielu (vnútri 23 °C - vonku) a deň: porovnateľné medzi rôzne studenými dňami
    if s["hodin_s_datami"] >= 20 and s.get("vonku") is not None and 23 - s["vonku"] > 3:
        s["kwh_na_stupen"] = round(s["kwh"] / (23 - s["vonku"]), 2)
    else:
        s["kwh_na_stupen"] = None
    if s.get(REF_ROOM) is not None:
        for r in ROOMS[1:]:
            s["rozdiel_" + r] = None if s.get(r) is None else round(s[r] - s[REF_ROOM], 2)
    return s


def local_midnight(ts=None):
    lt = time.localtime(ts if ts is not None else time.time())
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


# ---- želané teploty izieb (bod 7) ----
ROOM_NAMES = profil.room_names(P)
TARGET_ROOMS = profil.target_rooms(P)   # poradie v API
TARGET_DEFAULTS = profil.target_defaults(P)


def get_targets(c):
    """Želané minimálne teploty izieb zo state.targets (chýbajúce / neplatné hodnoty = predvolené)."""
    out = {k: dict(v) for k, v in TARGET_DEFAULTS.items()}
    try:
        st = json.loads(get_state(c, "targets", "") or "{}")
        for k in TARGET_ROOMS:
            v = float((st.get(k) or {}).get("min"))
            if 15 <= v <= 26:
                out[k]["min"] = v
    except (ValueError, TypeError, AttributeError):
        pass
    return out


# ---- zostatok OpenRouter (presunuté z app.py; volá ho web aj agent) ----
_bal = {"ts": 0, "key": "", "data": None}


def balance(cfg, force=False):
    """Zostatok kreditu na účte OpenRouter a limit kľúča (cache 5 min). Kľúč sa nikam nevypisuje."""
    key = cfg["OPENROUTER_API_KEY"]
    if not key:
        return None
    if not force and _bal["key"] == key and time.time() - _bal["ts"] < 300 and _bal["data"]:
        return _bal["data"]
    out = {"ts": int(time.time())}

    def get(p):
        req = urllib.request.Request("https://openrouter.ai/api/v1/" + p, headers={"Authorization": "Bearer " + key})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8")).get("data") or {}
    try:
        d = get("credits")
        out["total"], out["used"] = d.get("total_credits"), d.get("total_usage")
        if out["total"] is not None and out["used"] is not None:
            out["left"] = round(out["total"] - out["used"], 4)
    except Exception as e:
        out["error"] = "%s" % e
    try:
        k = get("key")
        out["key_limit"], out["key_left"] = k.get("limit"), k.get("limit_remaining")
        out["key_reset"] = k.get("limit_reset")
    except Exception:
        pass
    _bal.update(ts=time.time(), key=key, data=out)
    return out