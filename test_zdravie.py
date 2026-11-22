#!/usr/bin/env python3
"""Zdravie čerpadla (zdravie.py): základ vs. nedávne dni so zhoršeným COP / prietokom / odmrazovaním / cyklovaním, málo dát = žiadne
zistenie, upozornenie raz a zrušenie, denné súčty z tc.db, tvar API, vstup agenta a súhrn. Syntetické dočasné databázy."""
import datetime as dt, http.client, json, os, shutil, sqlite3, tempfile, threading, time, unittest
from http.server import ThreadingHTTPServer
from unittest import mock

os.environ.setdefault("DRY_RUN", "1")
os.environ["TZ"] = "Europe/Bratislava"
time.tzset()
import agent, app, common, stats, zdravie

TODAY = dt.date(2026, 11, 22)
NOW = int(time.mktime((2026, 11, 22, 5, 0, 0, 0, 0, -1)))
ALL = set(zdravie.KODY)


def mid(d):
    return stats._midnight(d)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="t_zdravie_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.cfg = {"TC_DB": os.path.join(self.dir, "tc.db"), "STATS_DB": os.path.join(self.dir, "stats.db"),
                    "AGENT_DB": os.path.join(self.dir, "agent.db"), "OPENROUTER_API_KEY": "", "HA_TOKEN": ""}
        tc = sqlite3.connect(self.cfg["TC_DB"])
        tc.executescript("CREATE TABLE samples(ts INTEGER, entity TEXT, value REAL); CREATE INDEX samples_ts ON samples(ts);"
                         "CREATE TABLE events(ts INTEGER, kind TEXT, entity TEXT, old TEXT, new TEXT, text TEXT);"
                         "CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);")
        tc.commit()
        self.tc = tc
        self.addCleanup(tc.close)
        self.sent = []

    def notify(self, kind, title, msg):
        self.sent.append((kind, title, msg))
        return True, ""

    def fill(self, days_back=67, degr=frozenset(), first=None, recent_only=False):
        """Hodiny, cykly a denné súčty za `days_back` dní končiace včera; v posledných 7 dňoch sú zhoršené ukazovatele z `degr`."""
        con = zdravie._con(self.cfg)
        hrs, cyc, den = [], [], []
        for back in range(days_back, 0, -1):
            d = TODAY - dt.timedelta(days=back)
            rec = back <= 7
            if recent_only and not rec:
                continue
            ds = d.isoformat()
            for h in range(24):
                t = -4 + (h % 6) * 2 + 1.0                      # priehradky -4 .. 6 °C
                cop = (3.0 + 0.1 * t) * (0.8 if rec and "cop" in degr else 1.0)
                hrs.append({"ts": mid(d) + h * 3600, "n": 60, "vonku": t, "beh_min": 50.0, "kurenie_min": 50.0, "odmraz_min": 0.0,
                            "starty": 3 if rec and "cyklovanie" in degr else 1, "cop": round(cop, 3)})
            for k in range(24):
                t = -4 + (k % 6) * 2 + 1.0
                cyc.append((mid(d) + k * 3600 + 10, mid(d) + k * 3600 + 700, 6.0 if rec and "cyklovanie" in degr else 30.0, "kurenie", t))
            for tb in (-4, -2, 0, 2, 4, 6):
                dn, dd = (4, 12.0) if rec and "odmrazovanie" in degr else (2, 8.0)
                den.append((ds, "odm", tb, dn, dn * dd))
            den.append((ds, "_", 0, 1, 0))
            for fb in (30, 40, 50):
                den.append((ds, "prietok", fb, 300, 300 * (17.0 if rec and "prietok" in degr else 22.0)))
                den.append((ds, "dt", fb, 300, 300 * (3.0 if rec and "dt" in degr else 5.0)))
        stats.upsert_hourly(self.cfg, hrs)
        con.executemany("INSERT OR REPLACE INTO cycles(ts_start, ts_stop, minutes, mode, vonku) VALUES(?,?,?,?,?)", cyc)
        con.executemany("INSERT OR REPLACE INTO zdravie_den(den, ind, bin, n, s) VALUES(?,?,?,?,?)", den)
        con.commit()
        con.close()

    def clear(self):
        con = zdravie._con(self.cfg)
        d0 = (TODAY - dt.timedelta(days=7)).isoformat()
        con.execute("DELETE FROM zdravie_den WHERE den>=?", (d0,))
        con.execute("DELETE FROM hourly WHERE ts>=?", (mid(TODAY - dt.timedelta(days=7)),))
        con.execute("DELETE FROM cycles WHERE ts_start>=?", (mid(TODAY - dt.timedelta(days=7)),))
        con.commit()
        con.close()

    def run_z(self, now=NOW):
        return zdravie.run(self.cfg, now, self.notify)

    @staticmethod
    def ind(rep, kod):
        return next(i for i in rep["indikatory"] if i["kod"] == kod)


class Ukazovatele(Base):
    def test_bez_zhorsenia_vsetko_ok(self):
        self.fill()
        rep = zdravie.report(self.cfg, NOW)
        self.assertTrue(rep["dost_dat"])
        self.assertEqual(rep["stav"], "ok")
        for i in rep["indikatory"]:
            self.assertEqual(i["stav"], "ok", i["kod"])
            self.assertAlmostEqual(i["zmena_pct"], 0.0, delta=0.5)
            self.assertEqual(len(i["trend"]), 12)
        self.assertEqual(self.run_z()["zmeny"], {"nove": [], "zrusene": []})
        self.assertEqual(self.sent, [])

    def test_zhorsene_ukazovatele(self):
        self.fill(degr=ALL)
        rep = zdravie.report(self.cfg, NOW)
        self.assertEqual(rep["stav"], "crit")
        self.assertAlmostEqual(self.ind(rep, "cop")["zmena_pct"], -20.0, delta=0.5)
        self.assertAlmostEqual(self.ind(rep, "prietok")["zmena_pct"], -22.7, delta=0.5)
        self.assertAlmostEqual(self.ind(rep, "dt")["zmena_pct"], -40.0, delta=0.5)
        od = self.ind(rep, "odmrazovanie")
        self.assertAlmostEqual(od["pocet_zmena"], 100.0, delta=1)
        self.assertAlmostEqual(od["trvanie_zmena"], 50.0, delta=1)
        self.assertAlmostEqual(self.ind(rep, "cyklovanie")["zmena_pct"], 200.0, delta=1)
        for k in ALL:
            self.assertEqual(self.ind(rep, k)["stav"], "crit", k)
        # trend: posledný týždeň zhoršený, predošlé nie
        t = self.ind(rep, "cop")["trend"]
        self.assertAlmostEqual(t[-1], -20.0, delta=0.5)
        self.assertAlmostEqual(t[-2], 0.0, delta=0.5)

    def test_pod_prahom_je_ok_alebo_pozor(self):
        self.fill()
        con = zdravie._con(self.cfg)
        con.execute("UPDATE hourly SET cop=cop*0.94 WHERE ts>=?", (mid(TODAY - dt.timedelta(days=7)),))     # -6 % pri prahu 10 %
        con.commit(); con.close()
        self.assertEqual(self.ind(zdravie.report(self.cfg, NOW), "cop")["stav"], "warn")
        self.cfg["ZDRAVIE_COP_PCT"] = "5"
        self.assertEqual(self.ind(zdravie.report(self.cfg, NOW), "cop")["stav"], "crit")
        self.cfg["ZDRAVIE_COP_PCT"] = "30"
        self.assertEqual(self.ind(zdravie.report(self.cfg, NOW), "cop")["stav"], "ok")

    def test_pocasie_nie_je_zhorsenie(self):
        # nedávne dni sú výrazne chladnejšie (iné priehradky) – COP je horší absolútne, ale rovnaký pri rovnakej teplote
        self.fill()
        con = zdravie._con(self.cfg)
        con.execute("UPDATE hourly SET vonku=vonku-2, cop=cop-0.2 WHERE ts>=?", (mid(TODAY - dt.timedelta(days=7)),))
        con.commit(); con.close()
        rep = zdravie.report(self.cfg, NOW)
        self.assertEqual(self.ind(rep, "cop")["stav"], "ok")
        self.assertAlmostEqual(self.ind(rep, "cop")["zmena_pct"], 0.0, delta=0.5)

    def test_malo_dat_zaklad(self):
        self.fill(days_back=15, degr=ALL)
        rep = zdravie.report(self.cfg, NOW)
        self.assertFalse(rep["dost_dat"])
        self.assertEqual(rep["stav"], "nodata")
        self.assertTrue(all(i["stav"] == "nodata" for i in rep["indikatory"]))
        self.assertEqual(self.run_z()["zmeny"]["nove"], [])
        self.assertEqual(self.sent, [])

    def test_malo_hodin_v_priehradke(self):
        # dosť dní, ale nedávne okno má málo hodín v každej priehradke -> bez hodnotenia (nie falošný poplach)
        self.fill(degr=ALL)
        con = zdravie._con(self.cfg)
        for (ts,) in con.execute("SELECT ts FROM hourly WHERE ts>=?", (mid(TODAY - dt.timedelta(days=7)),)).fetchall():
            if time.localtime(ts).tm_hour >= 3:
                con.execute("DELETE FROM hourly WHERE ts=?", (ts,))
        con.execute("DELETE FROM zdravie_den WHERE den>=?", ((TODAY - dt.timedelta(days=7)).isoformat(),))
        con.execute("DELETE FROM cycles WHERE ts_start>=?", (mid(TODAY - dt.timedelta(days=7)),))
        con.commit(); con.close()
        self.cfg["ZDRAVIE_MIN_HODIN"] = "8"
        rep = zdravie.report(self.cfg, NOW)
        self.assertTrue(rep["dost_dat"])
        for i in rep["indikatory"]:
            self.assertEqual(i["stav"], "nodata", i["kod"])
            self.assertIsNone(i["zmena_pct"])
        self.assertEqual(self.run_z()["zmeny"]["nove"], [])

    def test_chybove_kody(self):
        self.fill()
        con = zdravie._con(self.cfg)
        con.execute("INSERT INTO faults(ts_start, ts_end, code, source, title, text, auto_key, created) VALUES(?,?,?,?,?,?,?,?)",
                    (NOW - 86400, NOW - 80000, "E911", "tc_error", "", "", "k1", 1))
        con.execute("INSERT INTO faults(ts_start, ts_end, code, source, title, text, auto_key, created) VALUES(?,?,?,?,?,?,?,?)",
                    (NOW - 40 * 86400, NOW - 39 * 86400, "E101", "tc_error", "", "", "k2", 1))
        con.commit(); con.close()
        rep = zdravie.report(self.cfg, NOW)
        self.assertEqual([x["kod"] for x in rep["chyby"]["kody"]], ["E911"])
        self.assertEqual(rep["stav"], "warn")


class Nalezy(Base):
    def test_jedno_upozornenie_a_zrusenie(self):
        self.fill(degr={"cop", "prietok"})
        r = self.run_z()
        self.assertEqual(sorted(r["zmeny"]["nove"]), ["cop", "prietok"])
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(k == "zdravie" for k, _t, _m in self.sent))
        self.assertIn("COP", self.sent[0][2] + self.sent[1][2])
        # opakovaný beh (ďalší deň) neposiela nič nové
        self.run_z(NOW + 3600)
        self.run_z(NOW + 7200)
        self.assertEqual(len(self.sent), 2)
        n = zdravie.nalezy(self.cfg)
        self.assertEqual(len(n["aktivne"]), 2)
        self.assertEqual(n["historia"], [])
        # návrat do normy: zhoršené dni nahradené normálnymi
        self.clear()
        self.fill(recent_only=True)
        r = self.run_z(NOW + 86400)
        self.assertEqual(sorted(r["zmeny"]["zrusene"]), ["cop", "prietok"])
        self.assertEqual(len(self.sent), 4)
        self.assertEqual({t for _k, t, _m in self.sent[2:]}, {"Zdravie čerpadla: v norme"})
        n = zdravie.nalezy(self.cfg)
        self.assertEqual(n["aktivne"], [])
        self.assertEqual(len(n["historia"]), 2)
        self.assertTrue(all(x["do"] for x in n["historia"]))
        self.run_z(NOW + 2 * 86400)
        self.assertEqual(len(self.sent), 4)

    def test_zlyhane_odoslanie_sa_zopakuje(self):
        self.fill(degr={"cop"})
        calls = []
        def bad(k, t, m):
            calls.append(1); return False, "chyba"
        zdravie.run(self.cfg, NOW, bad)
        self.assertEqual(len(calls), 1)
        zdravie.run(self.cfg, NOW + 86400, self.notify)
        self.assertEqual(len(self.sent), 1)
        zdravie.run(self.cfg, NOW + 2 * 86400, self.notify)
        self.assertEqual(len(self.sent), 1)

    def test_hysteréza_nezrusi_na_hrane(self):
        self.fill(degr={"cop"})
        self.run_z()
        con = zdravie._con(self.cfg)       # zlepšenie na -8 %: pod prahom 10 %, ale nad 70 % prahu -> nález ostáva
        con.execute("UPDATE hourly SET cop=cop/0.8*0.92 WHERE ts>=?", (mid(TODAY - dt.timedelta(days=7)),))
        con.commit(); con.close()
        self.assertEqual(self.run_z(NOW + 86400)["zmeny"]["zrusene"], [])
        self.assertEqual(len(zdravie.nalezy(self.cfg)["aktivne"]), 1)

    def test_vypnute(self):
        self.fill(degr=ALL)
        self.cfg["ZDRAVIE"] = "0"
        self.assertEqual(zdravie.api(self.cfg), {"enabled": False})
        self.assertEqual(zdravie.active_texts(self.cfg), [])
        c = common.adb(self.cfg)
        self.assertIsNone(zdravie.tick(self.cfg, c, NOW, self.notify))

    def test_tick_raz_denne_po_0410(self):
        self.fill(degr={"cop"})
        c = common.adb(self.cfg)
        ts = lambda h, m: int(time.mktime((2026, 11, 22, h, m, 0, 0, 0, -1)))
        self.assertIsNone(zdravie.tick(self.cfg, c, ts(4, 9), self.notify))
        self.assertIsNotNone(zdravie.tick(self.cfg, c, ts(4, 10), self.notify))
        self.assertIsNone(zdravie.tick(self.cfg, c, ts(9, 0), self.notify))
        self.assertEqual(len(self.sent), 1)


class DenneSucty(Base):
    def test_day_totals_z_tc_db(self):
        T = stats.TC
        d = TODAY - dt.timedelta(days=1)
        a = mid(d)
        rows = []
        for i in range(0, 600):                       # 10 h ustáleného kúrenia, 40 Hz, prietok 20, ΔT 5
            ts = a + 3600 + 60 * i
            rows += [(ts, T["freq"], 40.0), (ts, T["flow"], 20.0), (ts, T["dt"], 5.0), (ts, T["vonku"], 1.0)]
        self.tc.executemany("INSERT INTO samples VALUES(?,?,?)", rows)
        # odmrazovanie 12 min uprostred: minúty okolo sa nepočítajú
        s0 = a + 3600 + 300 * 60
        self.tc.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (s0, "stav", stats.DEFROST, "OFF", "ON", ""))
        self.tc.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (s0 + 720, "stav", stats.DEFROST, "ON", "OFF", ""))
        self.tc.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (a, "stav", stats.VALVE, "", "Room", ""))
        self.tc.commit()
        tot = zdravie.day_totals(self.cfg, d)
        self.assertEqual(tot[("odm", 0)], (1, 12.0))
        n, s = tot[("prietok", 40)]
        self.assertAlmostEqual(s / n, 20.0)
        self.assertLess(n, 600 - 12 - 10 - 15 + 2)
        self.assertGreater(n, 400)
        self.assertAlmostEqual(tot[("dt", 40)][1] / tot[("dt", 40)][0], 5.0)

    def test_update_days_dopln_a_idempotentne(self):
        self.fill(days_back=5)
        con = zdravie._con(self.cfg)
        con.execute("DELETE FROM zdravie_den")
        con.commit()
        n = zdravie.update_days(self.cfg, NOW, con)
        self.assertEqual(n, 5)
        self.assertEqual(con.execute("SELECT count(DISTINCT den) FROM zdravie_den WHERE ind='_'").fetchone()[0], 5)
        self.assertEqual(zdravie.update_days(self.cfg, NOW, con), 2)       # posledné 2 dni sa prepočítajú vždy
        con.close()


class ApiAgent(Base):
    def test_tvar_api(self):
        self.fill(degr={"cop"})
        self.run_z()
        o = zdravie.api(self.cfg)
        for k in ("enabled", "stav", "dost_dat", "okno", "tyzdne", "indikatory", "chyby", "nalezy", "prahy"):
            self.assertIn(k, o)
        self.assertEqual([i["kod"] for i in o["indikatory"]], list(zdravie.KODY))
        self.assertEqual(len(o["tyzdne"]), 12)
        for i in o["indikatory"]:
            for k in ("nazov", "popis", "stav", "zmena_pct", "prah_pct", "trend", "biny"):
                self.assertIn(k, i)
            self.assertEqual(len(i["trend"]), 12)
        self.assertEqual(o["okno"]["zaklad"], ["2026-09-16", "2026-11-15"])
        self.assertEqual(o["okno"]["nedavne"], ["2026-11-15", "2026-11-22"])
        self.assertEqual(o["nalezy"]["aktivne"][0]["kod"], "cop")
        self.assertIn("Skontroluj", o["indikatory"][0]["popis"] + o["nalezy"]["aktivne"][0]["text"])
        json.dumps(o)

    def test_api_http_len_admin(self):
        self.fill(degr={"cop"})
        cfg = dict(self.cfg)
        c = common.adb(cfg)
        c.execute("INSERT INTO users(email, role, added_ts) VALUES('u@example.com','user',1)"); c.commit()
        admin, user = app.create_session(c), app.create_session(c, "user", "u@example.com")
        c.close()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), app.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))

        def get(tok):
            k = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)
            k.request("GET", "/api/zdravie", None, {"Cookie": "%s=%s" % (app.COOKIE, tok), "X-Kurenie": "1"})
            r = k.getresponse(); raw = r.read(); k.close()
            return r.status, json.loads(raw.decode("utf-8"))
        with mock.patch.object(app, "load_cfg", lambda: dict(cfg)), mock.patch("zdravie.time.time", return_value=NOW):
            s, o = get(admin)
            self.assertEqual(s, 200)
            self.assertTrue(o["enabled"])
            self.assertEqual(len(o["indikatory"]), 5)
            self.assertEqual(get(user)[0], 403)

    def test_vstup_agenta_a_suhrn(self):
        self.fill(degr={"prietok"})
        self.run_z()
        self.assertEqual(len(zdravie.active_texts(self.cfg)), 1)
        self.assertIn("Prietok vody", zdravie.active_texts(self.cfg)[0])
        d = {"zdravie": zdravie.active_texts(self.cfg), "zmeny_agenta": 0, "zasahy": 0, "poplachy": []}
        txt = agent.suhrn_text(d, "sk")
        self.assertIn("Zdravie čerpadla: Prietok vody", txt)
        d["zdravie"] = []
        self.assertNotIn("Zdravie čerpadla", agent.suhrn_text(d, "sk"))
        en = zdravie.active_texts(self.cfg, "en")
        self.assertIn("Water flow", en[0])

    def test_active_texts_bez_db_nevytvori_subor(self):
        cfg = dict(self.cfg, STATS_DB=os.path.join(self.dir, "nema.db"))
        self.assertEqual(zdravie.active_texts(cfg), [])
        self.assertFalse(os.path.exists(cfg["STATS_DB"]))


if __name__ == "__main__":
    unittest.main()
