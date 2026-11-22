#!/usr/bin/env python3
"""Testy kúrenia a TÚV podľa tarify (tarifa.py, dhw.pick_window_tarifa): bloky VT, predhrev / útlm, advise vs control, návrat po VT,
poistka minima izby, A/B test, predvolene vypnuté, vyhodnotenie VT/NT, okno TÚV v NT a v teple. Dočasné DB, ESP a predpoveď sú napodobené.
Spustenie: python test_tarifa.py   (v priečinku kurenia)"""
import datetime as dt, json, os, sqlite3, sys, tempfile, time, unittest
from unittest import mock

os.environ.setdefault("DRY_RUN", "0")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent, common, dhw, stats, tarifa
from common import S, TARGET_ROOMS

D = dt.date(2026, 11, 23)          # pondelok, zima
VT = "06:00-08:00,17:00-19:00"
ENT = agent.ROOM_TARGET_ENT


def T(h, m=0, d=D):
    return int(time.mktime((d.year, d.month, d.day, h, m, 0, 0, 0, -1)))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        n = self.tmp.name
        self.cfg = {"TC_DB": os.path.join(n, "tc.db"), "AGENT_DB": os.path.join(n, "agent.db"), "STATS_DB": os.path.join(n, "stats.db"),
                    "ESP_URL": "http://x", "DRY_RUN": "0", "TARIFA_KURENIE": "1", "TARIFA_VT_INTERVALY": VT, "LAT": "48.1", "LON": "17.1"}
        tc = sqlite3.connect(self.cfg["TC_DB"])
        tc.executescript("CREATE TABLE samples(ts INTEGER, entity TEXT, value REAL); CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);")
        tc.commit(); tc.close()
        self.c = common.adb(self.cfg)
        common.set_state(self.c, "mode", "control")
        self.esp = {ENT: 22.0}
        self.writes = []
        self.fc_temp = 2.0
        self.patches = [
            mock.patch.object(agent, "esp_set_number", side_effect=self._set),
            mock.patch.object(agent, "esp_get", side_effect=lambda cfg, dom, name, timeout=6: {"value": self.esp.get(name)}),
            mock.patch.object(agent, "room_target_current", side_effect=lambda cfg: self.esp.get(ENT)),
            mock.patch.object(agent, "_live_value"),
            mock.patch.object(tarifa, "fetch_forecast", side_effect=lambda cfg: [(T(h), self.fc_temp) for h in range(24)]),
            mock.patch.object(tarifa.abtest, "active", return_value=False),
            mock.patch.object(time, "sleep"),
        ]
        for p in self.patches:
            p.start()
        self.rooms(24.0)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.c.close()
        self.tmp.cleanup()

    def _set(self, cfg, name, value):
        self.writes.append((name, float(value)))
        self.esp[name] = float(value)

    def rooms(self, v, now=None, only=None):
        """Teploty izieb (všetky = v, alebo slovník kľúč -> hodnota) tesne pred `now`."""
        now = now or T(12)
        tc = sqlite3.connect(self.cfg["TC_DB"])
        tc.execute("DELETE FROM samples")
        for k in TARGET_ROOMS:
            tc.execute("INSERT INTO samples VALUES(?,?,?)", (int(now) - 60, S[k], (only or {}).get(k, v)))
        tc.commit(); tc.close()

    def tick(self, h, m=0):
        now = T(h, m)
        self.rooms(self.rv if hasattr(self, "rv") else 24.0, now)
        return tarifa.tick(self.cfg, self.c, now)

    def actions(self):
        return self.c.execute("SELECT entity, old, new, reason, applied FROM actions ORDER BY rowid").fetchall()


class Bloky(Base):
    def test_intervaly(self):
        self.assertEqual(tarifa.parse_intervals("06:00-08:00, 17:00-19:00;xx;5-6"), [(360, 480), (1020, 1140)])
        self.assertEqual(tarifa.parse_intervals(""), [])
        self.assertFalse(tarifa.known({}))
        self.assertTrue(tarifa.known(self.cfg))

    def test_vt_detekcia(self):
        self.assertTrue(tarifa.is_vt(self.cfg, T(6)))
        self.assertTrue(tarifa.is_vt(self.cfg, T(7, 59)))
        self.assertFalse(tarifa.is_vt(self.cfg, T(8)))
        self.assertTrue(tarifa.is_vt(self.cfg, T(18)))
        self.assertFalse(tarifa.is_vt(self.cfg, T(12)))
        self.assertTrue(tarifa.overlaps_vt(self.cfg, T(4), T(6, 1)))
        self.assertFalse(tarifa.overlaps_vt(self.cfg, T(9), T(17)))

    def test_cez_polnoc(self):
        cfg = {"TARIFA_VT_INTERVALY": "22:00-02:00"}
        self.assertTrue(tarifa.is_vt(cfg, T(23)))
        self.assertTrue(tarifa.is_vt(cfg, T(1)))
        self.assertFalse(tarifa.is_vt(cfg, T(3)))

    def test_faza(self):
        self.assertEqual(tarifa.phase_at(self.cfg, T(3))[0], "norm")
        self.assertEqual(tarifa.phase_at(self.cfg, T(4))[0], "pred")
        self.assertEqual(tarifa.phase_at(self.cfg, T(5, 59))[0], "pred")
        ph, blk = tarifa.phase_at(self.cfg, T(7))
        self.assertEqual((ph, blk), ("vt", (T(6), T(8))))
        self.assertEqual(tarifa.phase_at(self.cfg, T(8))[0], "norm")
        self.assertEqual(tarifa.phase_at(self.cfg, T(15))[0], "pred")

    def test_nezname(self):
        self.assertEqual(tarifa.phase_at({}, T(7))[0], "norm")
        self.assertEqual(tarifa.plan({}, self.c, T(7))["delta"], 0.0)


class Plan(Base):
    def test_rozvrh(self):
        self.rooms(24.0, T(4))
        self.assertEqual(tarifa.plan(self.cfg, self.c, T(4))["delta"], 0.5)
        self.rooms(24.0, T(7))
        self.assertEqual(tarifa.plan(self.cfg, self.c, T(7))["delta"], -0.5)
        self.assertEqual(tarifa.plan(self.cfg, self.c, T(10))["delta"], 0.0)

    def test_teplo_preskoci_predhrev(self):
        self.fc_temp = 15.0
        self.assertEqual(tarifa.plan(self.cfg, self.c, T(4))["delta"], 0.0)
        self.assertEqual(tarifa.plan(self.cfg, self.c, T(4))["why"], "teplo")

    def test_bez_predpovede_nepredhrieva(self):
        with mock.patch.object(tarifa, "fetch_forecast", side_effect=OSError("x")):
            self.assertEqual(tarifa.plan(self.cfg, self.c, T(4))["delta"], 0.0)

    def test_leto(self):
        common.set_state(self.c, "season", "leto")
        self.assertEqual(tarifa.plan(self.cfg, self.c, T(4))["delta"], 0.0)

    def test_limit_a_krok(self):
        cfg = dict(self.cfg, TARIFA_PREDKURENIE_C="3", TARIFA_UTLM_C="0.7")
        self.assertEqual(tarifa.pre_c(cfg), 1.0)
        self.assertEqual(tarifa.cut_c(cfg), 0.5)


class Riadenie(Base):
    def test_predvolene_vypnute(self):
        cfg = {k: v for k, v in self.cfg.items() if k != "TARIFA_KURENIE"}
        self.assertEqual(tarifa.tick(cfg, self.c, T(4)), "")
        self.assertEqual(tarifa.tick(dict(cfg, TARIFA_KURENIE="0"), self.c, T(7)), "")
        self.assertEqual(self.writes, [])
        self.assertEqual(self.actions(), [])
        self.assertFalse(tarifa.status(cfg, self.c)["enabled"])

    def test_control_cely_cyklus(self):
        self.tick(3)
        self.assertEqual(self.writes, [])
        self.tick(4)                                   # predhrev +0,5
        self.assertEqual(self.writes, [(ENT, 22.5)])
        self.tick(5)                                   # už nastavené, nič
        self.assertEqual(len(self.writes), 1)
        self.tick(6)                                   # VT: 22,0 - 0,5
        self.assertEqual(self.writes[-1], (ENT, 21.5))
        self.tick(7)
        self.assertEqual(len(self.writes), 2)
        self.tick(8)                                   # po VT späť na 22,0
        self.assertEqual(self.writes[-1], (ENT, 22.0))
        self.assertEqual(self.esp[ENT], 22.0)
        self.assertIsNone(tarifa._state_active(self.c))
        self.tick(9)
        self.assertEqual(len(self.writes), 3)
        a = self.actions()
        self.assertEqual([x[2] for x in a], [22.5, 21.5, 22.0])
        self.assertTrue(all(x[3].startswith("tarifa") and x[4] == 1 and x[0] == ENT for x in a))

    def test_advise_nic_nezapisuje(self):
        common.set_state(self.c, "mode", "advise")
        self.tick(4)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.actions(), [])
        st = tarifa.status(self.cfg, self.c, T(4))
        self.assertEqual(st["advice"]["phase"], "pred")
        self.assertIn("+0,5", st["advice"]["text"].replace("+0.5", "+0,5"))
        self.assertEqual(self.c.execute("SELECT count(*) FROM runs WHERE status='info'").fetchone()[0], 1)
        self.tick(4, 30)                               # tá istá fáza: bez ďalšieho záznamu
        self.assertEqual(self.c.execute("SELECT count(*) FROM runs WHERE status='info'").fetchone()[0], 1)
        self.tick(6)
        self.assertEqual(self.writes, [])
        self.assertEqual(tarifa.status(self.cfg, self.c, T(6))["advice"]["delta"], -0.5)

    def test_agent_off(self):
        common.set_state(self.c, "mode", "off")
        self.tick(4)
        self.assertEqual(self.writes, [])

    def test_ab_test(self):
        with mock.patch.object(tarifa.abtest, "active", return_value=True):
            self.tick(4)
            self.tick(7)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.actions(), [])

    def test_ab_test_pocas_predhrevu_vrati(self):
        self.tick(4)
        self.assertEqual(self.esp[ENT], 22.5)
        with mock.patch.object(tarifa.abtest, "active", return_value=True):
            self.tick(5)
        self.assertEqual(self.esp[ENT], 22.0)

    def test_izba_pod_minimom_utlm_nerobi(self):
        mn = common.get_targets(self.c)[TARGET_ROOMS[0]]["min"]
        self.rv = mn + 0.2                              # pod min + 0,3
        self.tick(6)
        self.assertEqual(self.writes, [])
        self.rv = mn + 0.3
        self.tick(6, 10)
        self.assertEqual(self.writes[-1], (ENT, 21.5))

    def test_jedna_izba_pod_minimom(self):
        mn = common.get_targets(self.c)[TARGET_ROOMS[1]]["min"]
        self.rooms(25.0, T(7), only={TARGET_ROOMS[1]: mn + 0.1})
        self.assertEqual(tarifa.tick(self.cfg, self.c, T(7)), tarifa.tr("srv.tarifa.t_nic", "sk"))
        self.assertEqual(self.writes, [])

    def test_utlm_sa_zrusi_ked_izba_klesne(self):
        self.tick(6)
        self.assertEqual(self.esp[ENT], 21.5)
        self.rv = common.get_targets(self.c)[TARGET_ROOMS[0]]["min"] + 0.1
        self.tick(6, 30)
        self.assertEqual(self.esp[ENT], 22.0)           # zrušené, späť na pôvodnú
        self.assertEqual(self.actions()[-1][2], 22.0)
        self.assertIn("izba", self.actions()[-1][3])

    def test_bez_dat_izieb_utlm_nerobi(self):
        tc = sqlite3.connect(self.cfg["TC_DB"]); tc.execute("DELETE FROM samples"); tc.commit(); tc.close()
        tarifa.tick(self.cfg, self.c, T(7))
        self.assertEqual(self.writes, [])

    def test_rucna_zmena_sa_neprepisuje(self):
        self.tick(4)
        self.esp[ENT] = 23.5                            # používateľ zmenil ručne
        self.tick(8)
        self.assertEqual(self.esp[ENT], 23.5)
        self.assertIsNone(tarifa._state_active(self.c))

    def test_teplo_ziadny_zapis(self):
        self.fc_temp = 16.0
        self.tick(4); self.tick(6)
        self.assertEqual(self.writes, [])

    def test_rozsah_profilu(self):
        self.esp[ENT] = agent.ROOM_TARGET_MIN           # nižšie ako minimum sa neznižuje
        self.tick(6)
        self.assertEqual(self.writes, [])
        self.esp[ENT] = agent.ROOM_TARGET_MAX           # vyššie ako maximum sa nepredhrieva
        self.tick(4)
        self.assertEqual(self.writes, [])

    def test_neuspesny_zapis_retry(self):
        with mock.patch.object(agent, "esp_set_number", side_effect=OSError("boom")):
            self.tick(4)
            self.assertEqual(self.actions()[-1][4], 0)
            n = len(self.actions())
            self.tick(4, 1)                             # do 5 min sa neopakuje
            self.assertEqual(len(self.actions()), n)
        self.assertIsNone(tarifa._state_active(self.c))

    def test_dry_run(self):
        self.cfg["DRY_RUN"] = "1"
        with mock.patch.object(agent, "esp_set_number") as w:
            self.tick(4)
            self.tick(8)
        self.assertEqual(w.call_count, 2)
        self.assertIsNone(tarifa._state_active(self.c))
        self.assertEqual([x[2] for x in self.actions()], [22.5, 22.0])


class Vyhodnotenie(Base):
    def put_day(self, d, vt, nt):
        a, b = stats._bounds(d)
        rows = []
        for h in range(a, b, 3600):
            v = vt if tarifa.is_vt(self.cfg, h + 1800) else nt
            rows.append({"ts": h, "kwh": v, "kwh_cal": v})
        stats.upsert_hourly(self.cfg, rows)

    def test_podiel_a_uspora(self):
        now = T(12)
        common.set_state(self.c, "tarifa_od", (D - dt.timedelta(days=5)).isoformat())
        for k in range(1, 11):
            d = D - dt.timedelta(days=k)
            self.put_day(d, 0.5, 0.25) if d.isoformat() < (D - dt.timedelta(days=5)).isoformat() else self.put_day(d, 0.1, 0.25)
        v = tarifa.evaluate(self.cfg, self.c, now)
        self.assertEqual((v["dni_pred"], v["dni_po"]), (5, 5))
        # VT 4 h/deň (06-08, 17-19), NT 20 h/deň
        self.assertAlmostEqual(v["podiel_pred"], 2.0 / (2.0 + 5.0), places=3)
        self.assertAlmostEqual(v["podiel_po"], 0.4 / (0.4 + 5.0), places=3)
        self.assertGreater(v["uspora_eur"], 0)
        exp = (v["podiel_pred"] - v["podiel_po"]) * (v["kwh_vt_po"] + v["kwh_nt_po"]) * tarifa._price_gap(self.cfg, v["od"])
        self.assertAlmostEqual(v["uspora_eur"], exp, places=1)
        self.assertEqual(len(json.loads(common.get_state(self.c, "tarifa_dni"))), 10)   # uložené po dňoch

    def test_malo_dni_bez_uspory(self):
        common.set_state(self.c, "tarifa_od", (D - dt.timedelta(days=1)).isoformat())
        for k in range(1, 6):
            self.put_day(D - dt.timedelta(days=k), 0.2, 0.2)
        self.assertIsNone(tarifa.evaluate(self.cfg, self.c, T(12))["uspora_eur"])

    def test_status_v_api(self):
        st = tarifa.status(self.cfg, self.c, T(7))
        self.assertTrue(st["known"] and st["enabled"])
        self.assertEqual(st["phase"], "vt")
        self.assertEqual(st["next_block"], {"start": T(6), "end": T(8)})


class TuvOkno(Base):
    def temps(self, f):
        return {h: f(h) for h in range(24)}

    def test_nt_a_najteplejsie(self):
        cfg = dict(self.cfg, TUV_TARIFA="1")
        t = self.temps(lambda h: 5.0 + (6 if h == 16 else 0) + (3 if h == 11 else 0))   # 16-17 je teplejšie, ale prekrýva VT 17-19
        w = dhw.pick_window_tarifa(cfg, t, D.isoformat())
        self.assertEqual(w[2], "nt_teplo")
        self.assertEqual(w[:2], ("10:00", "12:00"))       # 11:00 teplejší: okno 10-12 aj 11-13 majú rovnaký priemer, remíza = skôr
        self.assertFalse(tarifa.overlaps_vt(cfg, T(int(w[0][:2])), T(int(w[1][:2]))))

    def test_vyhne_sa_vt_v_bezne_okne(self):
        cfg = dict(self.cfg, TUV_TARIFA="1", TARIFA_VT_INTERVALY="09:00-17:00")   # cez celé 10..16: bežné okno nemá NT
        t = self.temps(lambda h: 8.0 if h < 9 else 3.0)
        w = dhw.pick_window_tarifa(cfg, t, D.isoformat())
        self.assertEqual(w[2], "nt")
        self.assertEqual(w[:2], ("00:00", "02:00"))
        self.assertEqual(dhw.pick_window(t), ("10:00", "12:00"))   # pôvodné správanie zostáva

    def test_bez_nt_okna(self):
        cfg = dict(self.cfg, TARIFA_VT_INTERVALY="00:00-24:00")
        self.assertIsNone(dhw.pick_window_tarifa(cfg, self.temps(lambda h: 5.0), D.isoformat()))

    def _ensure(self, cfg, temps):
        with mock.patch.object(dhw, "fetch_temps", return_value=temps):
            return dhw.ensure_window(cfg, self.c, T(7))

    def test_ensure_window_zapnute(self):
        w = self._ensure(dict(self.cfg, TUV_TARIFA="1"), self.temps(lambda h: 5.0 + (2 if h == 13 else 0)))
        self.assertEqual((w["start"], w["end"], w["reason"]), ("12:00", "14:00", "nt_teplo"))

    def test_ensure_window_vypnute_je_ako_predtym(self):
        cfg = dict(self.cfg, TARIFA_VT_INTERVALY="10:00-14:00")   # TUV_TARIFA chýba
        w = self._ensure(cfg, self.temps(lambda h: 5.0 + (2 if h == 11 else 0)))
        self.assertEqual((w["start"], w["end"]), ("10:00", "12:00"))
        self.assertNotIn("reason", w)

    def test_ensure_window_tarifa_nezname_fallback(self):
        cfg = dict(self.cfg, TUV_TARIFA="1", TARIFA_VT_INTERVALY="")
        w = self._ensure(cfg, self.temps(lambda h: 5.0 + (2 if h == 13 else 0)))
        self.assertEqual((w["start"], w["end"]), ("12:00", "14:00"))   # pôvodný pick_window
        self.assertFalse(dhw.tuv_tarifa(cfg))

    def test_ensure_window_bez_nt_fallback_na_teplo(self):
        cfg = dict(self.cfg, TUV_TARIFA="1", TARIFA_VT_INTERVALY="00:00-24:00")
        w = self._ensure(cfg, self.temps(lambda h: 5.0 + (2 if h == 13 else 0)))
        self.assertEqual((w["start"], w["end"], w["reason"]), ("12:00", "14:00", "teplo"))

    def test_jedno_okno_za_den(self):
        cfg = dict(self.cfg, TUV_TARIFA="1")
        w1 = self._ensure(cfg, self.temps(lambda h: 5.0))
        w2 = self._ensure(cfg, self.temps(lambda h: 9.0))
        self.assertEqual(w1, w2)                       # okno sa vyberá raz za deň, dĺžka 2 h
        self.assertEqual(int(w1["end"][:2]) - int(w1["start"][:2]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
