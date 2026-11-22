#!/usr/bin/env python3
"""Webový prehľad monitoringu TČ - http://<LXC>:8099/ (iba čítanie z SQLite)."""
import json, os, sqlite3, sys, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

DB = os.environ.get("TC_DB", "/opt/tc-monitor/tc.db")
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(ROOT))   # profil.py, i18n.py sú v koreňovom priečinku aplikácie
try:
    import profil
except Exception:
    profil = None
PORT = int(os.environ.get("TC_PORT", "8099"))

# Entity zobrazované v grafoch (názov v ESP -> krátky kľúč)
SERIES = {
    "Sledovaná teplota (Detská izba)": "detska",
    "Aktuálna cieľová teplota izby": "ciel",
    "Posun ekvitermickej krivky": "posun",
    "Cieľová ekvitermická voda (WL)": "wl",
    "Výstupná teplota vody (Tw2)": "tw2",
    "Spiatočka (Tw1)": "tw1",
    "Vonkajšia teplota": "vonku",
    "Frekvencia kompresora": "freq",
    "Odhadovaný príkon TČ": "prikon",
    "Denná spotreba TČ": "spotreba",
    "Teplota TÚV (Bojler)": "bojler",
    "Chybový kód TČ": "chyba",
    "Izba Jedáleň": "r_jedalen",
    "Radiátor Jedáleň (hlavica)": "r_jedalen_rad",
    "Izba Obývačka": "r_obyvacka",
    "Izba Detská (Zigbee)": "r_detska",
    "Izba Kúpeľňa": "r_kupelna",
    "Izba Spálňa": "r_spalna",
    "Izba Pracovňa": "r_pracovna",
    "Izba Kuchyňa": "r_kuchyna",
}


def q(sql, args=()):
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def data(since):
    names = list(SERIES)
    rows = q(f"SELECT ts, entity, value FROM samples WHERE ts>=? AND entity IN ({','.join('?'*len(names))}) ORDER BY ts",
             (since, *names))
    series = {k: [] for k in SERIES.values()}
    for ts, ent, v in rows:
        series[SERIES[ent]].append([ts, v])
    # stav prepínačov kúrenia/TÚV ako krokové série z udalostí
    sw = {}
    for name, key in (("Kúrenie", "kurenie"), ("Ohrev TÚV", "tuv")):
        prev = q("SELECT new FROM events WHERE entity=? AND ts<? ORDER BY ts DESC LIMIT 1", (name, since))
        pts = [[since, 1 if prev[0][0] == "ON" else 0]] if prev else []
        pts += [[ts, 1 if new == "ON" else 0]
                for ts, new in q("SELECT ts, new FROM events WHERE entity=? AND ts>=? ORDER BY ts", (name, since))]
        sw[key] = pts
    events = q("SELECT ts, kind, entity, old, new, text FROM events WHERE ts>=? ORDER BY ts DESC LIMIT 2000", (since,))
    notes = q("SELECT ts, author, title, body FROM notes ORDER BY ts DESC LIMIT 50")
    meta = dict(q("SELECT k, v FROM meta"))
    return {"now": int(time.time()), "since": since, "series": series, "switches": sw,
            "events": [dict(zip(("ts", "kind", "entity", "old", "new", "text"), e)) for e in events],
            "notes": [dict(zip(("ts", "author", "title", "body"), n)) for n in notes],
            "meta": meta}


CTX = {"vonku": "Vonkajšia teplota", "tw2": "Výstupná teplota vody (Tw2)", "tw1": "Spiatočka (Tw1)",
       "wl": "Cieľová ekvitermická voda (WL)", "detska": "Sledovaná teplota (Detská izba)",
       "posun": "Posun ekvitermickej krivky", "freq": "Frekvencia kompresora"}
STATES = {"odu": "Režim vonkajšej jednotky", "termostat": "Termostat (požiadavka na teplo)",
          "odmraz": "Odmrazovanie", "ventil": "3-cestný ventil (Smer ohrevu)",
          "kurenie": "Kúrenie", "tuv": "Ohrev TÚV"}
SETTINGS = ["FSV 2011 Vonku najchladnejšie", "FSV 2012 Vonku najteplejšie", "FSV 2031 WL2 voda pri najchladnejšom",
            "FSV 2032 WL2 voda pri najteplejšom", "FSV 2021 WL1 voda pri najchladnejšom",
            "FSV 2022 WL1 voda pri najteplejšom", "FSV 4013 Vypnutie kúrenia nad vonk. teplotou",
            "Cieľová izbová teplota", "Teplota zapnutia kúrenia", "Maximálny posun krivky",
            "Zosilnenie posunu krivky", "Hybrid: vypnúť kúrenie pri prekročení o",
            "Minimálny chod kúrenia (min)", "Minimálna pauza kúrenia (min)", "Cieľová teplota TÚV"]


def tech(since):
    import bisect
    now = int(time.time())
    ser = {}
    for k, n in CTX.items():
        r = q("SELECT ts, value FROM samples WHERE entity=? AND ts>=? ORDER BY ts", (n, since - 3600))
        ser[k] = ([t for t, _ in r], [v for _, v in r])

    def at(k, ts):
        t, v = ser[k]
        i = bisect.bisect_right(t, ts) - 1
        return v[i] if i >= 0 and ts - t[i] < 600 else None

    stl = {}
    for k, n in STATES.items():
        r = q("SELECT ts, new FROM events WHERE entity=? ORDER BY ts", (n,))
        stl[k] = ([t for t, _ in r], [v for _, v in r])

    def st(k, ts):
        t, v = stl[k]
        i = bisect.bisect_right(t, ts) - 1
        return v[i] if i >= 0 else None

    def changes(k, a, b):
        t, v = stl[k]
        return [v[i] for i in range(len(t)) if a <= t[i] <= b]

    # prechody kompresora: presné udalosti zberača, pred nimi z minútových vzoriek
    ev = q("SELECT ts, text FROM events WHERE kind='kompresor' AND entity='Kompresor' ORDER BY ts")
    t0 = ev[0][0] if ev else now + 1
    trans = []
    ft, fv = ser["freq"]
    for i in range(1, len(ft)):
        if ft[i] >= t0:
            break
        a, b = fv[i - 1] > 0, fv[i] > 0
        if a != b:
            trans.append((ft[i], b, "1-min vzorky"))
    trans += [(t, txt.startswith("START"), "presne") for t, txt in ev]

    # stav na začiatku
    cycles, start, last_stop = [], None, None
    if ft and ft[0] < (trans[0][0] if trans else now) and fv[0] > 0:
        start = ft[0]
    for t, on, src in trans:
        if on and start is None:
            start = t
            pause = (t - last_stop) / 60 if last_stop else None
            cycles.append({"start": t, "pauza_pred": pause, "zdroj": src})
        elif not on and start is not None:
            if not cycles or cycles[-1].get("start") != start:
                cycles.append({"start": start, "pauza_pred": None, "zdroj": src})
            cycles[-1]["stop"] = t
            start, last_stop = None, t
    if start is not None:
        if not cycles or cycles[-1].get("start") != start:
            cycles.append({"start": start, "pauza_pred": None, "zdroj": "?"})
        cycles[-1]["stop"] = None

    out = []
    for c in cycles:
        s, e = c["start"], c.get("stop")
        end = e or now
        if end < since:
            continue
        fr = [v for t, v in zip(*ser["freq"]) if s <= t <= end and v > 0]
        stop_odu = st("odu", e) if e else None
        odu_during = sorted(set(changes("odu", s, end)))
        defrost = "ON" in changes("odmraz", s, end) or st("odmraz", s) == "ON"
        if e is None:
            dovod = "beží"
        elif defrost or stop_odu in ("Deice", "Split Deice"):
            dovod = "odmrazovanie"
        elif st("kurenie", e) == "OFF" and st("tuv", e) == "OFF":
            dovod = "vypnuté prepínačmi (tarifa / ručne)"
        elif st("termostat", e) == "OFF":
            dovod = "termostat OFF – voda dosiahla cieľ"
        elif stop_odu in ("Safety", "Comp Down", "Prohibit", "Recovery"):
            dovod = f"ochrana vonk. jednotky ({stop_odu})"
        else:
            dovod = "neurčený" + (f" (ODU {stop_odu})" if stop_odu else "")
        out.append({
            "start": s, "stop": e, "chod_min": round((end - s) / 60, 1),
            "pauza_pred_min": None if c["pauza_pred"] is None else round(c["pauza_pred"], 1),
            "f_max": max(fr) if fr else None, "f_avg": round(sum(fr) / len(fr), 1) if fr else None,
            "vonku": at("vonku", s), "wl": at("wl", s), "tw2_start": at("tw2", s), "tw1_start": at("tw1", s),
            "tw2_stop": at("tw2", e) if e else None, "detska": at("detska", s), "posun": at("posun", s),
            "ventil": st("ventil", s), "odu": ", ".join(o for o in odu_during if o) or stop_odu,
            "odmraz": defrost, "dovod": dovod, "zdroj": c["zdroj"]})

    # epizódy cyklovania: >= 4 štarty v kĺzavom okne 60 min
    starts = [c["start"] for c in out if c["start"] >= since]
    flags = [sum(1 for x in starts if s - 3600 < x <= s) >= 4 for s in starts]
    epis, cur = [], None
    for s, fl in zip(starts, flags):
        if fl:
            if cur and s - cur["do"] <= 3600:
                cur["do"] = s
            else:
                cur = {"od": s - 3600, "do": s}
                epis.append(cur)
    for e in epis:
        e["od"] = max(e["od"], next(x for x in starts if x >= e["od"]))
        cs = [c for c in out if e["od"] <= c["start"] <= e["do"]]
        hrs = max((e["do"] - e["od"]) / 3600, 1 / 60)
        e.update(starty=len(cs), za_hod=round(len(cs) / max(hrs, 1), 1),
                 chod_avg=round(sum(c["chod_min"] for c in cs) / len(cs), 1),
                 pauza_avg=round(sum(c["pauza_pred_min"] or 0 for c in cs) / len(cs), 1),
                 vonku=round(sum(c["vonku"] or 0 for c in cs) / len(cs), 1),
                 dovody=sorted({c["dovod"] for c in cs}))

    # hodinový prehľad
    hours = {}
    for t, v in zip(*ser["freq"]):
        if t >= since:
            h = t - t % 3600
            hours.setdefault(h, {"n": 0, "run": 0, "f": []})
            hours[h]["n"] += 1
            if v > 0:
                hours[h]["run"] += 1
                hours[h]["f"].append(v)
    hourly = []
    for h in sorted(hours):
        def avg(k):
            t, v = ser[k]
            xs = [v[i] for i in range(len(t)) if h <= t[i] < h + 3600]
            return round(sum(xs) / len(xs), 1) if xs else None
        d = hours[h]
        hourly.append({"hod": h, "beh_pct": round(d["run"] / d["n"] * 100), "starty": sum(1 for s in starts if h <= s < h + 3600),
                       "f_avg": round(sum(d["f"]) / len(d["f"]), 1) if d["f"] else 0, "vonku": avg("vonku"),
                       "wl": avg("wl"), "tw2": avg("tw2"), "tw1": avg("tw1"), "detska": avg("detska"), "posun": avg("posun")})

    sets = {}
    for n in SETTINGS:
        r = q("SELECT value FROM samples WHERE entity=? ORDER BY ts DESC LIMIT 1", (n,))
        if r:
            sets[n] = r[0][0]
    for n in ("FSV 2041 Typ krivky WL1 alebo WL2", "Režim kúrenia", "Režim izbovej regulácie"):
        r = q("SELECT new FROM events WHERE entity=? ORDER BY ts DESC LIMIT 1", (n,))
        if r:
            sets[n] = r[0][0]

    done = [c for c in out if c["stop"]]
    summ = {"cyklov": len(out), "starty": len(starts),
            "hodin": round((now - max(since, ft[0] if ft else now)) / 3600, 1),
            "chod_avg": round(sum(c["chod_min"] for c in done) / len(done), 1) if done else None,
            "chod_min": min((c["chod_min"] for c in done), default=None),
            "kratke": sum(1 for c in done if c["chod_min"] < 10),
            "odmraz": sum(1 for c in out if c["odmraz"]),
            "dovody": {d: sum(1 for c in done if c["dovod"] == d) for d in sorted({c["dovod"] for c in done})}}
    summ["za_hod"] = round(summ["starty"] / summ["hodin"], 1) if summ["hodin"] else None
    return {"now": now, "since": since, "summary": summ, "cycles": out[::-1], "episodes": epis[::-1],
            "hourly": hourly, "settings": sets}


# Porovnanie teraz vs. rovnaký čas včera: (popis, entita v samples, HA entity_id pre doplnené hodinové dáta, jednotka)
# Záložný zoznam – použije sa, ak sa profil nedá načítať. Izby sa berú z profilu (profil.json).
_COMPARE_FALLBACK = [
    ("Detská – regulačný senzor (ESPresense)", "Sledovaná teplota (Detská izba)", "sensor.detska_izba_espresense_detska_izba_detska_izba_teplota", "°C"),
    ("Detská – Zigbee (overený)", "Izba Detská (Zigbee)", "sensor.detska_t_h_temperature", "°C"),
    ("Jedáleň", "Izba Jedáleň", "sensor.0x00124b0029114db8_temperature", "°C"),
    ("Jedáleň – radiátor (hlavica)", "Radiátor Jedáleň (hlavica)", "sensor.jedalen_radiator_local_temperature", "°C"),
    ("Obývačka", "Izba Obývačka", "sensor.obyvacka_t_h_cidlo_temperature", "°C"),
    ("Kúpeľňa", "Izba Kúpeľňa", "sensor.kupelna_t_h_temperature", "°C"),
    ("Spálňa", "Izba Spálňa", "sensor.spalna_t_h_cidlo_2_kruhovy_temperature", "°C"),
    ("Pracovňa", "Izba Pracovňa", "sensor.pracovna_t_h_cidlo_temperature", "°C"),
    ("Vonku (TČ)", "Vonkajšia teplota", "m5stack_tc_vonkajsia_teplota", "°C"),
    ("Vonku (meteostanica terasa)", "-", "sensor.irimav20_temperature", "°C"),
    ("Cieľová voda WL", "Cieľová ekvitermická voda (WL)", "m5stack_tc_cielova_ekvitermicka_voda_wl", "°C"),
    ("Výstupná voda Tw2", "Výstupná teplota vody (Tw2)", "m5stack_tc_vystupna_teplota_vody_tw2", "°C"),
    ("Spiatočka Tw1", "Spiatočka (Tw1)", "m5stack_tc_spiatocka_tw1", "°C"),
    ("Posun krivky", "Posun ekvitermickej krivky", "m5stack_tc_posun_ekvitermickej_krivky", "°C"),
    ("Frekvencia kompresora", "Frekvencia kompresora", "m5stack_tc_frekvencia_kompresora", "Hz"),
    ("Príkon (odhad)", "Odhadovaný príkon TČ", "m5stack_tc_odhadovany_prikon_tc", "W"),
]


# ESP názvy izbových senzorov v záložnom zozname (tie sa nahrádzajú izbami z profilu)
_ROOM_ESP_FALLBACK = {x[1] for x in _COMPARE_FALLBACK if x[1].startswith(("Izba ", "Sledovaná teplota ("))}


def _compare_rows():
    """Riadky porovnania: izby z profilu (referenčná prvá), ostatné riadky (vonku, voda, posun, ...) ako doteraz."""
    if profil is None:
        return list(_COMPARE_FALLBACK)
    try:
        p = profil.load()
        ref = profil.reference_room(p)
        izby = sorted(p["izby"], key=lambda r: r["kluc"] != ref)   # stabilné: referenčná prvá
    except Exception:
        return list(_COMPARE_FALLBACK)
    e = p["esp"]["entity"]
    rows = []
    for r in izby:
        ha = r.get("ha") or [""]          # prázdne HA id = bez doplnenia z HA (ha_hourly ho preskočí)
        if r["kluc"] == ref:
            # regulačný senzor referenčnej izby (ESPresense) a jej ESP izbová entita (napr. Zigbee)
            if e.get("sledovana"):
                rows.append((r["nazov"] + " – regulačný senzor", e["sledovana"], ha[0], "°C"))
            if r.get("esp"):
                rows.append((r["nazov"], r["esp"], ha[1] if len(ha) > 1 else ha[0], "°C"))
        elif r.get("esp"):
            rows.append((r["nazov"], r["esp"], ha[0], "°C"))
    # riadky, ktoré nepatria k žiadnej izbe (vonku, voda, posun, frekvencia, príkon, radiátor ...) – zachované
    rows += [x for x in _COMPARE_FALLBACK if x[1] not in _ROOM_ESP_FALLBACK]
    return rows


COMPARE = _compare_rows()


def hourly_tbl():
    c = sqlite3.connect(DB, timeout=30)
    c.execute("CREATE TABLE IF NOT EXISTS hourly(entity TEXT, ts INTEGER, mean REAL, min REAL, max REAL, src TEXT, "
              "PRIMARY KEY(entity, ts))")
    c.commit(); c.close()


def ha_hourly(ha_id, a, b):
    """Hodinové priemery z HA (doplnené) prekrývajúce interval [a, b)."""
    if not ha_id:
        return []
    r = q("SELECT mean, ts FROM hourly WHERE (entity=? OR entity LIKE ?) AND ts>? AND ts<?",
          (ha_id, "%" + ha_id, a - 3600, b))
    # vážené prekrytím hodiny s intervalom (aby okrajová hodina nemala plnú váhu)
    out = []
    for m, t in r:
        if m is None:
            continue
        w = max(0, min(b, t + 3600) - max(a, t)) / 3600
        out += [m] * max(1, round(w * 12)) if w > 0 else []
    return out


def window(name, ha_id, a, b, allow_ha=True):
    """Priemer v intervale [a, b): z minútových vzoriek, ak chýbajú, z hodinových dát HA."""
    r = q("SELECT avg(value), count(*) FROM samples WHERE entity=? AND ts>=? AND ts<?", (name, a, b))[0]
    if r[1] and r[1] >= max(3, (b - a) / 60 * 0.3):
        return r[0], "ESP"
    h = ha_hourly(ha_id, a, b) if allow_ha else None
    if h:
        return sum(h) / len(h), "HA"
    if r[1]:
        return r[0], "ESP (neúplné)"
    return None, None


def compare(span_min=60):
    hourly_tbl()
    now = int(time.time())
    lt = time.localtime(now)
    midnight = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    d = 86400
    rows = []
    for label, name, ha_id, unit in COMPARE:
        t_now, s1 = window(name, ha_id, now - span_min * 60, now, allow_ha=False)
        t_y, s2 = window(name, ha_id, now - d - span_min * 60, now - d)
        day_now, s3 = window(name, ha_id, midnight, now)
        day_y, s4 = window(name, ha_id, midnight - d, now - d)
        rows.append({"label": label, "unit": unit, "teraz": t_now, "vcera": t_y, "zdroj_teraz": s1, "zdroj_vcera": s2,
                     "den_teraz": day_now, "den_vcera": day_y, "zdroj_den_vcera": s4})

    def energy_at(t):   # stav dennej spotreby (od polnoci) v čase t
        r = q("SELECT value FROM samples WHERE entity='Denná spotreba TČ' AND ts<=? AND ts>? ORDER BY ts DESC LIMIT 1",
              (t, t - 900))
        if r:
            return r[0][0], "ESP"
        # HA: stav na konci hodiny -> lineárne medzi koncom predošlej a tejto hodiny
        h = q("SELECT ts, max FROM hourly WHERE entity LIKE '%denna_spotreba%' AND ts<=? AND ts>? ORDER BY ts DESC LIMIT 1",
              (t, t - 3600))
        if not h:
            return None, None
        ts0, end = h[0]
        lt0 = time.localtime(ts0)
        prev = 0.0 if lt0.tm_hour == 0 else (q("SELECT max FROM hourly WHERE entity LIKE '%denna_spotreba%' AND ts=?",
                                                (ts0 - 3600,)) or [[None]])[0][0]
        if prev is None:
            return end, "HA"
        return prev + (end - prev) * (t - ts0) / 3600, "HA"

    def comp_stats(a, b):
        f = q("SELECT value FROM samples WHERE entity='Frekvencia kompresora' AND ts>=? AND ts<? ORDER BY ts", (a, b))
        if len(f) < max(10, (b - a) / 60 * 0.3):
            return None
        v = [x for (x,) in f]
        return {"beh": sum(1 for x in v if x > 0) / len(v) * 100,
                "starty": sum(1 for x, y in zip(v, v[1:]) if x == 0 and y > 0)}

    e_now, es1 = energy_at(now)
    e_y, es2 = energy_at(now - d)
    return {"now": now, "span_min": span_min, "rows": rows,
            "energia": {"teraz": e_now, "vcera": e_y, "zdroj_vcera": es2},
            "kompresor": {"den_teraz": comp_stats(midnight, now), "den_vcera": comp_stats(midnight - d, now - d),
                          "h_teraz": comp_stats(now - 3 * 3600, now), "h_vcera": comp_stats(now - d - 3 * 3600, now - d)}}


def overlay(name, ha_id):
    """Krivka dnes od polnoci a včera celý deň posunutá o +24 h (pre graf)."""
    now = int(time.time())
    lt = time.localtime(now)
    midnight = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    today = q("SELECT ts, value FROM samples WHERE entity=? AND ts>=? ORDER BY ts", (name, midnight))
    yest = q("SELECT ts, value FROM samples WHERE entity=? AND ts>=? AND ts<? ORDER BY ts", (name, midnight - 86400, midnight))
    yh = {} if not ha_id else {t: m for m, t in q("SELECT mean, ts FROM hourly WHERE (entity=? OR entity LIKE ?) AND ts>=? AND ts<?",
                              (ha_id, "%" + ha_id, midnight - 86400, midnight))}
    # minútové vzorky zriedime na 5 min, chýbajúce hodiny doplníme z HA
    ys, have = [], set()
    for t, v in yest:
        if t % 300 < 60:
            ys.append([t + 86400, v]); have.add(t - t % 3600)
    for t, m in yh.items():
        if t not in have and m is not None:
            ys.append([t + 1800 + 86400, m])
    ys.sort()
    return {"dnes": [[t, v] for t, v in today if t % 300 < 60], "vcera": ys}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(ROOT, "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/api/data":
            hours = float(parse_qs(u.query).get("h", ["24"])[0])
            since = int(time.time() - hours * 3600) if hours > 0 else 0
            return self.send(200, json.dumps(data(since), ensure_ascii=False), "application/json; charset=utf-8")
        if u.path == "/api/compare":
            p = parse_qs(u.query)
            out = compare(int(p.get("m", ["60"])[0]))
            k = int(p.get("k", ["1"])[0])
            k = k if 0 <= k < len(COMPARE) else 1
            out["overlay"] = overlay(COMPARE[k][1], COMPARE[k][2]); out["overlay_label"] = COMPARE[k][0]
            out["labels"] = [x[0] for x in COMPARE]
            return self.send(200, json.dumps(out, ensure_ascii=False), "application/json; charset=utf-8")
        if u.path == "/api/tech":
            hours = float(parse_qs(u.query).get("h", ["24"])[0])
            since = int(time.time() - hours * 3600) if hours > 0 else 0
            return self.send(200, json.dumps(tech(since), ensure_ascii=False), "application/json; charset=utf-8")
        self.send(404, "not found", "text/plain")


class _Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # Odpojenie klienta (zavretý prehliadač) nie je chyba servera - bez tracebacku v logu
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


if __name__ == "__main__":
    _Server(("0.0.0.0", PORT), H).serve_forever()
