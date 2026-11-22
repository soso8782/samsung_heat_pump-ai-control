#!/usr/bin/env python3
"""Výpadok riadiaceho izbového čidla: agent.cidlo_vypadok + kontrola v watchdog_tick (jedno upozornenie na výpadok,
jedno "obnovené", poplach v state "alerts" -> /api/status). Dočasné databázy, žiadna sieť."""
import os, sqlite3, tempfile, unittest
from unittest import mock

os.environ.setdefault("DRY_RUN", "1")
import agent, app, profil
import common
from common import S, get_state

NOW = 1_800_000_000
ENT = profil.device_entity("sledovana")


class Cidlo(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.tc = os.path.join(d, "tc.db")
        c = sqlite3.connect(self.tc)
        c.executescript("CREATE TABLE samples(ts INTEGER, entity TEXT, value REAL);"
                        "CREATE TABLE events(ts INTEGER, kind TEXT, entity TEXT, old TEXT, new TEXT, text TEXT);"
                        "CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);")
        c.commit(); c.close()
        self.cfg = dict(common.load_cfg(), TC_DB=self.tc, AGENT_DB=os.path.join(d, "agent.db"),
                        OPENROUTER_API_KEY="", HA_TOKEN="", CIDLO_STALE_MIN="60")
        self.c = common.adb(self.cfg)
        self.sent = []
        ps = [mock.patch.object(agent.notify, "event", side_effect=lambda cfg, k, t, m: (self.sent.append((k, m)) or (True, ""))),
              mock.patch.object(agent, "_esp_val", side_effect=RuntimeError("bez siete")),
              mock.patch.object(agent.dhw, "tick"), mock.patch.object(agent.abtest, "tick"),
              mock.patch.object(agent.interventions, "evaluate_due"), mock.patch.object(agent, "event_trigger")]
        for p in ps:
            p.start(); self.addCleanup(p.stop)

    def db(self, sql, *a):
        c = sqlite3.connect(self.tc); c.execute(sql, a); c.commit(); c.close()

    def collector_ok(self, now):
        self.db("INSERT OR REPLACE INTO meta VALUES('last_sample',?)", str(now - 30))

    def tick(self, now):
        self.collector_ok(now)
        return agent.watchdog_tick(self.cfg, self.c, {}, now)

    def kinds(self):
        return [k for k, _ in self.sent]

    def test_cerstve_data_bez_poplachu(self):
        self.db("INSERT INTO samples VALUES(?,?,?)", NOW - 60, ENT, 21.5)
        self.assertNotIn("cidlo", self.tick(NOW))
        self.assertEqual(self.sent, [])

    def test_chybajuca_vzorka_dlhsie_ako_limit(self):
        self.db("INSERT INTO samples VALUES(?,?,?)", NOW - 90 * 60, ENT, 21.5)
        al = self.tick(NOW)
        self.assertIn("cidlo", al)
        self.assertIn("90", al["cidlo"])
        self.assertEqual(self.kinds(), ["cidlo"])

    def test_pod_limitom_ticho(self):
        self.db("INSERT INTO samples VALUES(?,?,?)", NOW - 50 * 60, ENT, 21.5)
        self.assertNotIn("cidlo", self.tick(NOW))

    def test_text_cakam_na_teplotu(self):
        self.db("INSERT INTO samples VALUES(?,?,?)", NOW - 60, ENT, 21.5)   # vzorky chodia (držaná hodnota)
        for ts, txt in ((NOW - 7200, "Kúrenie"), (NOW - 4000, "Čakám na teplotu z izby (20 s) - stav sa drží"),
                        (NOW - 3900, "Čakám na teplotu z izby (120 s) - stav sa drží")):
            self.db("INSERT INTO events VALUES(?,?,?,?,?,NULL)", ts, "regulacia", agent.CIDLO_STAV_ENTITA, "", txt)
        al = self.tick(NOW)
        self.assertIn("cidlo", al)
        self.assertIn("66", al["cidlo"])          # od NOW-4000 = 66 min

    def test_jedno_upozornenie_na_vypadok_a_obnovene(self):
        self.db("INSERT INTO samples VALUES(?,?,?)", NOW - 90 * 60, ENT, 21.5)
        self.tick(NOW); self.tick(NOW + 60); self.tick(NOW + 120)
        self.assertEqual(self.kinds(), ["cidlo"])
        self.assertEqual(get_state(self.c, "wd_cidlo_sent"), "1")
        self.db("INSERT INTO samples VALUES(?,?,?)", NOW + 150, ENT, 22.0)
        al = self.tick(NOW + 180)
        self.assertNotIn("cidlo", al)
        self.assertEqual(self.kinds(), ["cidlo", "cidlo_ok"])
        self.tick(NOW + 240)
        self.assertEqual(self.kinds(), ["cidlo", "cidlo_ok"])
        # nový výpadok = nové upozornenie
        self.tick(NOW + 240 + 2 * 3600)
        self.assertEqual(self.kinds(), ["cidlo", "cidlo_ok", "cidlo"])

    def test_poplach_v_api_status(self):
        self.db("INSERT INTO samples VALUES(?,?,?)", NOW - 90 * 60, ENT, 21.5)
        self.tick(NOW)
        import json
        al = json.loads(get_state(self.c, "alerts"))
        self.assertEqual([a["kind"] for a in al], ["cidlo"])    # app.status čita tento kľúč

    def test_zberac_neaktualny_nehodnoti(self):
        self.db("INSERT INTO samples VALUES(?,?,?)", NOW - 5 * 3600, ENT, 21.5)
        self.db("INSERT OR REPLACE INTO meta VALUES('last_sample',?)", str(NOW - 5 * 3600))
        self.assertIsNone(agent.cidlo_vypadok(self.cfg, NOW))

    def test_vypnute_nulou(self):
        self.cfg["CIDLO_STALE_MIN"] = "0"
        self.db("INSERT INTO samples VALUES(?,?,?)", NOW - 9 * 3600, ENT, 21.5)
        self.assertNotIn("cidlo", self.tick(NOW))
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
