#!/usr/bin/env python3
"""Entity pre Home Assistant cez MQTT discovery: Preč z domu, nočný útlm, tarifa, zdravie čerpadla, náklady a verzia (+ príkazy z HA).

Beží ako jedno daemon vlákno AGENTA (vedľa ha_pub): agent je jediný proces, ktorý beží stále a už drží utlm.tick, takže príkaz z HA sa
prejaví hneď a nezávisí od webu. Jedno dlhé spojenie s brokerom (reconnect 5 -> 60 s, last will „offline“); stav sa posiela raz za 30 s
a hneď po príkaze. Príkazy: switch „Preč z domu“ (ON/OFF) a selecty režimov (off/advise/control) sa vykonajú cez tie isté funkcie ako web
(utlm.away_start / away_cancel, utlm.MODES + state), takže platia rovnaké poistky (A/B test, režim „Preč“ vypnutý…). Príkazy sa prijímajú
len pri MQTT_OVLADANIE=1 (predvolene 0: ovládacie prvky sú iba senzory), nikdy pri DRY_RUN=1 a retained správy sa ignorujú.
MQTT_PUB=0 = nič sa neposiela. Hodnota „kto“ v logu: „Home Assistant (MQTT)“. Iba štandardná knižnica."""
import datetime as _dt, json, socket, struct, threading, time, traceback

import contacts_mqtt as cm
import i18n
import ha_pub
import utlm
from common import adb, is_dry, load_cfg, set_state
from i18n import tr

WHO = "Home Assistant (MQTT)"
BASE = "kurenie/tc/ent"
STATE_TOPIC = BASE + "/state"
AVAIL_TOPIC = BASE + "/avail"
CMD = {"away": BASE + "/cmd/away", "utlm_mode": BASE + "/cmd/utlm_mode", "prec_mode": BASE + "/cmd/prec_mode"}
MODE_KEY = {"utlm_mode": "utlm_mode", "prec_mode": "prec_mode"}      # kľúč príkazu -> kľúč v state
STATE_EVERY = 30
SLOW_EVERY = 1800            # s: náklady a verzia (siete, najviac raz za 30 min)
HEALTH_EVERY = 600
CFG_EVERY = 30
KEEPALIVE = 60
DEAD_AFTER = 100
PHASES = ("norm", "pred", "vt")
UTLM_STATES = ("normal", "night", "ramp", "away")
HEALTH = ("ok", "warn", "crit", "nodata")
# (kľúč, doména pri MQTT_OVLADANIE=0, doména pri =1, doplnok discovery; názov je v locales srv.ha_ent.n_<kľúč>)
ENTITIES = (
    ("away", "binary_sensor", "switch", {"icon": "mdi:airplane-takeoff"}),
    ("utlm_mode", "sensor", "select", {"icon": "mdi:weather-night", "options": list(utlm.MODES)}),
    ("prec_mode", "sensor", "select", {"icon": "mdi:home-export-outline", "options": list(utlm.MODES)}),
    ("utlm_state", "sensor", "sensor", {"icon": "mdi:weather-night", "device_class": "enum", "options": list(UTLM_STATES)}),
    ("utlm_next", "sensor", "sensor", {"device_class": "timestamp", "icon": "mdi:clock-outline"}),
    ("tarifa_faza", "sensor", "sensor", {"icon": "mdi:cash-clock", "device_class": "enum", "options": list(PHASES)}),
    ("tarifa_posun", "sensor", "sensor", {"unit_of_measurement": "°C", "icon": "mdi:thermometer-plus", "state_class": "measurement"}),
    ("zdravie", "sensor", "sensor", {"icon": "mdi:heart-pulse", "device_class": "enum", "options": list(HEALTH), "attrs": True}),
    ("zdravie_problem", "binary_sensor", "binary_sensor", {"device_class": "problem"}),
    ("naklady_mesiac", "sensor", "sensor", {"unit_of_measurement": "€", "device_class": "monetary", "icon": "mdi:cash", "suggested_display_precision": 2}),
    ("verzia", "sensor", "sensor", {"icon": "mdi:tag-outline", "entity_category": "diagnostic"}),
    ("update", "binary_sensor", "binary_sensor", {"device_class": "update", "entity_category": "diagnostic"}),
)


def control_on(cfg):
    return str(cfg.get("MQTT_OVLADANIE", "0")).strip() == "1"


def enabled(cfg):
    """Publikovanie: je broker a MQTT_PUB nie je 0 (DRY_RUN stav neblokuje, blokuje len príkazy)."""
    return bool(str(cfg.get("MQTT_HOST", "")).strip()) and str(cfg.get("MQTT_PUB", "1")).strip() != "0"


def _domain(key, ctl):
    for k, d0, d1, _x in ENTITIES:
        if k == key:
            return d1 if ctl else d0
    raise KeyError(key)


def discovery(cfg, lang="sk"):
    """Zoznam (topic, payload, retain) discovery správ. Pri zmene domény (MQTT_OVLADANIE 0 <-> 1) sa stará retained konfigurácia vymaže
    prázdnou správou, aby v HA neostala duplicitná entita."""
    ctl = control_on(cfg)
    dev = ha_pub.device_block(lang)
    out = []
    for key, d0, d1, extra in ENTITIES:
        dom = d1 if ctl else d0
        extra = dict(extra)
        attrs = extra.pop("attrs", False)
        d = {"name": tr("srv.ha_ent.n_" + key, lang), "unique_id": "kurenie_tc_" + key, "object_id": "kurenie_tc_" + key,
             "device": dev, "availability_topic": AVAIL_TOPIC, "state_topic": STATE_TOPIC}
        if dom in ("switch", "binary_sensor"):
            tpl = "{{ value_json.%s }}" % key
            d["value_template"] = tpl
            if dom == "switch":
                d.update(command_topic=CMD["away"], payload_on="ON", payload_off="OFF", state_on="ON", state_off="OFF")
            else:
                d.update(payload_on="ON", payload_off="OFF")
        else:
            d["value_template"] = "{{ value_json.%s if value_json.%s is not none else 'unknown' }}" % (key, key)
            if dom == "select":
                d["command_topic"] = CMD[key]
        d.update(extra)
        if dom == "sensor" and key in MODE_KEY:           # iba na čítanie: enum senzor
            d["device_class"] = "enum"
        if attrs:
            d["json_attributes_topic"] = STATE_TOPIC
            d["json_attributes_template"] = "{{ value_json.zdravie_attr | tojson }}"
        out.append(("%s/%s/kurenie_tc/%s/config" % (ha_pub.DISC_PREFIX, dom, key), json.dumps(d, ensure_ascii=False), True))
        if d0 != d1:
            other = d0 if ctl else d1
            out.append(("%s/%s/kurenie_tc/%s/config" % (ha_pub.DISC_PREFIX, other, key), "", True))
    return out


def _iso(ts):
    return _dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds") if ts else None


def collect_slow(cfg, mem, now, lang="sk"):
    """Náklady (naklady.odhad), verzia (verzia.info) a zdravie; každé s vlastným intervalom, chyba = ponechá sa stará hodnota."""
    if now - mem.get("slow_ts", 0) >= SLOW_EVERY or "slow" not in mem:
        mem["slow_ts"] = now
        slow = mem.setdefault("slow", {})
        try:
            import naklady
            o = naklady.odhad(cfg, now=now, lang=lang)
            m = (o or {}).get("mesiac") or {}
            slow["naklady_mesiac"] = m.get("spolu_eur") if (o or {}).get("dost_dat") else None
        except Exception as e:
            print("ha_ent: naklady: %s: %s" % (type(e).__name__, e), flush=True)
        try:
            import verzia
            v = verzia.info(cfg, None, now=now)
            slow["verzia"], slow["update"] = v.get("app"), "ON" if v.get("novsia") else "OFF"
        except Exception as e:
            print("ha_ent: verzia: %s: %s" % (type(e).__name__, e), flush=True)
    if now - mem.get("health_ts", 0) >= HEALTH_EVERY or "health" not in mem:
        mem["health_ts"] = now
        try:
            import zdravie
            z = zdravie.api(cfg, lang, now)
            st = z.get("stav") if z.get("enabled") else "nodata"
            st = st if st in HEALTH else "nodata"
            mem["health"] = {"zdravie": st, "zdravie_problem": "ON" if st in ("warn", "crit") else "OFF",
                             "zdravie_attr": {"indikatory": [{"kod": i.get("kod"), "nazov": i.get("nazov"), "stav": i.get("stav"), "zmena_pct": i.get("zmena_pct")}
                                                             for i in z.get("indikatory", [])],
                                              "chyby": (z.get("chyby") or {}).get("kody", []),
                                              "nalezy": [n.get("text") for n in (z.get("nalezy") or []) if isinstance(n, dict) and n.get("text")][:10]}}
        except Exception as e:
            print("ha_ent: zdravie: %s: %s" % (type(e).__name__, e), flush=True)
    out = dict(mem.get("slow") or {})
    out.update(mem.get("health") or {})
    return out


def build_state(cfg, c, mem, now=None, lang="sk"):
    """Stavová správa (slovník). Chýbajúca hodnota = None."""
    now = int(now or time.time())
    u = utlm.status_block(cfg, c, now, admin=False)
    st = u.get("state")
    out = {"away": "ON" if u.get("away") else "OFF", "utlm_mode": u.get("mode"), "prec_mode": u.get("prec_mode"),
           "utlm_state": st if st in UTLM_STATES else None, "utlm_next": _iso(u.get("next_change")),
           "tarifa_faza": None, "tarifa_posun": None, "zdravie": "nodata", "zdravie_problem": "OFF", "zdravie_attr": {},
           "naklady_mesiac": None, "verzia": None, "update": "OFF"}
    try:
        import tarifa
        t = tarifa.status(cfg, c, now)
        out["tarifa_faza"] = t.get("phase") if t.get("phase") in PHASES else None
        off = t.get("offset")
        out["tarifa_posun"] = off.get("value") if isinstance(off, dict) else 0.0
    except Exception as e:
        print("ha_ent: tarifa: %s: %s" % (type(e).__name__, e), flush=True)
    out.update(collect_slow(cfg, mem, now, lang))
    return out


def handle_command(cfg, c, topic, payload, retain=False, now=None):
    """Spracuje príkaz z HA. Vracia text výsledku: "applied", "ignored:<dôvod>" alebo "refused:<dôvod>". Nikdy nevyhodí výnimku z platnosti vstupu."""
    now = int(now or time.time())
    hl = i18n.home_lang(cfg)
    key = next((k for k, t in CMD.items() if t == topic), None)
    if key is None:
        return "ignored:topic"
    if retain:
        return "ignored:retain"          # zastaraný príkaz po novom pripojení sa nikdy nevykoná
    if not control_on(cfg):
        return "ignored:ovladanie"
    try:
        val = payload.decode("utf-8").strip()
    except UnicodeDecodeError:
        return "ignored:payload"
    if len(val) > 16:
        return "ignored:payload"
    if key == "away":
        val = val.upper()
        if val not in ("ON", "OFF"):
            return "ignored:payload"
    elif val not in utlm.MODES:
        return "ignored:payload"
    if is_dry(cfg):
        utlm._log(c, cfg, tr("srv.ha_ent.log_dry", hl, who=WHO, cmd="%s=%s" % (key, val)))
        return "ignored:dry"
    if key == "away":
        if val == "ON":
            if utlm.load_away(c):
                return "applied"
            ok, text = utlm.away_start(cfg, c, None, now, "manual", hl)
            if not ok:
                utlm._log(c, cfg, tr("srv.ha_ent.log_odmietnute", hl, who=WHO, cmd=key, why=text))
                return "refused:" + text
            utlm._log(c, cfg, tr("srv.ha_ent.log_away_on", hl, who=WHO))
        else:
            if not utlm.load_away(c):
                return "applied"
            utlm.away_cancel(c)
            utlm._log(c, cfg, tr("srv.ha_ent.log_away_off", hl, who=WHO))
    else:
        set_state(c, MODE_KEY[key], val)
        utlm._log(c, cfg, tr("srv.ha_ent.log_rezim", hl, who=WHO, what=tr("srv.ha_ent.n_" + key, hl), value=val))
    try:
        utlm.tick(cfg, c, now)           # zmena sa prejaví hneď (ako pri webe)
    except Exception:
        pass
    return "applied"


# ---------------------------------------------------------------- MQTT
def build_connect_will(client_id, user, password, will_topic, will_msg=b"offline"):
    flags = 0x02 | 0x04 | 0x20 | (0x80 if user else 0) | (0x40 if user and password else 0)      # clean, will, will retain
    body = cm._str("MQTT") + bytes([4, flags]) + struct.pack(">H", KEEPALIVE) + cm._str(client_id) + cm._str(will_topic) + struct.pack(">H", len(will_msg)) + will_msg
    if user:
        body += cm._str(user)
        if password:
            body += cm._str(password)
    return b"\x10" + cm._enc_len(len(body)) + body


def _conn_sig(cfg):
    return tuple(str(cfg.get(k, "")) for k in ("MQTT_HOST", "MQTT_PORT", "MQTT_USER", "MQTT_PASS", "MQTT_OVLADANIE", "MQTT_PUB", "UI_LANG"))


def publish_state(sock, cfg, c, mem, now=None):
    p = build_state(cfg, c, mem, now, i18n.home_lang(cfg))
    sock.sendall(ha_pub.build_publish(STATE_TOPIC, json.dumps(p, ensure_ascii=False), retain=True))
    return p


def run_once(cfg, stop=None, connect=None):
    """Jedno spojenie: CONNECT (+will), subscribe príkazov (iba pri ovládaní), discovery, stav a slučka. Skončí pri zmene konfigurácie."""
    host, user, pw = cfg.get("MQTT_HOST", "").strip(), cfg.get("MQTT_USER", "").strip(), cfg.get("MQTT_PASS", "")
    try:
        port = int(cfg.get("MQTT_PORT") or 1883)
    except ValueError:
        port = 1883
    sig = _conn_sig(cfg)
    sock = (connect or socket.create_connection)((host, port), timeout=10)
    c = adb(cfg)
    mem = {}
    try:
        sock.settimeout(10)
        sock.sendall(build_connect_will("kurenie-ent-%d" % (int(time.time()) % 100000), user, pw, AVAIL_TOPIC))
        t, _, body = cm.read_packet(sock)
        if t != 2 or len(body) < 2 or body[1] != 0:
            raise ConnectionError("broker odmietol pripojenie (kód %s)" % (body[1] if len(body) > 1 else "?"))
        if control_on(cfg):
            for i, topic in enumerate(CMD.values(), 1):
                sock.sendall(cm.build_subscribe(i, topic))
        for topic, data, retain in discovery(cfg, i18n.home_lang(cfg)):
            sock.sendall(ha_pub.build_publish(topic, data, retain=retain))
        sock.sendall(ha_pub.build_publish(AVAIL_TOPIC, "online", retain=True))
        publish_state(sock, cfg, c, mem)
        sock.settimeout(5)
        last_rx = last_ping = last_state = last_cfg = time.time()
        while not (stop and stop.is_set()):
            send_now = False
            try:
                t, flags, body = cm.read_packet(sock)
                last_rx = time.time()
                if t == 3:
                    topic, data, retain = cm.parse_publish(flags, body)
                    res = handle_command(cfg, c, topic, data, retain)
                    if res != "applied" and not res.startswith("ignored:topic"):
                        print("ha_ent: príkaz %s: %s" % (topic, res), flush=True)
                    send_now = res == "applied"
            except socket.timeout:
                pass
            now = time.time()
            if now - last_cfg >= CFG_EVERY:
                last_cfg = now
                new = load_cfg()
                if not enabled(new) or _conn_sig(new) != sig:
                    sock.sendall(ha_pub.build_publish(AVAIL_TOPIC, "offline", retain=True))
                    return
                cfg.update(new)
            if send_now or now - last_state >= STATE_EVERY:
                last_state = now
                publish_state(sock, cfg, c, mem)
            if now - last_rx > DEAD_AFTER:
                raise ConnectionError("broker mlčí")
            if now - last_ping >= KEEPALIVE / 2:
                sock.sendall(b"\xc0\x00")
                last_ping = now
        sock.sendall(ha_pub.build_publish(AVAIL_TOPIC, "offline", retain=True))
    finally:
        c.close()
        sock.close()


def ent_loop():
    """Daemon vlákno agenta; chyba nikdy nezhodí agenta (pauza 5 -> 60 s, hlási sa len zmena chyby)."""
    time.sleep(30)
    delay, last_err = 5, None
    while True:
        t0 = time.time()
        try:
            cfg = load_cfg()
            if not enabled(cfg):
                time.sleep(10)
                continue
            run_once(cfg)
            delay, last_err = 5, None
            time.sleep(1)
            continue
        except Exception as e:
            err = "%s: %s" % (type(e).__name__, e)
            if err != last_err:
                print("ha_ent: " + err, flush=True)
                traceback.print_exc()
            last_err = err
            delay = 5 if time.time() - t0 > 120 else min(60, delay * 2)
        time.sleep(delay)


def start():
    threading.Thread(target=ent_loop, name="ha-ent", daemon=True).start()
