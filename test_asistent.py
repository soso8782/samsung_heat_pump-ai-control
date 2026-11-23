#!/usr/bin/env python3
"""Asistent vyregulovania ventilov: teplá izba -> priškrtiť, studená -> otvoriť, čakajúca zmena -> počkať, vyregulované -> nič,
učenie z verdiktov, kumulatívne otáčky. Dočasné databázy so syntetickými vzorkami."""
import json, os, sqlite3, tempfile, time, unittest

os.environ.setdefault("DRY_RUN", "1")
import asistent, common, interventions as iv
from common import ROOMS, S

NOW = int(time.time()) // 3600 * 3600
A, B = ROOMS[1], ROOMS[2]


class Asistent(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        tc = os.path.join(d, "tc.db")
        self.db = sqlite3.connect(tc)
        self.db.executescript("CREATE TABLE samples(ts INTEGER, entity TEXT, value REAL);"
                              "CREATE TABLE events(ts INTEGER, kind TEXT, entity TEXT, old TEXT, new TEXT, text TEXT);"
                              "CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);")
        self.cfg = dict(common.load_cfg(), TC_DB=tc, AGENT_DB=os.path.join(d, "agent.db"))
        self.c = common.adb(self.cfg)

    def fill(self, offs, out=5.0, days=7):
        """Hodinové vzorky za posledných `days` dní: izby 23 + offs.get(izba, 0)."""
        for h in range(days * 24):
            ts = NOW - (h + 1) * 3600 + 60
            for r in ROOMS:
                if r in S:
                    self.db.execute("INSERT INTO samples VALUES(?,?,?)", (ts, S[r], 23.0 + offs.get(r, 0)))
            self.db.execute("INSERT INTO samples VALUES(?,?,?)", (ts, S["vonku"], out))
        self.db.commit()

    def done_step(self, room, turns, verdict, room_delta, age_h=100):
        iid, _ = iv.add_valve(self.c, room, turns, ts=NOW - age_h * 3600)
        res = {"hours": 48, "valve": {"verdict": verdict, "room_vs_others_delta": room_delta, "spread_delta": 0}}
        self.c.execute("UPDATE interventions SET status='hotove', result=?, result48=? WHERE id=?", (json.dumps(res), json.dumps(res), iid))
        self.c.commit()
        return iid

    def rec(self):
        return asistent.recommend(self.cfg, self.c, NOW, "sk")

    def test_teply_zatvor(self):
        self.fill({A: 1.2})
        r = self.rec()
        self.assertEqual(r["status"], "krok")
        self.assertEqual((r["step"]["room"], r["step"]["turns"]), (A, -0.25))
        self.assertIn("priškrtenie", r["reason"])
        self.assertIn("Očakávaný účinok", r["effect"])
        self.assertGreater(r["spread"], 1.0)

    def test_studena_otvor(self):
        self.fill({A: -1.2})
        r = self.rec()
        self.assertEqual((r["status"], r["step"]["room"], r["step"]["turns"]), ("krok", A, 0.25))
        self.assertTrue(any("Východisková poloha" in x for x in r["notes"]))

    def test_caka(self):
        self.fill({A: 1.2})
        iv.add_valve(self.c, B, -0.25, ts=NOW - 10 * 3600)
        r = self.rec()
        self.assertEqual(r["status"], "caka")
        self.assertIsNone(r["step"])
        self.assertEqual(r["wait_until"], NOW - 10 * 3600 + 48 * 3600)

    def test_vyregulovane(self):
        self.fill({A: 0.2})
        r = self.rec()
        self.assertEqual(r["status"], "vyregulovane")
        self.assertIsNone(r["step"])
        self.assertIn("netreba meniť", r["text"])

    def test_malo_dat(self):
        self.fill({A: 1.2}, days=0)
        self.assertEqual(self.rec()["status"], "malo_dat")

    def test_ucenie_horsie(self):
        self.fill({A: 1.2, B: 0.6})
        self.done_step(A, -0.25, "horsie", -0.1)
        r = self.rec()                                   # A sa znova nepriškrtí, na rade je B
        self.assertEqual((r["status"], r["step"]["room"]), ("krok", B))
        self.assertTrue(any("sa vynecháva" in x for x in r["notes"]))

    def test_ucenie_bez_zmeny_a_vacsi_krok(self):
        self.fill({A: 1.2})
        self.done_step(A, -0.25, "bez_zmeny", 0.0)       # takmer nulový účinok -> väčší krok
        r = self.rec()
        self.assertEqual((r["step"]["room"], r["step"]["turns"]), (A, -0.5))
        self.c.execute("DELETE FROM interventions"); self.c.commit()
        self.done_step(A, -0.25, "bez_zmeny", -0.15)     # malý, ale nie nulový účinok -> nezopakuje sa
        self.assertEqual(self.rec()["status"], "ziadny")

    def test_empiricky_ucinok(self):
        self.fill({A: 1.5})
        self.done_step(A, -0.25, "lepsie", -0.5)         # 2 °C na otáčku -> 0,5 °C na ¼
        self.done_step(B, -0.25, "lepsie", -0.5, age_h=200)
        r = self.rec()
        self.assertEqual(r["step"]["room"], A)
        self.assertIn("-0,50", r["effect"])

    def test_kumulativne_otacky(self):
        self.fill({A: -1.2})
        self.done_step(A, -0.5, "lepsie", -0.3, age_h=300)
        self.done_step(A, -0.25, "lepsie", -0.2, age_h=200)
        r = self.rec()
        row = [x for x in r["rooms"] if x["key"] == A][0]
        self.assertEqual(row["cum_turns"], -0.75)
        self.assertEqual((r["step"]["room"], r["step"]["turns"]), (A, 0.25))
        self.assertTrue(any("-0,75" in x for x in r["notes"]))

    def test_nespadne_pod_minimum(self):
        self.fill({A: 1.2})
        common.set_state(self.c, "targets", json.dumps({A: {"min": 24.1}}))   # A je teplá voči priemeru, ale po kroku by klesla pod minimum
        self.assertNotEqual((self.rec()["step"] or {}).get("room"), A)


if __name__ == "__main__":
    unittest.main()
