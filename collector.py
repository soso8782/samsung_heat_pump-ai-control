#!/usr/bin/env python3
"""Zberač dát z ESP m5stack-tc (Samsung TČ) - iba čítanie.

Číta Server-Sent Events z webového servera ESPHome (/events):
  - 'state' udalosti -> aktuálny stav všetkých entít
  - 'log' udalosti   -> riadky regulácie, tarify, chyby
Každých SAMPLE_S sekúnd uloží všetky číselné hodnoty do SQLite (tabuľka samples).
Zmeny prepínačov, režimov, nastavení a textových stavov ukladá ako udalosti (tabuľka events).
"""
import json, os, re, sqlite3, threading, time, urllib.request

try:
    import profil
    from common import load_cfg
except ImportError:   # zberač spustený samostatne bez ostatných modulov: predvolený dom
    profil = None
    load_cfg = None
try:
    import i18n
except ImportError:
    i18n = None


def _loc(text):
    """Text udalosti v jazyku domácnosti (UI_LANG v config.env); bez i18n alebo pri chybe ostáva slovensky.
    Staré slovenské riadky v DB sa pri zobrazení prekladajú (i18n.event_text)."""
    try:
        if i18n is not None and load_cfg is not None:
            return i18n.event_text(text, i18n.home_lang(load_cfg()))
    except Exception:
        pass
    return text


def _esp_url():
    """Adresa ESP: premenná TC_ESP, inak ESP_URL z config.env (cez common.load_cfg), inak doterajšia predvolená."""
    if os.environ.get("TC_ESP"):
        return os.environ["TC_ESP"]
    try:
        if load_cfg:
            u = (load_cfg().get("ESP_URL") or "").strip().rstrip("/")
            if u:
                return u
    except Exception:
        pass
    return "http://192.168.1.199"


ESP = _esp_url()
DB = os.environ.get("TC_DB", "/opt/tc-monitor/tc.db")
SAMPLE_S = int(os.environ.get("TC_SAMPLE_S", "60"))
END = float(os.environ.get("TC_END", "0"))  # unix čas konca zberu, 0 = bez konca

ANSI = re.compile(r"\x1b\[[0-9;]*m")
LOG_KEEP = re.compile(r"\[(izba|tarifa)[:\]]|\]\[E\]\[|\]\[W\]\[(izba|tarifa|api)")
LOG_SKIP = re.compile(r"Packet exceeds size limits|node-red")
EVENT_DOMAINS = {"switch", "select", "text_sensor", "number", "datetime", "binary_sensor", "button"}


def db():
    c = sqlite3.connect(DB, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS samples(ts INTEGER, entity TEXT, value REAL);
    CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts);
    CREATE TABLE IF NOT EXISTS events(ts INTEGER, kind TEXT, entity TEXT, old TEXT, new TEXT, text TEXT);
    CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
    CREATE TABLE IF NOT EXISTS notes(ts INTEGER, author TEXT, title TEXT, body TEXT);
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    """)
    return c


state = {}          # name -> (domain, value, state_text)
lock = threading.Lock()
last_state = [0.0]  # čas poslednej 'state' udalosti (strážnik zamrznutého streamu)
STALL_S = 120


TECH = ("Odmrazovanie", "Termostat (požiadavka na teplo)", "Režim vonkajšej jednotky")
comp = {"on": None, "since": None, "max": 0.0}   # sledovanie cyklu kompresora


def sv(name):
    v = state.get(name)
    return v[1] if v else None


def ctx():
    def f(n, unit="°C"):
        v = sv(n)
        return f"{v:.1f} {unit}" if isinstance(v, (int, float)) else "?"
    return (f"vonku {f('Vonkajšia teplota')}, Tw2 {f('Výstupná teplota vody (Tw2)')}, "
            f"Tw1 {f('Spiatočka (Tw1)')}, WL {f('Cieľová ekvitermická voda (WL)')}, "
            f"ventil {sv('3-cestný ventil (Smer ohrevu)')}, ODU {sv('Režim vonkajšej jednotky')}, "
            f"termostat {'ON' if sv('Termostat (požiadavka na teplo)') else 'OFF'}, "
            f"odmraz. {'ON' if sv('Odmrazovanie') else 'OFF'}")


def on_freq(c, val):
    if not isinstance(val, (int, float)):
        return
    now = int(time.time())
    running = val > 0
    if comp["on"] is None:          # prvé čítanie po štarte zberača - bez udalosti
        comp.update(on=running, since=now, max=val)
        return
    if running:
        comp["max"] = max(comp["max"], val)
    if running == comp["on"]:
        return
    dur = (now - comp["since"]) / 60
    if running:
        txt = f"START (pauza {dur:.0f} min) - {ctx()}"
        comp.update(on=True, since=now, max=val)
    else:
        txt = f"STOP (chod {dur:.0f} min, max {comp['max']:.0f} Hz) - {ctx()}"
        comp.update(on=False, since=now, max=0.0)
    c.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (now, "kompresor", "Kompresor", None, None, _loc(txt)))
    c.commit()


def kind_for(domain, name):
    if name in TECH:
        return "kompresor"
    if name.startswith("Stav izbovej") or name.startswith("Záložné čidlo"):
        return "regulacia"
    if domain in ("switch", "select") or name.startswith("3-cestný"):
        return "ovladanie"
    if domain in ("number", "datetime"):
        return "regulacia" if name.startswith("Posun ekvitermickej") else "nastavenie"
    return "stav"


id_map = {}   # SSE id -> (name, domain); aktualizácie po úvodnom výpise nesú len id


def on_state(c, d):
    name, domain, eid = d.get("name"), d.get("domain"), d.get("id")
    if name and eid:
        id_map[eid] = (name, domain)
    elif eid in id_map:
        name, domain = id_map[eid]
    if not name:
        return
    if not domain and eid:
        domain = eid.split("-", 1)[0]
    val, txt = d.get("value"), d.get("state")
    if domain == "number" and isinstance(val, str):   # number posiela hodnotu ako text ("17", "-1.0")
        try:
            val = float(val)
        except ValueError:
            val = None
    with lock:
        prev = state.get(name)
        state[name] = (domain, val, txt)
    if name == "Frekvencia kompresora":
        on_freq(c, val)
    if prev is None and (domain == "switch" or name in TECH or name.startswith("3-cestný")):
        # počiatočný stav (pre graf a technickú správu)
        c.execute("INSERT INTO events VALUES(?,?,?,?,?,?)",
                  (int(time.time()), "stav", name, None, txt or str(val), None))
        c.commit()
    if domain in EVENT_DOMAINS and prev is not None and prev[1] != val:
        old = prev[2] if prev[2] not in (None, "") else str(prev[1])
        new = txt if txt not in (None, "") else str(val)
        c.execute("INSERT INTO events VALUES(?,?,?,?,?,?)",
                  (int(time.time()), kind_for(domain, name), name, old, new, None))
        c.commit()
    if name == "Chybový kód TČ" and prev is not None and prev[1] != val and val not in (0, None):
        c.execute("INSERT INTO events VALUES(?,?,?,?,?,?)",
                  (int(time.time()), "chyba", name, str(prev[1]), str(val), _loc("Nenulový chybový kód TČ")))
        c.commit()


def on_log(c, line):
    line = ANSI.sub("", line).strip()
    if not LOG_KEEP.search(line) or LOG_SKIP.search(line):
        return
    m = re.search(r"\[([IWE])\]\[([a-z_\.]+)(?::\d+)?\]:\s*(.*)", line)
    lvl, tag, msg = (m.group(1), m.group(2), m.group(3)) if m else ("I", "log", line)
    kind = {"izba": "regulacia", "tarifa": "tarifa"}.get(tag, "log")
    if lvl == "E":
        kind = "chyba"
    c.execute("INSERT INTO events VALUES(?,?,?,?,?,?)",
              (int(time.time()), kind, tag, None, None, f"[{lvl}] {msg}"))
    c.commit()


def sampler():
    c = db()
    while not END or time.time() < END:
        time.sleep(SAMPLE_S - (time.time() % SAMPLE_S))
        ts = int(time.time())
        if ts - last_state[0] > STALL_S:   # bez čerstvých dát neukladať staré hodnoty
            continue
        with lock:
            rows = [(ts, n, float(v)) for n, (dom, v, _) in state.items()
                    if dom in ("sensor", "number") and isinstance(v, (int, float)) and v == v]
        if rows:
            c.executemany("INSERT INTO samples VALUES(?,?,?)", rows)
            c.execute("INSERT OR REPLACE INTO meta VALUES('last_sample',?)", (str(ts),))
            c.commit()


def stream():
    c = db()
    c.execute("INSERT INTO events VALUES(?,?,?,?,?,?)",
              (int(time.time()), "system", "zberač", None, None, _loc("Zberač spustený")))
    c.commit()
    online = None
    while not END or time.time() < END:
        try:
            req = urllib.request.Request(ESP + "/events", headers={"Accept": "text/event-stream"})
            with urllib.request.urlopen(req, timeout=90) as r:
                if online is False:
                    c.execute("INSERT INTO events VALUES(?,?,?,?,?,?)",
                              (int(time.time()), "system", "ESP", None, None, _loc("ESP znova dostupné")))
                    c.commit()
                online, ev = True, None
                # Trvalé spojenie slúži hlavne na logy - ESP po úvodnom výpise
                # zmeny hodnôt neposiela; tie zabezpečuje snapshots() každých 30 s.
                for raw in r:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line.startswith("event:"):
                        ev = line[6:].strip()
                    elif line.startswith("data:") and ev in ("state", "log"):
                        data = line[5:].strip()
                        if ev == "state":
                            last_state[0] = time.time()
                            try:
                                on_state(c, json.loads(data))
                            except ValueError:
                                pass
                        else:
                            on_log(c, data)
                    if END and time.time() >= END:
                        break
        except Exception as e:  # výpadok ESP / siete
            if online is not False:
                c.execute("INSERT INTO events VALUES(?,?,?,?,?,?)",
                          (int(time.time()), "system", "ESP", None, None, _loc(f"ESP nedostupné ({type(e).__name__})")))
                c.commit()
            online = False
            time.sleep(10)
    c.execute("INSERT INTO events VALUES(?,?,?,?,?,?)",
              (int(time.time()), "system", "zberač", None, None, _loc("Zber ukončený (koniec 48 h)")))
    c.commit()


SNAP_S = 30

# Entity, ktoré ESP v /events neposiela (homeassistant senzory) - čítajú sa cez REST
if profil is not None:
    _P = profil.load()
    REST_POLL = [r["esp"] for r in _P["izby"] if r["esp"]] + list(_P["esp"].get("rest_extra") or [])
else:
    REST_POLL = ["Izba Kuchyňa", "Izba Jedáleň", "Radiátor Jedáleň (hlavica)", "Izba Obývačka", "Izba Detská (Zigbee)",
                 "Izba Kúpeľňa", "Izba Spálňa", "Izba Pracovňa"]


def rest_poll():
    import urllib.parse
    c = db()
    while not END or time.time() < END:
        t0 = time.time()
        for n in REST_POLL:
            if time.time() - mqtt_ts.get(n, 0) < MQTT_ROOM_HOLD:
                continue          # čerstvá hodnota priamo z MQTT má prednosť pred kópiou v ESP
            try:
                with urllib.request.urlopen(f"{ESP}/sensor/{urllib.parse.quote(n)}", timeout=5) as r:
                    d = json.loads(r.read().decode("utf-8"))
                d.setdefault("name", n); d.setdefault("domain", "sensor")
                on_state(c, d)
            except Exception:
                pass
        time.sleep(max(1.0, 60 - (time.time() - t0)))


# --- Izbové teploty priamo zo Zigbee2MQTT (voliteľné: MQTT_HOST v config.env, ako pri okenných kontaktoch) ---
# Cesta cez HA -> ESP -> REST trvá 1-2 min; správa z MQTT sa zapíše hneď (stav pre ďalšiu vzorku + meta `live:<izba>`,
# ktorú web ukáže okamžite). Párovanie: HA entita izby `sensor.<slug>_temperature` <-> z2m meno so slugom <slug>.
MQTT_ROOM_HOLD = 300       # s: po správe z MQTT REST z ESP túto izbu neprepisuje
MQTT_RECONNECT_S = 600     # s: po tomto čase sa spojenie obnoví (nová konfigurácia, nové párovanie izieb)
mqtt_ts = {}               # ESP názov izby -> čas poslednej hodnoty z MQTT


def mqtt_room_map(p=None):
    """slug z2m mena -> ESP názov izby (z profilu: izby s `esp` a HA entitou `sensor.<slug>_temperature`)."""
    if p is None:
        p = profil.load() if profil is not None else None
    m = {}
    for r in (p or {}).get("izby") or []:
        if not r.get("esp"):
            continue
        for e in r.get("ha") or []:
            if e.startswith("sensor.") and e.endswith("_temperature"):
                m[e[len("sensor."):-len("_temperature")]] = r["esp"]
    return m


def mqtt_temp(payload):
    """JSON správa z2m -> teplota (°C) alebo None."""
    try:
        d = json.loads(payload.decode("utf-8", "replace"))
    except ValueError:
        return None
    t = d.get("temperature") if isinstance(d, dict) else None
    if isinstance(t, bool) or not isinstance(t, (int, float)) or not -40 <= t <= 60:
        return None
    return float(t)


def mqtt_room(c, rmap, base, topic, payload, retain=False, now=None):
    """Spracuje jednu správu. Vracia ESP názov izby, ak sa zapísala teplota, inak None. Retained správy sa ignorujú (môžu byť staré)."""
    import contacts_mqtt
    now = int(now or time.time())
    if retain or not topic.startswith(base + "/"):
        return None
    name = topic[len(base) + 1:]
    if "/" in name:
        return None
    esp = rmap.get(contacts_mqtt.slug(name))
    t = mqtt_temp(payload) if esp else None
    if t is None:
        return None
    on_state(c, {"name": esp, "domain": "sensor", "value": t, "state": "%.1f °C" % t})
    mqtt_ts[esp] = now
    c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("live:" + esp, json.dumps({"v": t, "ts": now})))
    c.commit()
    return esp


def mqtt_rooms_once(cfg, rmap):
    import socket
    import contacts_mqtt as cm
    host, user, pw = cfg.get("MQTT_HOST", "").strip(), cfg.get("MQTT_USER", "").strip(), cfg.get("MQTT_PASS", "")
    try:
        port = int(cfg.get("MQTT_PORT") or 1883)
    except ValueError:
        port = 1883
    base = (cfg.get("MQTT_BASE") or "zigbee2mqtt").strip().strip("/")
    sock = socket.create_connection((host, port), timeout=10)
    c = db()
    try:
        sock.sendall(cm.build_connect("kurenie-izby-%d" % (int(time.time()) % 100000), user, pw))
        t, _, body = cm.read_packet(sock)
        if t != 2 or len(body) < 2 or body[1] != 0:
            raise ConnectionError("broker odmietol pripojenie (kód %s)" % (body[1] if len(body) > 1 else "?"))
        sock.sendall(cm.build_subscribe(1, base + "/+"))
        sock.settimeout(cm.READ_TIMEOUT)
        t0 = last_rx = last_ping = time.time()
        while (not END or time.time() < END) and time.time() - t0 < MQTT_RECONNECT_S:
            try:
                t, flags, body = cm.read_packet(sock)
                last_rx = time.time()
                if t == 3:
                    topic, payload, retain = cm.parse_publish(flags, body)
                    mqtt_room(c, rmap, base, topic, payload, retain)
            except socket.timeout:
                pass
            now = time.time()
            if now - last_rx > cm.DEAD_AFTER:
                raise ConnectionError("broker mlčí")
            if now - last_ping >= cm.KEEPALIVE / 2:
                sock.sendall(b"\xc0\x00")
                last_ping = now
    finally:
        try:
            c.close()
        finally:
            sock.close()


def mqtt_rooms_loop():
    """Daemon vlákno. Bez MQTT_HOST alebo bez párovateľných izieb čaká a skúša znova; pri chybe pauza 5 -> 60 s. Heslo sa nevypisuje."""
    if load_cfg is None or profil is None:
        return
    time.sleep(5)
    delay = 5
    while not END or time.time() < END:
        t0 = time.time()
        try:
            cfg, rmap = load_cfg(), mqtt_room_map()
            if not cfg.get("MQTT_HOST", "").strip() or not rmap:
                time.sleep(60)
                continue
            mqtt_rooms_once(cfg, rmap)
            delay = 5
            continue
        except Exception as e:
            print(time.strftime("%F %T"), "mqtt izby: %s: %s" % (type(e).__name__, str(e)[:120]), flush=True)
            delay = 5 if time.time() - t0 > 120 else min(60, delay * 2)
        time.sleep(delay)


def snapshots():
    """Každých SNAP_S s nové krátke pripojenie na /events: ESP pri pripojení pošle
    aktuálny stav všetkých entít. Po prvom 'ping' za výpisom sa spojenie zavrie."""
    c = db()
    while not END or time.time() < END:
        t0 = time.time()
        n = 0
        try:
            req = urllib.request.Request(ESP + "/events", headers={"Accept": "text/event-stream"})
            with urllib.request.urlopen(req, timeout=15) as r:
                ev = None
                for raw in r:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line.startswith("event:"):
                        ev = line[6:].strip()
                        if ev == "ping" and n > 0:
                            break
                    elif line.startswith("data:") and ev == "state":
                        try:
                            on_state(c, json.loads(line[5:].strip()))
                            n += 1
                        except ValueError:
                            pass
                    if time.time() - t0 > 14:
                        break
        except Exception:
            pass
        if n:
            last_state[0] = time.time()
        time.sleep(max(1.0, SNAP_S - (time.time() - t0)))


if __name__ == "__main__":
    c = db()
    if END:
        c.execute("INSERT OR REPLACE INTO meta VALUES('end',?)", (str(int(END)),))
    c.execute("INSERT OR IGNORE INTO meta VALUES('start',?)", (str(int(time.time())),))
    c.commit()
    threading.Thread(target=sampler, daemon=True).start()
    threading.Thread(target=snapshots, daemon=True).start()
    threading.Thread(target=rest_poll, daemon=True).start()
    threading.Thread(target=mqtt_rooms_loop, name="mqtt-izby", daemon=True).start()
    stream()
