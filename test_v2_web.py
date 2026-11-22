#!/usr/bin/env python3
"""Testy webu v2 (úloha B): profil API, sprievodca, profil_info, reštart služieb. Nič sa nepripája na ESP ani HA (všetko mockované)."""
import http.client, json, os, sqlite3, sys, tempfile, threading, time, unittest
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app, profil
from common import adb


def custom_profile():
    p = profil.default()
    p["zariadenie"].update(kurenie="podlahove", tuv=False, model="", nazov="Test Mono 8 kW", typ="mono", vykon_kw=8.0)
    p["esp"]["entity"]["tuv"] = ""
    p["izby"] = [{"kluc": "salon", "nazov": "Salón", "esp": "Izba Salón", "referencna": True, "min": None, "ha": []},
                 {"kluc": "kuchyna", "nazov": "Kuchyňa", "esp": "Izba Kuchyňa", "min": 21.5, "ha": []},
                 {"kluc": "spalna", "nazov": "Spálňa", "esp": "Izba Spálňa", "min": 20.0, "ha": []}]
    return p


class Tmp(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.pf = os.path.join(self.d, "profil.json")
        self._pp = mock.patch.object(profil, "PROFILE_FILE", self.pf); self._pp.start()
        self.cfg = {"AGENT_DB": os.path.join(self.d, "a.db"), "TC_DB": os.path.join(self.d, "tc.db"), "STATS_DB": os.path.join(self.d, "stats.db"),
                    "HA_TOKEN": "", "OPENROUTER_API_KEY": "", "HA_URL": "http://ha.invalid:8123", "ESP_URL": "http://esp.invalid"}
        app._spotreba_cache.update(t=0.0, v=False)

    def tearDown(self):
        self._pp.stop()


class Info(Tmp):
    def test_default_info(self):
        self.cfg["OPENROUTER_API_KEY"] = "sk-or-x"
        with mock.patch.object(app, "_spotreba_domu", return_value=True):
            i = app.profil_info(self.cfg)
        self.assertEqual([r["kluc"] for r in i["izby"]][0], "detska")
        self.assertEqual(len(i["izby"]), 7)
        self.assertEqual([r["referencna"] for r in i["izby"]].count(True), 1)
        self.assertEqual(i["izby"][0]["esp"], "Sledovaná teplota (Detská izba)")
        f = i["funkcie"]
        self.assertTrue(f["tuv"] and f["agent_ai"] and f["spotreba_domu"]); self.assertFalse(f["ha"])
        self.assertEqual(i["tuv_entity"]["teplota"], "Teplota TÚV (Bojler)")
        self.assertEqual(i["ciel_rozsah"], [20.0, 24.5])

    def test_custom_info_no_tuv(self):
        self.assertEqual(profil.save(custom_profile()), [])
        i = app.profil_info(self.cfg)
        self.assertEqual([r["kluc"] for r in i["izby"]], ["salon", "kuchyna", "spalna"])
        self.assertFalse(i["funkcie"]["tuv"]); self.assertNotIn("tuv_entity", i)
        self.assertFalse(i["funkcie"]["agent_ai"]); self.assertFalse(i["funkcie"]["model_cop"])
        self.assertEqual(i["kurenie"], "podlahove")

    def test_no_secrets_in_info(self):
        self.cfg.update(HA_TOKEN="TAJNY-HA", OPENROUTER_API_KEY="TAJNY-OR")
        i = app.profil_info(self.cfg)
        self.assertNotIn("TAJNY", json.dumps(i)); self.assertTrue(i["funkcie"]["ha"] and i["funkcie"]["agent_ai"])

    def test_spotreba_detection(self):
        import sqlite3
        self.assertFalse(app._spotreba_domu(self.cfg) and False)
        with mock.patch("ha_cost.load_ims", return_value={}):
            app._spotreba_cache["t"] = 0.0
            self.assertFalse(app._spotreba_domu(self.cfg))
        c = sqlite3.connect(self.cfg["STATS_DB"]); c.execute("CREATE TABLE hourly(ts INTEGER, kwh_dom REAL)"); c.execute("INSERT INTO hourly VALUES(1, 0.4)"); c.commit(); c.close()
        app._spotreba_cache["t"] = 0.0
        self.assertTrue(app._spotreba_domu(self.cfg))


class Api(Tmp):
    def test_get_structure(self):
        g = app.profil_get()
        for k in ("profil", "chyby", "modely", "typy_kurenia", "typy_jednotky", "abs_limity", "predvolby"):
            self.assertIn(k, g)
        self.assertEqual(g["chyby"], []); self.assertFalse(g["ulozeny"])
        self.assertIn("fsv2031", g["abs_limity"]); self.assertIn("podlahove", g["predvolby"])
        json.dumps(g)

    def test_post_errors_and_ok(self):
        bad = custom_profile(); bad["zariadenie"]["vykon_kw"] = 99; bad["izby"][1]["referencna"] = True
        code, out = app.profil_post({"profil": bad})
        self.assertEqual(code, 400); self.assertTrue(out["chyby"]); self.assertFalse(os.path.exists(self.pf))
        self.assertEqual(app.profil_post({})[0], 400)
        code, out = app.profil_post({"profil": custom_profile()})
        self.assertEqual((code, out["ok"], out["restart_potrebny"]), (200, True, True))
        self.assertEqual(profil.load()["zariadenie"]["nazov"], "Test Mono 8 kW")
        self.assertEqual(app.profil_get()["ulozeny"], True)

    def test_default_deletes(self):
        profil.save(custom_profile()); self.assertTrue(os.path.exists(self.pf))
        code, out = app.profil_default()
        self.assertEqual(code, 200); self.assertFalse(os.path.exists(self.pf))
        self.assertEqual(profil.load(), profil.default())
        self.assertEqual(app.profil_default()[0], 200)   # idempotentné

    def test_navrh_default_names(self):
        d = profil.default()
        names = list(d["esp"]["entity"].values()) + list(d["esp"]["nastavenia"].values()) + [r["esp"] for r in d["izby"]]
        n = app.esp_navrh(names)
        self.assertEqual(n["entity"], {k: v for k, v in d["esp"]["entity"].items() if v})
        self.assertEqual(n["nastavenia"], d["esp"]["nastavenia"])
        self.assertEqual(len(n["izby"]), 7)
        self.assertIn("jedalen", [x["kluc"] for x in n["izby"]])

    def test_navrh_foreign_names(self):
        n = app.esp_navrh(["Outdoor temp", "Vystupna voda Tw2", "FSV 2032 low", "Teplota TUV", "Zosilnenie posunu"])
        self.assertEqual(n["entity"].get("tw2"), "Vystupna voda Tw2"); self.assertEqual(n["nastavenia"].get("fsv2032"), "FSV 2032 low")
        self.assertEqual(n["entity"].get("tuv"), "Teplota TUV"); self.assertEqual(n["nastavenia"].get("zosilnenie"), "Zosilnenie posunu")

    def test_slug(self):
        self.assertEqual(app.slug_key("Kúpeľňa hore"), "kupelna_hore"); self.assertEqual(app.slug_key("2. poschodie")[:2], "i_")
        self.assertTrue(profil.KEY_RE.match(app.slug_key("Veľmi dlhý názov izby na poschodí")))

    def test_esp_entities_merge(self):
        import sqlite3
        c = sqlite3.connect(self.cfg["TC_DB"]); c.execute("CREATE TABLE samples(ts INTEGER, entity TEXT, value REAL)")
        now = int(time.time()); c.executemany("INSERT INTO samples VALUES(?,?,?)", [(now - 30, "Frekvencia kompresora", 42.0), (now - 20, "Frekvencia kompresora", 44.0),
                                                                                      (now - 5000, "Stará entita", 1.0)]); c.commit(); c.close()
        page = '<table><tr id="sensor-a"><td>Vonkajšia teplota</td><td>3.5 °C</td></tr><tr id="sensor-b"><td>Frekvencia kompresora</td><td>40 Hz</td></tr></table>'
        with mock.patch.object(app, "_http_get_text", return_value=page):
            r = app.profil_esp_entity(self.cfg)
        by = {e["nazov"]: e for e in r["entity"]}
        self.assertEqual(by["Vonkajšia teplota"]["zdroj"], "esp"); self.assertEqual(by["Frekvencia kompresora"]["hodnota"], "40 Hz")
        self.assertNotIn("Stará entita", by); self.assertEqual(r["navrh"]["entity"]["vonku"], "Vonkajšia teplota")
        with mock.patch.object(app, "_http_get_text", side_effect=OSError("offline")):
            r = app.profil_esp_entity(self.cfg)   # ESP nedostupné: ostanú vzorky z tc.db
        self.assertEqual([e["zdroj"] for e in r["entity"]], ["tc.db"]); self.assertEqual(r["entity"][0]["hodnota"], 44.0)

    def test_esp_entities_nothing_no_crash(self):
        with mock.patch.object(app, "_http_get_text", side_effect=OSError("x")):
            r = app.profil_esp_entity({"TC_DB": os.path.join(self.d, "chyba.db"), "ESP_URL": "http://x"})
        self.assertEqual(r["entity"], [])

    def test_ha_entity(self):
        r = app.profil_ha_entity(self.cfg); self.assertFalse(r["ok"]); self.assertEqual(r["entity"], [])
        self.cfg["HA_TOKEN"] = "tok"
        rows = [{"entity_id": "sensor.b_t", "meno": "B", "stav": "21"}, {"entity_id": "light.x", "meno": "X"}, {"entity_id": "sensor.a_t", "meno": "A", "stav": "20"}]
        with mock.patch.object(app, "_ha_temp_sensors", return_value=rows):
            r = app.profil_ha_entity(self.cfg)
        self.assertEqual([e["entity_id"] for e in r["entity"]], ["sensor.a_t", "sensor.b_t"])
        with mock.patch.object(app, "_ha_temp_sensors", side_effect=OSError("down")):
            r = app.profil_ha_entity(self.cfg)
        self.assertFalse(r["ok"]); self.assertNotIn("tok", json.dumps(r))

    def test_restart(self):
        self.assertFalse(app.restart_services(dict(self.cfg, DRY_RUN="1"))["ok"])
        with mock.patch.object(app.shutil, "which", return_value=None):
            r = app.restart_services(dict(self.cfg, DRY_RUN="0")); self.assertFalse(r["ok"]); self.assertTrue(r["dovod"])
        calls = []; done = threading.Event()

        def fake(*a):
            calls.append(a)
            if a[-1] == "kurenie-web":
                done.set()
            return 0, ""
        with mock.patch.object(app.shutil, "which", return_value="/bin/systemctl"), mock.patch.object(app, "_systemctl", side_effect=fake), \
                mock.patch.object(app, "RESTART_DELAY", 0):
            r = app.restart_services(dict(self.cfg, DRY_RUN="0"))
            self.assertTrue(r["ok"]); self.assertTrue(done.wait(5))
        self.assertEqual(calls[0], ("restart", "kurenie-agent", "kurenie-collector")); self.assertEqual(calls[1], ("restart", "kurenie-web"))

    def test_module_constants_follow_profile(self):
        self.assertIn("detska", app.SERIES_KEYS); self.assertIn("bojler", app.SERIES_KEYS)
        self.assertTrue(all(k in app.S for k in app.ROOM_KEYS))


class Riadiaca(Tmp):
    """Výber riadiacej izby: select „Riadiaca izba“ v ESP + prepnutie referenčnej izby v profile (ESP aj reštart sú mockované)."""
    OPTS = ["Salón (ESPresense)", "Salón (Zigbee)", "Kuchyňa", "Spálňa", "Pivnica"]

    def setUp(self):
        super().setUp()
        self.assertEqual(profil.save(custom_profile()), [])
        self.state = {"v": self.OPTS[0]}
        app._riadiaca_cache.update(t=0.0, ttl=0, v=None)
        self.cfg["DRY_RUN"] = "0"
        mapa = lambda: app.riadiaca_mapa(self.OPTS, profil.load()["izby"])
        self.p1 = mock.patch.object(app, "riadiaca_info", side_effect=lambda cfg, force=False: {"stav": self.state["v"], "moznosti": mapa()})
        self.p2 = mock.patch.object(app._agent_mod, "esp_set_select", side_effect=lambda cfg, n, o: self.state.update(v=o))
        self.p3 = mock.patch.object(app, "restart_services", return_value={"ok": True})
        self.p4 = mock.patch.object(app.time, "sleep")
        for x in (self.p1, self.p2, self.p3, self.p4):
            x.start()

    def tearDown(self):
        for x in (self.p1, self.p2, self.p3, self.p4):
            x.stop()
        super().tearDown()

    def test_mapa(self):
        m = app.riadiaca_mapa(self.OPTS, profil.load()["izby"])
        self.assertEqual([(x["kluc"], x["cidlo"]) for x in m],
                         [("salon", "ESPresense"), ("salon", "Zigbee"), ("kuchyna", ""), ("spalna", "")])   # „Pivnica“ nemá izbu

    def test_ina_izba_prepne_profil_a_restartuje(self):
        code, out = app.riadiaca_post(self.cfg, {"moznost": "Spálňa"}, "admin@x")
        self.assertEqual(code, 200, out); self.assertTrue(out["restart"]); self.assertEqual(out["kluc"], "spalna")
        p = profil.load()
        self.assertEqual(profil.reference_room(p), "spalna")
        r = {x["kluc"]: x for x in p["izby"]}
        self.assertIsNone(r["spalna"]["min"]); self.assertFalse(r["salon"]["referencna"]); self.assertEqual(r["salon"]["min"], 21.0)
        app.restart_services.assert_called_once()

    def test_cidlo_tej_istej_izby_bez_restartu(self):
        code, out = app.riadiaca_post(self.cfg, {"moznost": "Salón (Zigbee)"}, "admin@x")
        self.assertEqual(code, 200, out); self.assertFalse(out["restart"])
        self.assertEqual(profil.reference_room(), "salon"); app.restart_services.assert_not_called()

    def test_neplatna_a_dry(self):
        self.assertEqual(app.riadiaca_post(self.cfg, {"moznost": "Pivnica"}, "a")[0], 400)
        code, out = app.riadiaca_post(dict(self.cfg, DRY_RUN="1"), {"moznost": "Spálňa"}, "a")
        self.assertEqual(code, 200); self.assertIn("note", out)
        self.assertEqual(profil.reference_room(), "salon"); app._agent_mod.esp_set_select.assert_not_called()

    def test_nepotvrdene(self):
        app._agent_mod.esp_set_select.side_effect = lambda cfg, n, o: None
        self.assertEqual(app.riadiaca_post(self.cfg, {"moznost": "Spálňa"}, "a")[0], 502)
        self.assertEqual(profil.reference_room(), "salon")

    def test_web_zapis_je_strojovo_citatelny(self):
        app.riadiaca_post(self.cfg, {"moznost": "Salón (Zigbee)"}, "admin@x")
        c = adb(self.cfg)
        row = c.execute("SELECT stary, novy, kluc, kto, ts FROM riadiaca_log").fetchall()
        self.assertEqual([r[:4] for r in row], [("Salón (ESPresense)", "Salón (Zigbee)", "salon", "admin@x")])
        self.assertEqual(c.execute("SELECT count(*) FROM runs WHERE summary LIKE 'Riadiaca izba zmenená%'").fetchone()[0], 1)
        c.close()


class RiadiacaHistoria(Tmp):
    """Prepnutia riadiacej izby v grafe: zmeny selectu zo zberača (events) + zápisy z webu (riadiaca_log), zlúčené a bez duplicít."""
    ENT = app.RIADIACA_ENT

    def setUp(self):
        super().setUp()
        self.assertEqual(profil.save(custom_profile()), [])
        tc = sqlite3.connect(self.cfg["TC_DB"])
        tc.executescript("CREATE TABLE events(ts INTEGER, kind TEXT, entity TEXT, old TEXT, new TEXT, text TEXT);"
                         "CREATE TABLE samples(ts INTEGER, entity TEXT, value REAL);"
                         "CREATE TABLE notes(ts INTEGER, author TEXT, title TEXT, body TEXT);")
        self.tc = tc
        self.c = adb(self.cfg)

    def tearDown(self):
        self.tc.close(); self.c.close()
        super().tearDown()

    def ev(self, ts, old, new):
        self.tc.execute("INSERT INTO events VALUES(?,?,?,?,?,NULL)", (ts, "ovladanie", self.ENT, old, new)); self.tc.commit()

    def web(self, ts, old, new, kluc, who="admin@x"):
        self.c.execute("INSERT INTO riadiaca_log VALUES(?,?,?,?,?)", (ts, old, new, kluc, who)); self.c.commit()

    def test_zmena_mimo_webu_z_eventov(self):
        self.ev(500, "Salón (ESPresense)", "Kuchyňa")   # priamo v ESP/HA
        r = app.riadiaca_prepnutia(self.cfg, self.c, 600, 2000)
        self.assertEqual((r["zaciatok"], r["zmeny"]), ("Kuchyňa", []))   # možnosť platná pred rozsahom, v ňom už žiadna zmena
        r = app.riadiaca_prepnutia(self.cfg, self.c, 0, 2000)
        z = r["zmeny"][0]
        self.assertEqual((z["ts"], z["stary"], z["novy"], z["kluc"], z["zdroj"], z["kto"]), (500, "Salón (ESPresense)", "Kuchyňa", "kuchyna", "esp", None))
        self.assertEqual(r["zaciatok"], "Salón (ESPresense)")

    def test_web_a_event_sa_zluci(self):
        self.ev(1000, "Salón (ESPresense)", "Salón (Zigbee)"); self.web(1003, "Salón (ESPresense)", "Salón (Zigbee)", "salon")
        self.ev(1500, "Salón (Zigbee)", "Spálňa")   # druhá zmena len v ESP
        r = app.riadiaca_prepnutia(self.cfg, self.c, 0, 2000)
        self.assertEqual([(z["novy"], z["zdroj"], z["kto"], z["cidlo"]) for z in r["zmeny"]],
                         [("Salón (Zigbee)", "web", "admin@x", "Zigbee"), ("Spálňa", "esp", None, "")])

    def test_web_bez_eventu_zberac_nebezal(self):
        self.web(1000, "Salón (ESPresense)", "Kuchyňa", "kuchyna")
        r = app.riadiaca_prepnutia(self.cfg, self.c, 0, 2000)
        self.assertEqual([(z["ts"], z["novy"], z["zdroj"]) for z in r["zmeny"]], [(1000, "Kuchyňa", "web")])
        self.assertEqual(r["zaciatok"], "Salón (ESPresense)")
        self.assertEqual(app.riadiaca_prepnutia(self.cfg, self.c, 1001, 2000)["zmeny"], [])   # mimo rozsahu

    def test_prazdne_a_chybajuca_db(self):
        self.assertEqual(app.riadiaca_prepnutia(self.cfg, self.c, 0, 2000), {"zaciatok": None, "zmeny": []})
        self.assertEqual(app.riadiaca_prepnutia(dict(self.cfg, TC_DB="/nonexistent/tc.db"), self.c, 0, 2000), {"zaciatok": None, "zmeny": []})

    def test_series_a_day_nesu_riadiaca(self):
        now = int(time.time())
        self.ev(now - 3600, "Salón (ESPresense)", "Kuchyňa")
        code, out = app.series_get(self.cfg, {"k": ["detska"], "days": ["1"]})
        self.assertEqual(code, 200)
        self.assertEqual([z["novy"] for z in out["riadiaca"]["zmeny"]], ["Kuchyňa"])
        if "bojler" in app.SERIES_KEYS:
            self.assertNotIn("riadiaca", app.series_get(self.cfg, {"k": ["bojler"]})[1])
        d = app.day(self.cfg, time.strftime("%Y-%m-%d", time.localtime(now - 3600)))
        self.assertIn("riadiaca", d)


class Http(Tmp):
    def setUp(self):
        super().setUp()
        self.p_cfg = mock.patch.object(app, "load_cfg", lambda: dict(self.cfg)); self.p_cfg.start()
        c = adb(self.cfg)
        c.execute("INSERT INTO users(email, role, added_ts) VALUES('u@example.com','user',1)")
        c.commit()
        self.admin = app.create_session(c); self.user = app.create_session(c, "user", "u@example.com"); c.close()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); self.p_cfg.stop(); super().tearDown()

    def req(self, method, path, tok, body=None, hdr=True):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        h = {"Cookie": "%s=%s" % (app.COOKIE, tok)} if tok else {}
        if hdr:
            h["X-Kurenie"] = "1"
        data = json.dumps(body) if body is not None else None
        c.request(method, path, data, h)
        r = c.getresponse(); raw = r.read(); c.close()
        try:
            return r.status, json.loads(raw.decode("utf-8"))
        except ValueError:
            return r.status, raw

    def test_admin_only(self):
        for m, p in (("GET", "/api/profil"), ("GET", "/api/profil/esp_entity"), ("GET", "/api/profil/ha_entity"), ("POST", "/api/profil"),
                     ("POST", "/api/profil/predvoleny"), ("POST", "/api/restart")):
            self.assertEqual(self.req(m, p, self.user, {} if m == "POST" else None)[0], 403, p)
            self.assertEqual(self.req(m, p, None, {} if m == "POST" else None)[0], 401, p)

    def test_admin_flow(self):
        s, g = self.req("GET", "/api/profil", self.admin); self.assertEqual(s, 200); self.assertIn("predvolby", g)
        s, o = self.req("POST", "/api/profil", self.admin, {"profil": custom_profile()}); self.assertEqual((s, o.get("restart_potrebny")), (200, True))
        s, o = self.req("POST", "/api/profil", self.admin, {"profil": {"izby": []}}); self.assertEqual(s, 400); self.assertTrue(o["chyby"])
        self.assertEqual(self.req("POST", "/api/profil", self.admin, {"profil": {}}, hdr=False)[0], 403)   # bez X-Kurenie
        s, o = self.req("POST", "/api/restart", self.admin, {}); self.assertEqual(s, 200)
        s, o = self.req("POST", "/api/profil/predvoleny", self.admin, {}); self.assertEqual(s, 200); self.assertFalse(os.path.exists(self.pf))

    def test_user_status_has_profil_info(self):
        s, o = self.req("GET", "/api/status", self.user)
        self.assertEqual(s, 200); self.assertIn("profil_info", o); self.assertIn("funkcie", o["profil_info"])
        self.assertNotIn("key_hint", o)


if __name__ == "__main__":
    unittest.main()
