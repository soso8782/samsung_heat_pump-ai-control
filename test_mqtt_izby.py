"""Izbové teploty priamo zo Zigbee2MQTT: párovanie, zápis v zberači a prekrytie aktuálnych hodnôt na webe."""
import json, os, sqlite3, tempfile, unittest

os.environ.setdefault("DRY_RUN", "1")
import app
import collector

PROFIL = {"izby": [
    {"kluc": "jedalen", "esp": "Izba Jedáleň", "ha": ["sensor.jedalen_t_h_temperature"]},
    {"kluc": "detska", "esp": "Izba Detská (Zigbee)", "ha": ["sensor.detska_t_h_temperature", "sensor.detska_espresense_teplota"]},
    {"kluc": "kuchyna", "esp": "", "ha": ["sensor.t_h_kuchyna_temperature"]},
]}


class Parovanie(unittest.TestCase):
    def test_mapa(self):
        self.assertEqual(collector.mqtt_room_map(PROFIL),
                         {"jedalen_t_h": "Izba Jedáleň", "detska_t_h": "Izba Detská (Zigbee)"})

    def test_teplota(self):
        self.assertEqual(collector.mqtt_temp(b'{"temperature": 22.72, "humidity": 59}'), 22.72)
        for p in (b'{"humidity": 59}', b'{"temperature": true}', b'{"temperature": 99}', b'nie json', b'[1]'):
            self.assertIsNone(collector.mqtt_temp(p), p)


class Zapis(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        collector.DB = os.path.join(self.d, "tc.db")
        self.c = collector.db()
        collector.state.clear(); collector.mqtt_ts.clear()
        self.rmap = collector.mqtt_room_map(PROFIL)

    def tearDown(self):
        self.c.close()

    def test_sprava_jedalen(self):
        esp = collector.mqtt_room(self.c, self.rmap, "zigbee2mqtt", "zigbee2mqtt/Jedálen T&H",
                                  b'{"temperature":22.72}', now=1000)
        self.assertEqual(esp, "Izba Jedáleň")
        self.assertEqual(collector.sv("Izba Jedáleň"), 22.72)
        self.assertEqual(collector.mqtt_ts["Izba Jedáleň"], 1000)
        v = self.c.execute("SELECT v FROM meta WHERE k='live:Izba Jedáleň'").fetchone()[0]
        self.assertEqual(json.loads(v), {"v": 22.72, "ts": 1000})

    def test_ignoruje(self):
        for topic, payload, retain in (("zigbee2mqtt/Jedálen T&H", b'{"temperature":22}', True),       # retained
                                       ("zigbee2mqtt/Jedálen T&H/availability", b'{"temperature":22}', False),
                                       ("iny/Jedálen T&H", b'{"temperature":22}', False),
                                       ("zigbee2mqtt/Neznámy", b'{"temperature":22}', False),
                                       ("zigbee2mqtt/Jedálen T&H", b'{"contact":true}', False)):
            self.assertIsNone(collector.mqtt_room(self.c, self.rmap, "zigbee2mqtt", topic, payload, retain, now=1000), topic)
        self.assertEqual(collector.state, {})


class Prekrytie(unittest.TestCase):
    def test_novsia_hodnota(self):
        meta = {"last_sample": "1000", "live:Izba Jedáleň": json.dumps({"v": 22.7, "ts": 1030}),
                "live:Izba Kuchyňa": json.dumps({"v": 21.0, "ts": 990}),          # staršia než vzorka
                "live:Iné": json.dumps({"v": 5, "ts": 1030}),                       # nie je medzi menami
                "live:Izba Spálňa": "zlé"}
        cur = app.live_overlay(meta, {"Izba Jedáleň": 22.5, "Izba Kuchyňa": 22.0}, ["Izba Jedáleň", "Izba Kuchyňa", "Izba Spálňa"], 1000, 1040)
        self.assertEqual(cur, {"Izba Jedáleň": 22.7, "Izba Kuchyňa": 22.0})

    def test_prilis_stara(self):
        meta = {"live:Izba Jedáleň": json.dumps({"v": 22.7, "ts": 1030})}
        cur = app.live_overlay(meta, {"Izba Jedáleň": 22.5}, ["Izba Jedáleň"], 1000, 1030 + app.LIVE_MAX_AGE + 1)
        self.assertEqual(cur, {"Izba Jedáleň": 22.5})


class CielHned(unittest.TestCase):
    def test_potvrdeny_ciel_sa_ukaze_hned(self):
        import agent, app, json, os, sqlite3, tempfile
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "tc.db")
            agent._live_value({"TC_DB": db}, "Cieľová izbová teplota", 23.5, now=2000)
            meta = dict(sqlite3.connect(db).execute("SELECT k, v FROM meta"))
            self.assertEqual(json.loads(meta["live:Cieľová izbová teplota"]), {"v": 23.5, "ts": 2000})
            cur = app.live_overlay(meta, {"Cieľová izbová teplota": 23.0}, ["Cieľová izbová teplota"], 1990, 2010)
            self.assertEqual(cur["Cieľová izbová teplota"], 23.5)
            # novšia vzorka zberača má prednosť
            cur = app.live_overlay(meta, {"Cieľová izbová teplota": 23.5}, ["Cieľová izbová teplota"], 2060, 2070)
            self.assertEqual(cur["Cieľová izbová teplota"], 23.5)


if __name__ == "__main__":
    unittest.main()
