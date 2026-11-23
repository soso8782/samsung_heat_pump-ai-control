#!/usr/bin/env python3
"""Senzory spotreby a COP tepelného čerpadla do Home Assistant cez MQTT discovery.

Vlákno agenta raz za minútu zoberie živé (kalibrované) hodnoty zo stats.live a pošle ich na MQTT broker z config.env
(MQTT_HOST…); Home Assistant si z discovery správ sám vytvorí zariadenie „Samsung Heat Pump AI Control – tepelné čerpadlo“ so senzormi.
Počítadlo „Spotreba celkom“ len rastie (vhodné do panela Energia): pripočítavajú sa prírastky dennej kalibrovanej spotreby,
takže zmena kalibračného koeficientu ani polnoc ho nikdy neznížia. Vypnutie: MQTT_PUB=0 v config.env; pri DRY_RUN=1 sa neposiela."""
import datetime as _dt, json, socket, threading, time, traceback

import contacts_mqtt as cm
import i18n
import stats
from i18n import tr
from common import adb, get_state, load_cfg, set_state

PERIOD = 60                 # s medzi odoslaniami
DISC_EVERY = 1800           # s: discovery správy sa opakujú (sú aj retained)
STALE_S = 600               # staršie živé dáta sa neposielajú (senzory v HA po EXPIRE_S prejdú na „nedostupné“)
EXPIRE_S = 300
MAX_STEP_KWH = 0.5          # najväčší prírastok počítadla za jeden krok (12 kW × 2,5 min); väčší skok = zmena kalibrácie
DISC_PREFIX = "homeassistant"
STATE_TOPIC = "kurenie/tc/state"
DEVICE = {"identifiers": ["kurenie_tc"], "name": "Samsung Heat Pump AI Control – tepelné čerpadlo", "manufacturer": "Samsung Heat Pump AI Control",
          "model": "Výpočet spotreby a COP"}
# (kľúč, názov, jednotka, device_class, state_class, ikona, desatinné miesta)
SENSORS = (
    ("prikon_w", "Príkon", "W", "power", "measurement", None, 0),
    ("energia_kwh", "Spotreba celkom", "kWh", "energy", "total_increasing", None, 2),
    ("spotreba_dnes_kwh", "Spotreba dnes", "kWh", "energy", "total_increasing", None, 2),
    ("teplo_kw", "Tepelný výkon", "kW", "power", "measurement", "mdi:radiator", 2),
    ("cop_teraz", "COP teraz", None, None, "measurement", "mdi:heat-pump-outline", 2),
    ("cop_dnes", "COP dnes", None, None, "measurement", "mdi:heat-pump-outline", 2),
    ("prietok_lpm", "Prietok (výpočet)", "L/min", None, "measurement", "mdi:waves-arrow-right", 1),
    ("koeficient", "Kalibračný koeficient", None, None, "measurement", "mdi:tune", 3),
    # obdobia (od pondelka / od 1. dňa mesiaca / od 1. januára) zo štatistík aplikácie; pri prekalibrovaní sa môžu mierne zmeniť
    ("spotreba_tyzden_kwh", "Spotreba tento týždeň", "kWh", "energy", "total", None, 1),
    ("spotreba_mesiac_kwh", "Spotreba tento mesiac", "kWh", "energy", "total", None, 1),
    ("spotreba_rok_kwh", "Spotreba tento rok", "kWh", "energy", "total", None, 0),
    ("teplo_tyzden_kwh", "Vyrobené teplo tento týždeň", "kWh", "energy", "total", "mdi:radiator", 1),
    ("teplo_mesiac_kwh", "Vyrobené teplo tento mesiac", "kWh", "energy", "total", "mdi:radiator", 1),
    ("teplo_rok_kwh", "Vyrobené teplo tento rok", "kWh", "energy", "total", "mdi:radiator", 0),
    ("cop_tyzden", "COP tento týždeň", None, None, "measurement", "mdi:heat-pump-outline", 2),
    ("cop_mesiac", "COP tento mesiac", None, None, "measurement", "mdi:heat-pump-outline", 2),
    ("cop_rok", "COP tento rok", None, None, "measurement", "mdi:heat-pump-outline", 2),
    # fakturačné obdobie dodávateľa elektriny (začiatok BILLING_START=MM-DD v config.env, predvolene 1. november)
    ("spotreba_obdobie_kwh", "Spotreba vo fakturačnom období", "kWh", "energy", "total", None, 0),
    ("teplo_obdobie_kwh", "Vyrobené teplo vo fakturačnom období", "kWh", "energy", "total", "mdi:radiator", 0),
    ("cop_obdobie", "COP vo fakturačnom období", None, None, "measurement", "mdi:heat-pump-outline", 2),
    ("dom_obdobie_kwh", "Dom spolu vo fakturačnom období (elektromer)", "kWh", "energy", "total", "mdi:home-lightning-bolt", 0),
)
BILLING_DEFAULT = (11, 1)


def billing_start(cfg, now):
    """Začiatok aktuálneho fakturačného obdobia (unix s): posledný výskyt dňa BILLING_START (MM-DD) nie neskôr ako dnes."""
    m, d = BILLING_DEFAULT
    try:
        mm, dd = str((cfg or {}).get("BILLING_START") or "").strip().split("-")
        _dt.date(2024, int(mm), int(dd))
        m, d = int(mm), int(dd)
    except ValueError:
        pass
    today = _dt.date.fromtimestamp(now)
    s = _dt.date(today.year, m, min(d, 28))
    if s > today:
        s = _dt.date(today.year - 1, m, min(d, 28))
    return int(time.mktime(s.timetuple()))
PERIODS_EVERY = 600         # s: súčty období sa zo stats.db čítajú najviac raz za 10 min


def period_starts(now):
    """Začiatky období (unix s, lokálny čas): pondelok 00:00, 1. deň mesiaca, 1. január."""
    d = _dt.date.fromtimestamp(now)
    ts = lambda x: int(time.mktime(x.timetuple()))
    return {"tyzden": ts(d - _dt.timedelta(days=d.weekday())), "mesiac": ts(d.replace(day=1)), "rok": ts(d.replace(month=1, day=1))}


def period_sums(cfg, now):
    """Spotreba (kalibrovaná, inak odhad), vyrobené teplo a COP za tento týždeň / mesiac / rok z hodinových štatistík (stats.db).
    COP = teplo / spotreba len z hodín, ktoré majú teplo. Vracia slovník kľúčov senzorov; pri chybe prázdny."""
    out = {}
    try:
        con = stats._ro(stats._db_path(cfg), 10)
        try:
            starts = dict(period_starts(now), obdobie=billing_start(cfg, now))
            dom = con.execute("SELECT sum(kwh_dom) FROM hourly WHERE ts>=? AND ts<=?", (starts["obdobie"], now)).fetchone()[0]
            out["dom_obdobie_kwh"] = round(dom, 2) if dom is not None else None
            for name, a in starts.items():
                e, q, eq = con.execute(
                    "SELECT sum(COALESCE(kwh_cal, kwh)), sum(q_kwh), sum(CASE WHEN q_kwh IS NOT NULL THEN COALESCE(kwh_cal, kwh) END) "
                    "FROM hourly WHERE ts>=? AND ts<=?", (a, now)).fetchone()
                out["spotreba_%s_kwh" % name] = round(e, 2) if e is not None else None
                out["teplo_%s_kwh" % name] = round(q, 2) if q is not None else None
                out["cop_%s" % name] = round(q / eq, 2) if q is not None and eq and eq >= 0.05 else None
        finally:
            con.close()
    except Exception as ex:
        print("ha_pub: obdobia: %s: %s" % (type(ex).__name__, ex), flush=True)
        return {}
    return out


def build_publish(topic, payload, retain=False):
    """MQTT 3.1.1 PUBLISH, QoS 0."""
    body = cm._str(topic) + (payload if isinstance(payload, bytes) else str(payload).encode("utf-8"))
    return bytes([0x30 | (1 if retain else 0)]) + cm._enc_len(len(body)) + body


def device_block(lang="sk"):
    """Blok `device` spoločný pre všetky entity (ha_pub aj ha_ent): jedno zariadenie v HA."""
    return DEVICE if lang == "sk" else dict(DEVICE, name=tr("srv.ha_pub.zariadenie", lang), model=tr("srv.ha_pub.model", lang), manufacturer=tr("srv.ha_pub.vyrobca", lang))


def discovery(lang="sk"):
    """Zoznam (topic, JSON) discovery správ pre všetky senzory. Názvy v jazyku domácnosti; unique_id sa nemení."""
    out = []
    device = device_block(lang)
    for key, name, unit, dc, sc, icon, dec in SENSORS:
        if lang != "sk":
            name = tr("srv.ha_pub.n_" + key, lang)
        d = {"name": name, "unique_id": "kurenie_tc_" + key, "object_id": "kurenie_tc_" + key, "state_topic": STATE_TOPIC,
             "value_template": "{{ value_json.%s }}" % key, "device": device, "expire_after": EXPIRE_S,
             "suggested_display_precision": dec}
        if unit:
            d["unit_of_measurement"] = unit
        if dc:
            d["device_class"] = dc
        if sc:
            d["state_class"] = sc
        if icon:
            d["icon"] = icon
        out.append(("%s/sensor/kurenie_tc/%s/config" % (DISC_PREFIX, key), json.dumps(d, ensure_ascii=False)))
    return out


def total_step(total, last_day, last_val, day, val):
    """Nová hodnota rastúceho počítadla. total = doterajší súčet, last_* = deň a denná spotreba pri minulom kroku, day/val = teraz.
    Nový deň: pripočíta sa celá dnešná hodnota (ak je malá, inak nič – reštart po dlhšom výpadku). Ten istý deň: kladný prírastok
    do MAX_STEP_KWH; záporný alebo väčší skok (zmena kalibrácie, výpadok) sa nepripočíta. Počítadlo nikdy neklesne."""
    if val is None:
        return total
    if last_val is None or last_day is None:
        return total
    step = val if day != last_day else val - last_val
    if 0 < step <= MAX_STEP_KWH:
        total += step
    return total


def payload(lv, total):
    """Stavová správa (slovník) zo stats.live a počítadla."""
    r = lambda v, n: round(v, n) if isinstance(v, (int, float)) else None
    return {"prikon_w": r(lv.get("p_el_w"), 0), "energia_kwh": round(total, 3), "spotreba_dnes_kwh": r(lv.get("kwh_day"), 2),
            "teplo_kw": r(lv.get("p_th_kw"), 2), "cop_teraz": r(lv.get("cop_now"), 2), "cop_dnes": r(lv.get("cop_day"), 2),
            "prietok_lpm": r(lv.get("flow_lpm"), 1), "koeficient": r(lv.get("k"), 4)}


def send(cfg, msgs):
    """Krátke spojenie: CONNECT, všetky správy (retained), DISCONNECT. Chyba = výnimka."""
    host, user, pw = cfg.get("MQTT_HOST", "").strip(), cfg.get("MQTT_USER", "").strip(), cfg.get("MQTT_PASS", "")
    port = int(cfg.get("MQTT_PORT") or 1883)
    s = socket.create_connection((host, port), timeout=10)
    try:
        s.settimeout(10)
        s.sendall(cm.build_connect("kurenie-pub", user, pw))
        typ, _, body = cm.read_packet(s)
        if typ != 2 or len(body) < 2 or body[1] != 0:
            raise ConnectionError("broker odmietol pripojenie (kód %s)" % (body[1] if len(body) > 1 else "?"))
        for topic, data in msgs:
            s.sendall(build_publish(topic, data, retain=True))
        s.sendall(b"\xe0\x00")
    finally:
        s.close()


def enabled(cfg):
    return bool(cfg.get("MQTT_HOST", "").strip()) and str(cfg.get("MQTT_PUB", "1")).strip() != "0" \
        and str(cfg.get("DRY_RUN", "0")).strip() != "1"


def tick(cfg, mem, now=None):
    """Jeden krok: prepočíta počítadlo a odošle stav (a podľa potreby discovery). Vracia odoslaný slovník alebo None."""
    now = int(now or time.time())
    lv = stats.live(cfg, now)
    if not lv.get("updated_ts") or now - lv["updated_ts"] > STALE_S:
        return None
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    c = adb(cfg)
    try:
        try:
            st = json.loads(get_state(c, "ha_pub_total") or "{}")
        except ValueError:
            st = {}
        total = float(st.get("total") or 0.0)
        val = lv.get("kwh_day")
        total = total_step(total, st.get("day"), st.get("val"), day, val)
        if val is not None:
            set_state(c, "ha_pub_total", json.dumps({"total": round(total, 4), "day": day, "val": val}))
    finally:
        c.close()
    p = payload(lv, total)
    if now - mem.get("per_ts", 0) >= PERIODS_EVERY or "per" not in mem:
        mem["per"], mem["per_ts"] = period_sums(cfg, now), now
    for key, _n, _u, _d, _s, _i, _dec in SENSORS:      # každý senzor musí mať v správe kľúč (chýbajúci = „neznáme“)
        p.setdefault(key, mem["per"].get(key))
    msgs = []
    if now - mem.get("disc", 0) >= DISC_EVERY:
        msgs += discovery(i18n.home_lang(cfg))
    msgs.append((STATE_TOPIC, json.dumps(p)))
    send(cfg, msgs)
    if len(msgs) > 1:
        mem["disc"] = now
    return p


def pub_loop():
    """Daemon vlákno agenta; chyba nikdy nezhodí agenta (pri chybe sa skúsi znova o minútu, hlási sa len zmena chyby)."""
    time.sleep(25)
    mem, last_err = {}, None
    while True:
        try:
            cfg = load_cfg()
            if time.time() - mem.get("cost_ts", 0) >= 3600 and str(cfg.get("DRY_RUN", "0")).strip() != "1":
                mem["cost_ts"] = time.time()          # náklady do panela Energia (ha_cost.py), raz za hodinu
                try:
                    import ha_cost
                    ha_cost.sync(cfg)
                except Exception as e:
                    print("ha_pub: naklady: %s: %s" % (type(e).__name__, e), flush=True)
            if enabled(cfg):
                tick(cfg, mem)
                if last_err:
                    print("ha_pub: spojenie obnovené", flush=True)
                last_err = None
        except Exception as e:
            err = "%s: %s" % (type(e).__name__, e)
            if err != last_err:
                print("ha_pub: " + err, flush=True)
                traceback.print_exc()
            last_err = err
        time.sleep(PERIOD)


def start():
    threading.Thread(target=pub_loop, name="ha-pub", daemon=True).start()
