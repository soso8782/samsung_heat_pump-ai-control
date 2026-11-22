#!/usr/bin/env python3
"""Testy odhadu nákladov a prehľadu sezóny (naklady.py): fit HDD, predpoveď s mock počasím, hrany fakturačného obdobia, hranice sezóny,
porovnanie s predošlou sezónou, týždenný odstavec, stavy bez dát. Dočasné databázy; nič sa nepripája von."""
import datetime as dt, math, os, sqlite3, sys, tempfile, time, unittest

os.environ["TZ"] = "Europe/Bratislava"
time.tzset()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common, ha_cost, naklady, stats, vonku
from common import get_state, set_state

NOW = int(time.mktime((2026, 11, 22, 12, 0, 0, 0, 0, -1)))
R1, R2 = vonku.IZBY[0], vonku.IZBY[1]


def mid(y, m, d):
    return int(time.mktime((y, m, d, 0, 0, 0, 0, 0, -1)))


def temp(d):
    """syntetická denná teplota: ročný chod, v januári ~ -2, v júli ~ 20"""
    return 9.0 - 11.0 * math.cos(2 * math.pi * (d.timetuple().tm_yday - 15) / 365.0)


def rows_for(d0, d1, a, b, tf=temp, q_cop=3.5, skip=lambda d: False):
    """hodinové riadky pre dni d0..d1 vrátane; denná spotreba = a + b·HDD rovnomerne rozložená"""
    out = []
    d = d0
    while d <= d1:
        if not skip(d):
            t = tf(d)
            kwh = a + b * max(0.0, 15.0 - t)
            for h in range(24):
                ts = int(time.mktime((d.year, d.month, d.day, h, 0, 0, 0, 0, -1)))
                out.append({"ts": ts, "kwh_cal": kwh / 24.0, "vonku": t, "q_kwh": kwh / 24.0 * q_cop, "starty": 1, "odmraz_min": 2.0,
                            R1: 21.0, R2: 22.0})
        d += dt.timedelta(days=1)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {"STATS_DB": os.path.join(self.tmp.name, "stats.db"), "BILLING_START": "11-01",
                    "AGENT_DB": os.path.join(self.tmp.name, "agent.db")}

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, rows):
        stats.upsert_hourly(self.cfg, rows)

    def hist(self, a=3.0, b=1.2):
        """posledných 70 dní pred 22. 11. 2026"""
        self.put(rows_for(dt.date(2026, 9, 13), dt.date(2026, 11, 21), a, b))


class Fit(Base):
    def test_fit_presny(self):
        self.hist()
        days = naklady._by_day(naklady._rows(self.cfg, 0, NOW))
        m = naklady.fit(days)
        self.assertAlmostEqual(m["a"], 3.0, places=4)
        self.assertAlmostEqual(m["b"], 1.2, places=4)
        self.assertLess(m["sigma"], 1e-6)

    def test_fit_zaporna_smernica_sa_orezava(self):
        d = {"2026-11-%02d" % i: {"kwh": 10.0 - i * 0.1, "nk": 24, "tsum": (2.0 + i) * 24, "nt": 24, "q": 0} for i in range(1, 15)}
        m = naklady.fit(d)
        self.assertGreaterEqual(m["b"], 0.0)
        self.assertGreaterEqual(m["a"], 0.0)

    def test_fit_malo_dni(self):
        d = {"2026-11-%02d" % i: {"kwh": 10.0, "nk": 24, "tsum": 48.0, "nt": 24, "q": 0} for i in range(1, 6)}
        self.assertIsNone(naklady.fit(d))

    def test_neuplne_dni_sa_nepouzivaju(self):
        self.put(rows_for(dt.date(2026, 10, 1), dt.date(2026, 11, 21), 3.0, 1.2, skip=lambda d: d.day % 2 == 0))
        days = naklady._by_day(naklady._rows(self.cfg, 0, NOW))
        for i in range(10):   # polovičné dni s inou spotrebou nesmú pokaziť fit
            days["2026-12-%02d" % (i + 1)] = {"kwh": 99.0, "nk": 5, "tsum": 5.0, "nt": 5, "q": 0}
        m = naklady.fit(days)
        self.assertAlmostEqual(m["b"], 1.2, places=3)

    def test_bez_db_nie_je_dost_dat(self):
        r = naklady.odhad(self.cfg, NOW, fetch=lambda c: {})
        self.assertFalse(r["dost_dat"])
        self.assertIsNone(r["model"])
        self.assertTrue(r["text"])


class Odhad(Base):
    def setUp(self):
        super().setUp()
        self.hist()
        self.price = ha_cost.tariff_at(self.cfg, mid(2026, 11, 25))[0]

    def fc(self, t):
        return lambda cfg: {(dt.date(2026, 11, 22) + dt.timedelta(days=i)).isoformat(): t for i in range(16)}

    def test_zvysok_mesiaca_s_predpovedou(self):
        r = naklady.odhad(self.cfg, NOW, fetch=self.fc(0.0))
        m = r["mesiac"]
        exp = 0.5 * (3 + 1.2 * 15) + 8 * (3 + 1.2 * 15)       # dnes zostáva pol dňa + 8 dní
        self.assertAlmostEqual(m["zvysok_kwh"], exp, delta=0.1)
        self.assertAlmostEqual(m["zvysok_eur"], exp * self.price, delta=0.1)
        self.assertEqual(m["dni_zvysok"], 9)
        self.assertEqual(m["do"], "2026-11-30")
        self.assertAlmostEqual(m["spolu_kwh"], m["uz_kwh"] + m["zvysok_kwh"], delta=0.11)
        self.assertEqual(r["predpoved_dni"], 16)
        self.assertGreaterEqual(m["pm_eur"], 0)

    def test_teplejsie_pocasie_menej(self):
        z = naklady.odhad(self.cfg, NOW, fetch=self.fc(-3.0))["mesiac"]["zvysok_kwh"]
        t = naklady.odhad(self.cfg, NOW, fetch=self.fc(10.0))["mesiac"]["zvysok_kwh"]
        self.assertGreater(z, t)

    def test_bez_predpovede_normaly(self):
        def boom(cfg):
            raise OSError("siet")
        r = naklady.odhad(self.cfg, NOW, fetch=boom)
        self.assertEqual(r["predpoved_dni"], 0)
        self.assertGreater(r["mesiac"]["zvysok_kwh"], 0)
        self.assertGreater(r["sezona"]["zvysok_kwh"], r["mesiac"]["zvysok_kwh"])

    def test_sezona_do_15_5(self):
        r = naklady.odhad(self.cfg, NOW, fetch=self.fc(0.0))
        s = r["sezona"]
        self.assertFalse(s["mimo_sezony"])
        self.assertEqual((s["od"], s["do"]), ("2026-09-01", "2027-05-15"))
        self.assertGreater(s["pm_kwh"], r["mesiac"]["pm_kwh"])
        r2 = naklady.odhad(dict(self.cfg, NAKLADY_DO="04-30"), NOW, fetch=self.fc(0.0))
        self.assertEqual(r2["sezona"]["do"], "2027-04-30")
        self.assertLess(r2["sezona"]["zvysok_kwh"], s["zvysok_kwh"])

    def test_klimatologia_z_vlastnej_historie(self):
        # minulé roky: november s -20 °C, aby bol priemer z histórie jasne odlišný od normálu pre Slovensko
        self.put(rows_for(dt.date(2025, 11, 1), dt.date(2025, 12, 31), 3.0, 1.2, tf=lambda d: -20.0))
        days = naklady._by_day(naklady._rows(self.cfg, 0, NOW))
        t = naklady.climate(days, dt.date(2026, 12, 10), dt.date(2026, 11, 22))
        self.assertAlmostEqual(t, -20.0, places=3)
        self.assertIsNone(naklady.climate(days, dt.date(2026, 7, 10), dt.date(2026, 11, 22)))   # v lete história nie je

    def test_normal_interpolacia(self):
        self.assertAlmostEqual(naklady.normal(dt.date(2026, 1, 15)), -1.5)
        self.assertAlmostEqual(naklady.normal(dt.date(2026, 12, 15)), 0.5)
        v = naklady.normal(dt.date(2026, 12, 31))
        self.assertTrue(-1.5 < v < 0.5)


class Obdobie(Base):
    def setUp(self):
        super().setUp()
        self.hist()

    def odh(self, bs, now=NOW):
        return naklady.odhad(dict(self.cfg, BILLING_START=bs), now, fetch=lambda c: {})["fakturacne_obdobie"]

    def test_zaciatok_dnes(self):
        p = self.odh("11-22")
        self.assertEqual((p["od"], p["do"]), ("2026-11-22", "2027-11-21"))

    def test_zaciatok_zajtra_je_minuly_rok(self):
        p = self.odh("11-23")
        self.assertEqual((p["od"], p["do"]), ("2025-11-23", "2026-11-22"))
        self.assertEqual(p["dni_zvysok"], 1)

    def test_29_februar_sa_orezava(self):
        p = self.odh("02-29")
        self.assertEqual(p["od"], "2026-02-28")
        self.assertEqual(p["do"], "2027-02-27")

    def test_neplatny_den_default(self):
        p = self.odh("xx")
        self.assertEqual(p["od"], "2026-11-01")

    def test_koniec_mesiaca_december(self):
        now = int(time.mktime((2026, 12, 31, 18, 0, 0, 0, 0, -1)))
        r = naklady.odhad(self.cfg, now, fetch=lambda c: {})
        self.assertEqual(r["mesiac"]["do"], "2026-12-31")
        self.assertEqual(r["mesiac"]["dni_zvysok"], 1)

    def test_uz_spotrebovane_od_zaciatku_obdobia(self):
        p = self.odh("11-15")
        exp = sum(3.0 + 1.2 * max(0, 15 - temp(dt.date(2026, 11, d))) for d in range(15, 22))
        self.assertAlmostEqual(p["uz_kwh"], exp, delta=0.1)
        self.assertEqual(p["dni_s_udajmi"], 7)


class Hranice(Base):
    def test_rozsah_sezony(self):
        self.assertEqual(naklady.season_range({}, 2026), (dt.date(2026, 9, 1), dt.date(2027, 5, 31)))
        self.assertEqual(naklady.season_range({"SEZONA_OD": "10-01", "SEZONA_DO": "04-30"}, 2025), (dt.date(2025, 10, 1), dt.date(2026, 4, 30)))
        self.assertEqual(naklady.season_range({"SEZONA_OD": "xx"}, 2026)[0], dt.date(2026, 9, 1))

    def test_mimo_sezony_odhad(self):
        c = {}
        D = dt.date
        self.assertEqual(naklady.season_start_for(c, D(2027, 5, 15)), D(2026, 9, 1))
        self.assertIsNone(naklady.season_start_for(c, D(2027, 5, 16)))
        self.assertIsNone(naklady.season_start_for(c, D(2026, 8, 31)))
        self.assertEqual(naklady.season_start_for(c, D(2026, 9, 1)), D(2026, 9, 1))
        self.assertEqual(naklady.season_start_for(c, D(2027, 1, 5)), D(2026, 9, 1))

    def test_odhad_v_lete(self):
        self.put(rows_for(dt.date(2026, 5, 1), dt.date(2026, 7, 9), 3.0, 1.2))
        now = int(time.mktime((2026, 7, 10, 12, 0, 0, 0, 0, -1)))
        r = naklady.odhad(self.cfg, now, fetch=lambda c: {})
        self.assertTrue(r["sezona"]["mimo_sezony"])
        self.assertEqual(r["sezona"]["dalsia_od"], "2026-09-01")

    def test_default_rok(self):
        D = dt.date
        self.assertEqual(naklady.default_rok({}, D(2026, 11, 22)), 2026)
        self.assertEqual(naklady.default_rok({}, D(2027, 2, 1)), 2026)
        self.assertEqual(naklady.default_rok({}, D(2027, 7, 1)), 2026)    # po sezóne = posledná skončená
        self.assertEqual(naklady.default_rok({}, D(2027, 9, 1)), 2027)

    def test_data_mimo_sezony_sa_nepocitaju(self):
        self.put(rows_for(dt.date(2025, 8, 20), dt.date(2025, 9, 10), 3.0, 1.0))
        s = naklady.sezona(self.cfg, 2025, NOW)["sezona"]
        self.assertEqual(s["dni_s_udajmi"], 10)      # 1. – 10. 9.
        self.assertEqual(s["od"], "2025-09-01")


class Sezona(Base):
    def setUp(self):
        super().setUp()
        # sezóna 2025/26 s horšou účinnosťou (b = 1,5), prebiehajúca 2026/27 s lepšou (b = 1,2)
        self.put(rows_for(dt.date(2025, 9, 1), dt.date(2026, 5, 31), 3.0, 1.5))
        self.put(rows_for(dt.date(2026, 9, 1), dt.date(2026, 11, 21), 3.0, 1.2))

    def test_sucty_a_normalizacia(self):
        r = naklady.sezona(self.cfg, 2025, NOW)
        s = r["sezona"]
        days = [dt.date(2025, 9, 1) + dt.timedelta(days=i) for i in range(273)]
        hdd = sum(max(0, 15 - temp(d)) for d in days)
        self.assertEqual(s["dni_s_udajmi"], 273)
        self.assertAlmostEqual(s["hdd"], hdd, delta=0.2)
        self.assertAlmostEqual(s["kwh"], 3.0 * 273 + 1.5 * hdd, delta=2.0)  # hodinové hodnoty sa v DB zaokrúhľujú
        self.assertAlmostEqual(s["kwh_hdd"], (3.0 * 273 + 1.5 * hdd) / hdd, delta=0.02)
        self.assertAlmostEqual(s["cop"], 3.5, places=2)
        self.assertAlmostEqual(s["rozptyl"], 1.0, places=2)
        self.assertAlmostEqual([x for x in s["izby"] if x["kluc"] == R1][0]["priemer"], 21.0)
        self.assertAlmostEqual(s["starty"], 273 * 24, delta=3)   # zmena času: generátor má v dňoch s 23/25 h inak hodín
        self.assertAlmostEqual(s["odmraz_min"], 273 * 24 * 2, delta=6)
        self.assertFalse(s["prebieha"])
        self.assertGreater(s["cena_eur"], 0)
        self.assertEqual(s["nazov"], "2025/2026")

    def test_porovnanie_s_predoslou_normalizovane(self):
        r = naklady.sezona(self.cfg, 2026, NOW)
        self.assertTrue(r["sezona"]["prebieha"])
        self.assertEqual(r["predchadzajuca"]["rok"], 2025)
        p = r["porovnanie"]
        self.assertLess(p["kwh_hdd_zmena_pct"], 0)           # účinnejšia sezóna
        self.assertGreater(p["uspora_kwh"], 0)
        self.assertAlmostEqual(p["kwh_pri_starej_ucinnosti"] - r["sezona"]["kwh"], p["uspora_kwh"], delta=0.2)
        # nominálna spotreba je v prebiehajúcej sezóne nižšia aj preto, že je kratšia; HDD-normalizácia to ignoruje
        self.assertGreater(p["hdd_zmena_pct"], -100)

    def test_bez_predoslej_sezony(self):
        r = naklady.sezona(self.cfg, 2024, NOW)
        self.assertFalse(r["sezona"]["ma_data"])
        r = naklady.sezona(self.cfg, 2025, NOW)
        self.assertIsNone(r["predchadzajuca"])
        self.assertIsNone(r["porovnanie"])
        self.assertTrue(r["sezona"]["ma_data"])

    def test_bez_dat_vobec(self):
        os.remove(self.cfg["STATS_DB"])
        r = naklady.sezona(self.cfg, None, NOW)
        self.assertFalse(r["sezona"]["ma_data"])
        self.assertEqual(r["udalosti"], [])
        self.assertIsNone(r["porovnanie"])

    def test_neplatny_rok(self):
        for bad in ("abc", "1999", "99999"):
            with self.assertRaises(ValueError):
                naklady.sezona(self.cfg, bad, NOW)

    def test_rok_default_je_prebiehajuca(self):
        self.assertEqual(naklady.sezona(self.cfg, None, NOW)["sezona"]["rok"], 2026)

    def test_udalosti_ventily_a_krivka(self):
        c = common.adb(self.cfg)
        ins = lambda ts, kind, room, text: c.execute(
            "INSERT INTO interventions(ts,source,kind,room,text,status,due_ts,notified) VALUES(?,?,?,?,?,?,?,0)",
            (ts, "user_manual", kind, room, text, "hotovo", ts + 86400))
        ins(mid(2026, 10, 5), "krivka", None, "Posun krivky o +1")
        ins(mid(2026, 10, 20), "ventil", R1, "Ventil detska 1 otáčka")
        ins(mid(2026, 10, 21), "vetranie", None, "Vetranie")            # nie je udalosť sezóny
        ins(mid(2026, 10, 22), "krivka", None, "zrušené")
        c.execute("UPDATE interventions SET status='zrusene' WHERE text='zrušené'")
        ins(mid(2025, 12, 1), "krivka", None, "Stará sezóna")
        c.commit()
        ev = naklady.sezona(self.cfg, 2026, NOW, c)["udalosti"]
        c.close()
        self.assertEqual([e["druh"] for e in ev], ["krivka", "ventil"])
        self.assertEqual(ev[0]["datum"], "2026-10-05")
        self.assertEqual(ev[0]["text"], "Posun krivky o +1")


class Suhrn(Base):
    def setUp(self):
        super().setUp()
        self.put(rows_for(dt.date(2025, 9, 1), dt.date(2026, 5, 31), 3.0, 1.5))
        self.put(rows_for(dt.date(2024, 9, 1), dt.date(2025, 5, 31), 3.0, 1.7))
        self.c = common.adb(self.cfg)

    def tearDown(self):
        self.c.close()
        super().tearDown()

    def test_iba_prvy_suhrn_po_skonceni(self):
        sun = dt.date(2026, 6, 7)
        r = naklady.sezona_suhrn(self.cfg, self.c, sun, NOW)
        self.assertEqual(r["rok"], 2025)
        self.assertIn("2025/2026", r["text"])
        self.assertIn("menej", r["text"])               # b 1,5 < 1,7
        set_state(self.c, "sezona_suhrn", "2025")
        self.assertIsNone(naklady.sezona_suhrn(self.cfg, self.c, dt.date(2026, 6, 14), NOW))

    def test_pocas_sezony_nie(self):
        self.assertIsNone(naklady.sezona_suhrn(self.cfg, self.c, dt.date(2026, 3, 1), NOW))
        self.assertIsNone(naklady.sezona_suhrn(self.cfg, self.c, dt.date(2026, 5, 31) - dt.timedelta(days=1), NOW))

    def test_neskory_tyzden_po_60_dnoch_nie(self):
        self.assertIsNone(naklady.sezona_suhrn(self.cfg, self.c, dt.date(2026, 9, 6), NOW))

    def test_tyzden_s_koncom_sezony(self):
        # 31. 5. 2026 je nedeľa: súhrn za tento týždeň už sezónu uzatvára
        self.assertEqual(naklady.sezona_suhrn(self.cfg, self.c, dt.date(2026, 5, 31), NOW)["rok"], 2025)

    def test_agent_suhrn_text_obsahuje_odstavec(self):
        import agent
        d = {"izby": [], "sezona": {"text": "Vykurovacia sezóna skončila.", "rok": 2025}}
        self.assertIn("Vykurovacia sezóna skončila.", agent.suhrn_text(d))
        d["sezona"] = None
        self.assertNotIn("Vykurovacia sezóna skončila.", agent.suhrn_text(d))


class Api(Base):
    def test_cesty_v_app(self):
        import app
        self.assertIn("/api/naklady/odhad", app.USER_GET)
        self.assertNotIn("/api/sezona", app.USER_GET)


if __name__ == "__main__":
    unittest.main()
