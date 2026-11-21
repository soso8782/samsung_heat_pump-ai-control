#!/usr/bin/env python3
"""Testy modulu ha_api (kľúč integrácie, build_state, validácia a dispatch akcií). Bez siete, DB v pamäti."""
import os, sqlite3, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ha_api


def mem():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE state(k TEXT PRIMARY KEY, v TEXT)")
    c.execute("CREATE TABLE runs(id INTEGER PRIMARY KEY, ts INTEGER, mode TEXT, status TEXT, stav TEXT, summary TEXT, actions TEXT)")
    return c


def sample(tuv=True):
    st = {
        "mode": "control", "next_run": 1700003600.0, "now": 1700000000,
        "last_run": {"ts": 1700000100, "stav": "ok", "summary": "Všetko v poriadku."},
        "alerts": [{"kind": "okno", "since": 1, "text": "Okno otvorené"}],
        "targets": {"kuchyna": {"min": 21.0}},
        "current": {"Vonkajšia teplota": 3.456, "Cieľová izbová teplota": 23.5, "Posun ekvitermickej krivky": -1,
                    "Cieľová ekvitermická voda (WL)": 33, "Výstupná teplota vody (Tw2)": 35.2, "Spiatočka (Tw1)": 31.1,
                    "Delta T vykurovacej vody": 4.1, "Frekvencia kompresora": 40, "Chybový kód TČ": 0,
                    "Odmrazovanie": 0, "Izba Obývačka": 22.4, "Izba Kuchyňa": 21.7,
                    "TUV teplota": 47.0, "Cieľová teplota TÚV": 48.0},
        "collector_age": 20, "ha": {"configured": True, "ok": True},
        "tuv": {"active": False}, "dhw": {"boosted": False},
        "profil_info": {"zariadenie": "Samsung EHS Mono", "kurenie": "podlahove", "ciel_rozsah": [19.0, 24.0],
                        "izby": [{"kluc": "obyvacka", "nazov": "Obývačka", "referencna": True, "esp": "Izba Obývačka"},
                                 {"kluc": "kuchyna", "nazov": "Kuchyňa", "referencna": False, "esp": "Izba Kuchyňa"}],
                        "funkcie": {"tuv": tuv},
                        "zariadenie_info": {"model": "AE080", "typ": "mono", "vykon_kw": 8.0, "kurenie": "podlahove"},
                        "tuv_entity": {"teplota": "TUV teplota", "ciel": "Cieľová teplota TÚV"} if tuv else None},
    }
    return st


class Kluc(unittest.TestCase):
    def test_zivotny_cyklus(self):
        c = mem()
        self.assertFalse(ha_api.check(c, "Bearer abc"))          # bez kľúča vždy False
        self.assertFalse(ha_api.key_info(c)["set"])
        k = ha_api.new_key(c, now=123)
        self.assertGreaterEqual(len(k), 40)
        self.assertTrue(ha_api.check(c, "Bearer " + k))
        self.assertTrue(ha_api.check(c, "bearer  " + k + " "))
        self.assertFalse(ha_api.check(c, "Bearer " + k + "x"))
        self.assertFalse(ha_api.check(c, k))
        self.assertFalse(ha_api.check(c, "Bearer "))
        self.assertFalse(ha_api.check(c, None))
        self.assertEqual(ha_api.key_info(c), {"set": True, "created": 123})
        stored = " ".join(v for (v,) in c.execute("SELECT v FROM state"))
        self.assertNotIn(k, stored)                              # uložený len hash
        k2 = ha_api.new_key(c)
        self.assertNotEqual(k, k2)
        self.assertFalse(ha_api.check(c, "Bearer " + k))         # starý prestal platiť
        ha_api.revoke(c)
        self.assertFalse(ha_api.check(c, "Bearer " + k2))
        self.assertFalse(ha_api.key_info(c)["set"])


class Stav(unittest.TestCase):
    def test_plny(self):
        s = ha_api.build_state(sample(), 1700000200, version="2.3.0")
        self.assertEqual(s["api"], 1)
        self.assertEqual(s["verzia"], "2.3.0")
        self.assertEqual(s["zariadenie"]["nazov"], "Samsung EHS Mono")
        self.assertEqual(s["zariadenie"]["model"], "AE080")
        self.assertEqual(s["vonku"], 3.46)
        self.assertEqual(s["ciel_izba"], 23.5)
        self.assertEqual(s["ciel_rozsah"], [19.0, 24.0])
        self.assertEqual(s["izby"][0], {"kluc": "obyvacka", "nazov": "Obývačka", "teplota": 22.4, "min": None, "referencna": True})
        self.assertEqual(s["izby"][1]["min"], 21.0)
        self.assertEqual(s["voda"], {"vystup": 35.2, "spiatocka": 31.1, "ciel_krivky": 33, "delta_t": 4.1})
        self.assertEqual(s["kompresor"], {"frekvencia": 40, "bezi": True, "odmrazovanie": False})
        self.assertEqual(s["tuv"], {"teplota": 47.0, "ciel": 48.0, "ohrev_bezi": False})
        self.assertEqual(s["agent"]["rezim"], "control")
        self.assertEqual(s["agent"]["posledny_beh"], 1700000100)
        self.assertEqual(s["agent"]["dalsi_beh"], 1700003600)
        self.assertEqual(s["chyba_tc"], 0)
        self.assertEqual(s["poplachy"], [{"druh": "okno", "text": "Okno otvorené"}])
        self.assertEqual(s["online"], {"esp": True, "zberac": True, "ha": True})

    def test_info_zaznam_nemaze_stav_agenta(self):
        st = sample()
        st["last_run"] = {"ts": 1700000300, "status": "info", "stav": None, "summary": "Cieľová izbová teplota zmenená"}
        st["last_assessment"] = {"ts": 1700000100, "status": "ok", "stav": "pozor", "summary": "Obývačka pod minimom."}
        a = ha_api.build_state(st, 1)["agent"]
        self.assertEqual((a["stav"], a["zhrnutie"], a["posledny_beh"]), ("pozor", "Obývačka pod minimom.", 1700000100))

    def test_bez_tuv(self):
        self.assertIsNone(ha_api.build_state(sample(tuv=False), 1)["tuv"])

    def test_chybajuce_hodnoty(self):
        st = sample()
        st["current"] = {}
        del st["last_run"], st["collector_age"]
        st["last_run"] = None
        s = ha_api.build_state(st, 1)
        self.assertIsNone(s["vonku"])
        self.assertIsNone(s["voda"]["vystup"])
        self.assertIsNone(s["kompresor"]["bezi"])
        self.assertIsNone(s["chyba_tc"])
        self.assertIsNone(s["izby"][0]["teplota"])
        self.assertIsNone(s["agent"]["posledny_beh"])
        self.assertIsNone(s["online"]["esp"])
        self.assertFalse(s["online"]["zberac"])

    def test_prazdny_a_zly_vstup(self):
        for st in ({}, None, {"current": "x", "profil_info": 5, "alerts": "x"}):
            s = ha_api.build_state(st, 1)
            self.assertEqual(s["izby"], [])
            self.assertIsNone(s["tuv"])
            self.assertEqual(s["poplachy"], [])

    def test_nazvy_z_profilu(self):
        st = sample()
        st["current"]["Moja vonku"] = -2
        s = ha_api.build_state(st, 1, names={"vonku": "Moja vonku"})
        self.assertEqual(s["vonku"], -2)

    def test_odmrazovanie_text(self):
        st = sample()
        st["current"]["Odmrazovanie"] = "on"
        self.assertTrue(ha_api.build_state(st, 1)["kompresor"]["odmrazovanie"])


class Akcie(unittest.TestCase):
    def test_validate(self):
        v = ha_api.validate_action
        self.assertTrue(v("/api/ha/v1/mode", {"mode": "advise"})[0])
        self.assertFalse(v("/api/ha/v1/mode", {"mode": "x"})[0])
        self.assertFalse(v("/api/ha/v1/mode", {})[0])
        self.assertTrue(v("/api/ha/v1/room_target", {"value": 23.5})[0])
        self.assertTrue(v("/api/ha/v1/room_target", {"value": 15})[0])
        self.assertFalse(v("/api/ha/v1/room_target", {"value": 26.5})[0])
        self.assertFalse(v("/api/ha/v1/room_target", {"value": "23"})[0])
        self.assertFalse(v("/api/ha/v1/room_target", {"value": True})[0])
        self.assertTrue(v("/api/ha/v1/tuv_boost", {})[0])
        self.assertFalse(v("/api/ha/v1/tuv_boost", {"target": 60})[0])
        self.assertTrue(v("/api/ha/v1/run_now", {})[0])
        self.assertFalse(v("/api/ha/v1/nic", {})[0])
        self.assertFalse(v("/api/ha/v1/mode", [])[0])

    def test_dispatch(self):
        class A:
            @staticmethod
            def room_target_post(cfg, body, who):
                return 200, {"ok": True, "value": body["value"], "who": who}

            @staticmethod
            def tuv_boost_post(cfg, body, who):
                return 200, {"ok": True, "body": body}
        self.assertEqual(ha_api.dispatch({}, "/api/ha/v1/room_target", {"value": 22}, A)[1]["value"], 22)
        self.assertEqual(ha_api.dispatch({}, "/api/ha/v1/tuv_boost", {}, A)[1]["body"], {"action": "start", "target": 50})
        self.assertEqual(ha_api.dispatch({}, "/api/ha/v1/mode", {"mode": "zle"}, A)[0], 400)
        self.assertEqual(ha_api.dispatch({}, "/api/ha/v1/x", {}, A)[0], 404)

    def test_mode_a_run_now(self):
        c = mem()
        class Proxy:
            execute, commit = staticmethod(c.execute), staticmethod(c.commit)

            @staticmethod
            def close():
                pass
        fake = lambda cfg: Proxy
        self.assertEqual(ha_api._mode({}, {"mode": "off"}, "ha", adb=fake)[1], {"mode": "off"})
        self.assertEqual(ha_api._get(c, "mode"), "off")
        self.assertEqual(c.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
        ha_api._run_now({}, {}, "ha", adb=fake)
        self.assertEqual(ha_api._get(c, "run_now"), "1")


if __name__ == "__main__":
    unittest.main()
