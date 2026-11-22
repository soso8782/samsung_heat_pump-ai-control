#!/usr/bin/env python3
"""Týždenný súhrn kúrenia (agent.suhrn_*): hranice týždňa (lokálny čas, zmena času), raz za týždeň, dobehnutie, výpadok modelu,
vypnutie, GET /api/suhrn. Dočasné databázy, notify aj model sú mock."""
import datetime, os, sqlite3, tempfile, time, unittest
from unittest import mock

os.environ.setdefault("DRY_RUN", "1")
os.environ["TZ"] = "Europe/Bratislava"
time.tzset()
import agent, app, common
from common import ROOMS, S, get_state, set_state


def ts(y, m, d, h=0, mi=0):
    return int(time.mktime((y, m, d, h, mi, 0, 0, 0, -1)))


# 2026-11-23 = pondelok; súhrn za 2026-W47 (16.-22. 11.)
MON = (2026, 11, 23)
WEEK = "2026-W47"


class Suhrn(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.tc = os.path.join(d, "tc.db")
        c = sqlite3.connect(self.tc)
        c.executescript("CREATE TABLE samples(ts INTEGER, entity TEXT, value REAL);"
                        "CREATE TABLE events(ts INTEGER, kind TEXT, entity TEXT, old TEXT, new TEXT, text TEXT);"
                        "CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);")
        a = ts(2026, 11, 16)
        for h in range(0, 7 * 24, 1):
            for i, r in enumerate(ROOMS):
                if r in S:
                    c.execute("INSERT INTO samples VALUES(?,?,?)", (a + h * 3600 + 60, S[r], 21.0 + i))
            if "vonku" in S:
                c.execute("INSERT INTO samples VALUES(?,?,?)", (a + h * 3600 + 60, S["vonku"], 3.0))
        c.commit(); c.close()
        self.cfg = dict(common.load_cfg(), TC_DB=self.tc, AGENT_DB=os.path.join(d, "agent.db"), OPENROUTER_API_KEY="k", SUHRN="1",
                        SUHRN_DEN="1", SUHRN_HODINA="8")
        self.c = common.adb(self.cfg)
        set_state(self.c, "mode", "advise")
        self.sent = []
        self.llm = mock.Mock(return_value=("Skús vyrovnať Detskú ventilom.\nJeden krok.", 1, 1, 0.002))
        ps = [mock.patch.object(agent.notify, "event", side_effect=lambda cfg, k, t, m: (self.sent.append((k, t, m)) or (True, ""))),
              mock.patch.object(agent, "call_llm", self.llm),
              mock.patch("stats.get_range", side_effect=RuntimeError("bez stats")),
              mock.patch("spotreba.report", side_effect=RuntimeError("bez ceny"))]
        for p in ps:
            p.start(); self.addCleanup(p.stop)

    def run_at(self, *t):
        return agent.suhrn_run(self.cfg, self.c, ts(*t))

    def test_hranice_tyzdna_dst(self):
        mon, sun, a, b = agent.suhrn_bounds("2026-W43")        # 19.-25. 10. 2026: v nedeľu 25. 10. sa mení čas (+1 h)
        self.assertEqual((mon.isoformat(), sun.isoformat()), ("2026-10-19", "2026-10-25"))
        self.assertEqual(a, ts(2026, 10, 19))
        self.assertEqual(b, ts(2026, 10, 26))
        self.assertEqual(b - a, 169 * 3600)
        _m, _s, a, b = agent.suhrn_bounds("2026-W13")           # jarná zmena 29. 3. (-1 h)
        self.assertEqual(b - a, 167 * 3600)
        self.assertEqual(agent.suhrn_bounds(WEEK)[2], ts(2026, 11, 16))
        with self.assertRaises(ValueError):
            agent.suhrn_bounds("2026-47")

    def test_pred_planovanym_casom_nic(self):
        self.assertIsNone(self.run_at(2026, 11, 23, 7, 59))
        self.assertEqual(self.sent, [])
        self.assertIsNone(get_state(self.c, "last_suhrn"))

    def test_odoslanie_a_ulozenie(self):
        txt = self.run_at(2026, 11, 23, 8, 1)
        self.assertIn("Izby", txt)
        self.assertIn("Odporúčanie", txt)
        self.assertEqual([k for k, _t, _m in self.sent], ["suhrn"])
        self.assertEqual(get_state(self.c, "last_suhrn"), WEEK)
        s = agent.suhrn_get(self.c, WEEK)
        self.assertEqual((s["od"], s["do"], s["tyzden"]), ("2026-11-16", "2026-11-22", WEEK))
        self.assertAlmostEqual(s["data"]["rozptyl"], len([r for r in ROOMS if r in S]) - 1 if len([r for r in ROOMS if r in S]) > 1 else 0, 1)
        self.assertEqual(agent.suhrn_get(self.c)["tyzden"], WEEK)
        self.assertIsNone(agent.suhrn_get(self.c, "2026-W40"))

    def test_iba_raz_za_tyzden(self):
        self.assertTrue(self.run_at(2026, 11, 23, 8, 1))
        self.assertIsNone(self.run_at(2026, 11, 23, 8, 2))
        self.assertIsNone(self.run_at(2026, 11, 25, 12, 0))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.c.execute("SELECT count(*) FROM reports WHERE kind='suhrn'").fetchone()[0], 1)

    def test_dobehnutie_v_tom_istom_tyzdni(self):
        self.assertTrue(self.run_at(2026, 11, 26, 19, 0))     # agent nebežal v pondelok o 8:00
        self.assertEqual(len(self.sent), 1)
        self.assertIsNone(self.run_at(2026, 11, 27, 8, 0))

    def test_den_a_hodina(self):
        self.cfg.update(SUHRN_DEN="3", SUHRN_HODINA="18")
        self.assertIsNone(self.run_at(2026, 11, 25, 17, 59))   # streda
        self.assertTrue(self.run_at(2026, 11, 25, 18, 0))

    def test_dalsi_tyzden_znova(self):
        self.assertTrue(self.run_at(2026, 11, 23, 8, 1))
        self.assertEqual(agent.suhrn_due(self.cfg, self.c, ts(2026, 11, 30, 9, 0)), "2026-W48")

    def test_vypnute(self):
        self.cfg["SUHRN"] = "0"
        self.assertIsNone(self.run_at(2026, 11, 23, 9, 0))
        self.assertEqual(self.sent, [])

    def test_model_zlyha_posle_sa_bez_neho(self):
        self.llm.side_effect = agent.LLMError("HTTP 500")
        txt = self.run_at(2026, 11, 23, 8, 1)
        self.assertIn("Izby", txt)
        self.assertNotIn("Odporúčanie", txt)
        self.assertEqual(len(self.sent), 1)

    def test_bez_kluca_alebo_vypnuty_rezim_bez_modelu(self):
        self.cfg["OPENROUTER_API_KEY"] = ""
        self.assertTrue(self.run_at(2026, 11, 23, 8, 1))
        self.llm.assert_not_called()
        self.cfg["OPENROUTER_API_KEY"] = "k"
        set_state(self.c, "last_suhrn", ""); set_state(self.c, "mode", "off")
        self.assertTrue(self.run_at(2026, 11, 23, 9, 0))
        self.llm.assert_not_called()

    def test_en_jazyk(self):
        self.cfg["HOME_LANG"] = "en"
        with mock.patch.object(agent.i18n, "home_lang", return_value="en"):
            txt = self.run_at(2026, 11, 23, 8, 1)
        self.assertIn("Rooms (mean temperature)", txt)
        self.assertIn("Open alerts: none", txt)

    def test_bez_dat_neposiela_a_neopakuje(self):
        self.assertIsNone(agent.suhrn_run(self.cfg, self.c, ts(2026, 11, 30, 9, 0)))   # W47 má dáta, W48 nie -> súhrn za W48? nie, za W47
        # súhrn za týždeň bez dát (W46) sa označí ako hotový bez odoslania
        self.sent.clear()
        set_state(self.c, "last_suhrn", WEEK)
        self.assertIsNone(agent.suhrn_run(self.cfg, self.c, ts(2026, 12, 7, 9, 0)))     # za W49 nie sú dáta
        self.assertEqual(get_state(self.c, "last_suhrn"), "2026-W49")
        self.assertEqual(self.sent, [])

    def test_poplachy_a_zmeny_agenta(self):
        a = ts(2026, 11, 18, 10)
        self.c.execute("INSERT INTO actions(ts, entity, old, new, reason, applied) VALUES(?,?,?,?,?,1)", (a, "x", "1", "2", "r"))
        set_state(self.c, "alerts", '[{"kind":"ha","since":1,"text":"HA neodpovedá"}]')
        self.c.commit()
        txt = self.run_at(2026, 11, 23, 8, 1)
        self.assertIn("1 zmien nastavení", txt)
        self.assertIn("HA neodpovedá", txt)

    def test_api(self):
        import http.client, json, threading
        from http.server import ThreadingHTTPServer
        self.run_at(2026, 11, 23, 8, 1)
        c = common.adb(self.cfg)
        c.execute("INSERT INTO users(email, role, added_ts) VALUES('u@example.com','user',1)"); c.commit()
        admin, user = app.create_session(c), app.create_session(c, "user", "u@example.com")
        c.close()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), app.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))

        def get(path, tok):
            k = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            k.request("GET", path, None, {"Cookie": "%s=%s" % (app.COOKIE, tok), "X-Kurenie": "1"})
            r = k.getresponse(); raw = r.read(); k.close()
            return r.status, json.loads(raw.decode("utf-8"))
        with mock.patch.object(app, "load_cfg", lambda: dict(self.cfg)):
            s, o = get("/api/suhrn?tyzden=" + WEEK, admin)
            self.assertEqual((s, o["tyzden"], o["od"]), (200, WEEK, "2026-11-16"))
            self.assertIn("Izby", o["text"])
            self.assertEqual(get("/api/suhrn", admin)[1]["tyzden"], WEEK)
            self.assertEqual(get("/api/suhrn?tyzden=2026-W01", admin)[0], 404)
            self.assertEqual(get("/api/suhrn?tyzden=zle", admin)[0], 400)
            self.assertEqual(get("/api/suhrn", user)[0], 403)
            items = get("/api/reports", admin)[1]["items"]
            self.assertEqual(items[0]["kind"], "suhrn")


if __name__ == "__main__":
    unittest.main()
