#!/usr/bin/env python3
"""Denník vyregulovania ventilov: zápis, vyhodnotenie 24/48 h (rozptyl izieb, izba voči ostatným, počasie), varovanie, súhrn.
Dočasné databázy so syntetickými vzorkami."""
import os, sqlite3, tempfile, time, unittest
from unittest import mock

os.environ.setdefault("DRY_RUN", "1")
import agent, common, interventions as iv
from common import ROOMS, S, set_state

T0 = int(time.time()) // 3600 * 3600 - 120 * 3600      # čas zmeny: pred 5 dňami (obe okná sú v minulosti)
ROOM = ROOMS[1]


class Ventily(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.tc = os.path.join(d, "tc.db")
        c = sqlite3.connect(self.tc)
        c.executescript("CREATE TABLE samples(ts INTEGER, entity TEXT, value REAL);"
                        "CREATE TABLE events(ts INTEGER, kind TEXT, entity TEXT, old TEXT, new TEXT, text TEXT);"
                        "CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);")
        self.db = c
        self.cfg = dict(common.load_cfg(), TC_DB=self.tc, AGENT_DB=os.path.join(d, "agent.db"))
        self.c = common.adb(self.cfg)

    def fill(self, a, b, base=21.0, room_off=None, out=3.0, spread=1.0):
        """Hodinové vzorky [a,b): izby base + i*spread/(n-1); `room_off` = posun ROOM."""
        n = len(ROOMS)
        for h in range((b - a) // 3600):
            for i, r in enumerate(ROOMS):
                if r in S:
                    v = base + i * spread / (n - 1) + (room_off if r == ROOM and room_off else 0)
                    self.db.execute("INSERT INTO samples VALUES(?,?,?)", (a + h * 3600 + 60, S[r], v))
            self.db.execute("INSERT INTO samples VALUES(?,?,?)", (a + h * 3600 + 60, S["vonku"], out))
        self.db.commit()

    def test_pridanie_a_varovanie(self):
        i1, w1 = iv.add_valve(self.c, ROOM, -0.25, None, "skúška")
        self.assertIsNone(w1)
        r = iv.get(self.c, i1)
        self.assertEqual((r["kind"], r["room"], r["status"]), ("ventil", ROOM, "caka"))
        self.assertEqual(r["detail"], {"label": "VK 33", "turns": -0.25, "change": "−¼ ot.", "note": "skúška"})
        self.assertIn("priškrtený", r["text"])
        self.assertEqual(iv.direction(r["text"]), -1)
        i2, w2 = iv.add_valve(self.c, ROOMS[2], 0.5, "VK 34")          # druhá zmena do 24 h: varovanie, ale zapíše sa
        self.assertIn("nie je vyhodnotená", w2)
        self.assertEqual(iv.get(self.c, i2)["detail"]["change"], "+½ ot.")
        iv.cancel(self.c, i1); iv.cancel(self.c, i2)
        self.assertIsNone(iv.add_valve(self.c, ROOM, 1)[1])            # zrušené sa nepočítajú
        self.assertEqual(iv.fmt_turns(-0.75), "−¾ ot.")
        self.assertEqual(iv.fmt_turns(1.5), "+1½ ot.")
        self.assertEqual(iv.fmt_turns(-0.3), "−0,3 ot.")

    def test_vyhodnotenie_24_a_48(self):
        iid, _ = iv.add_valve(self.c, ROOM, -0.25, ts=T0)
        # pred: rozptyl 1,0; po: rozptyl 0,5 a dotknutá izba o 0,4 teplejšia voči zvyšku; počasie rovnaké
        self.fill(T0 - 48 * 3600, T0, spread=1.0)
        self.fill(T0, T0 + 48 * 3600, spread=1.0, room_off=0.4)
        # druhé okno 48 h: do 24 h vyrovnanejšie, potom rovnaké → rozptyl sa líši medzi 24 a 48 h
        self.db.execute("DELETE FROM samples WHERE ts>=?", (T0 + 24 * 3600,))
        self.fill(T0 + 24 * 3600, T0 + 48 * 3600, spread=0.4, room_off=0.4)
        now = T0 + 25 * 3600
        self.assertEqual(iv.evaluate_due(self.cfg, self.c, now), [iid])
        r = iv.get(self.c, iid)
        self.assertEqual(r["status"], "hotove")
        self.assertIsNone(r["result48"])                                 # 48 h ešte nie
        v = r["result"]["valve"]
        self.assertEqual(r["result"]["hours"], 24)
        self.assertEqual(v["spread_before"], 1.0)
        self.assertAlmostEqual(v["spread_after"], 1.0, 1)
        self.assertEqual(v["verdict"], "bez_zmeny")
        self.assertGreater(v["room_vs_others_delta"], 0)
        self.assertFalse(v["weather_differs"])
        self.assertEqual(iv.evaluate_due(self.cfg, self.c, T0 + 40 * 3600), [])
        self.assertIsNone(iv.get(self.c, iid)["result48"])
        iv.evaluate_due(self.cfg, self.c, T0 + 49 * 3600)
        r = iv.get(self.c, iid)
        self.assertEqual(r["result48"]["hours"], 48)
        v48 = r["result48"]["valve"]
        self.assertAlmostEqual(v48["spread_after"], 0.7, 1)               # (24 h × 1,0 + 24 h × 0,4) / 48
        self.assertEqual(v48["spread_delta"], round(v48["spread_after"] - v48["spread_before"], 2))
        self.assertEqual(v48["verdict"], "lepsie")
        self.assertIn("lepšie", r["result48"]["verdict_text"])

    def test_pocasie_a_horsie(self):
        iid, _ = iv.add_valve(self.c, ROOM, 0.25, ts=T0)
        self.fill(T0 - 48 * 3600, T0, spread=0.5, out=5.0)
        self.fill(T0, T0 + 48 * 3600, spread=1.0, out=-1.0)
        iv.evaluate_due(self.cfg, self.c, T0 + 49 * 3600)
        r = iv.get(self.c, iid)
        for res in (r["result"], r["result48"]):
            v = res["valve"]
            self.assertEqual(v["verdict"], "horsie")
            self.assertTrue(v["weather_differs"])
            self.assertEqual(v["vonku_delta"], -6.0)
            self.assertIn("počasie sa líšilo", res["verdict_text"])

    def test_nedostatok_dat_a_ine_druhy(self):
        iid, _ = iv.add_valve(self.c, ROOM, -0.25, ts=T0)
        other = iv.add(self.c, "user_manual", "vetranie", None, "vetral som", ts=T0)
        self.fill(T0 - 24 * 3600, T0 + 24 * 3600)
        self.assertEqual(sorted(iv.evaluate_due(self.cfg, self.c, T0 + 25 * 3600)), sorted([iid, other]))
        o = iv.get(self.c, other)
        self.assertNotIn("valve", o["result"])                           # ostatné druhy: pôvodná logika
        self.assertEqual(o["result"]["delta"]["spread"], 0.0)
        iv.evaluate_due(self.cfg, self.c, T0 + 49 * 3600)                # 48 h okno nemá dosť dát, ešte čakáme
        self.assertIsNone(iv.get(self.c, iid)["result48"])
        iv.evaluate_due(self.cfg, self.c, T0 + 48 * 3600 + iv.GIVE_UP_S + 10)   # po čase sa uzavrie ako nedostatok dát
        r48 = iv.get(self.c, iid)["result48"]
        self.assertIn("Nedostatok dát", r48["verdict_text"])
        self.assertIsNone(iv.get(self.c, other)["result48"])

    def test_sumar_a_vstup_modelu(self):
        iid, _ = iv.add_valve(self.c, ROOM, -0.25, ts=T0)
        self.fill(T0 - 48 * 3600, T0, spread=1.0)
        self.fill(T0, T0 + 48 * 3600, spread=0.5)
        iv.evaluate_due(self.cfg, self.c, T0 + 49 * 3600)
        self.assertIn("48 h", iv.llm_text(self.c, T0 + 50 * 3600))
        line = iv.valve_line(iv.get(self.c, iid))
        self.assertIn("VK 33", line); self.assertIn("lepšie", line); self.assertIn("48 h", line)
        # týždenný súhrn: dáta za týždeň obsahujú zmenu ventilu s 48 h výsledkom
        wk = time.strftime("%G-W%V", time.localtime(T0))
        mon, sun, a, b = agent.suhrn_bounds(wk)
        iv2, _ = iv.add_valve(self.c, ROOMS[2], 0.5, ts=a + 3600)
        with mock.patch("stats.get_range", side_effect=RuntimeError), mock.patch("spotreba.report", side_effect=RuntimeError):
            self.fill(a, b, spread=1.0)
            d = agent.suhrn_data(self.cfg, self.c, wk)
        self.assertEqual(len(d["ventily"]), 2 if a <= T0 < b else 1)
        txt = agent.suhrn_text(d)
        self.assertIn("Ventily:", txt)
        self.assertIn("VK 33", txt)


if __name__ == "__main__":
    unittest.main()
