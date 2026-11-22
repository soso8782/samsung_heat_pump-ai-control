#!/usr/bin/env python3
"""Nočný útlm a režim Preč z domu (utlm.py): plán, učenie rýchlosti ohrevu, poistky, režimy, API. Dočasné DB, zápisy a čas sú mockované."""
import os, sqlite3, tempfile, time, unittest
from unittest import mock

os.environ.setdefault("DRY_RUN", "1")
import agent, app, utlm
from common import REF_ROOM, S, adb, get_state, set_state

ENT = agent.ROOM_TARGET_ENT
REF, OUT = S[REF_ROOM], S["vonku"]
H = 3600


def ts(y, m, d, hh=0, mm=0):
    return int(time.mktime((y, m, d, hh, mm, 0, 0, 0, -1)))


SUN = (2026, 11, 22)    # nedeľa; pondelok 23., piatok 27., sobota 28.


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.tc = os.path.join(self.d, "tc.db")
        t = sqlite3.connect(self.tc)
        t.executescript("CREATE TABLE samples(ts INTEGER, entity TEXT, value REAL);CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);")
        t.commit(); t.close()
        self.cfg = {"TC_DB": self.tc, "AGENT_DB": os.path.join(self.d, "agent.db"), "DRY_RUN": "1", "UI_LANG": "sk"}
        self.c = adb(self.cfg)
        set_state(self.c, "mode", "control")
        self.cur = 23.0
        self.writes = []
        utlm._cache.clear()
        p = [mock.patch.object(agent, "room_target_current", lambda cfg: self.cur),
             mock.patch.object(agent, "set_room_target", self.fake_set),
             mock.patch.object(utlm, "ha_state", lambda cfg, e: self.ha)]
        self.ha = "home"
        for x in p:
            x.start(); self.addCleanup(x.stop)

    def fake_set(self, cfg, c, value, who, lang="sk", reason=None):
        self.writes.append((value, reason))
        self.cur = float(value)
        return {"old": None, "new": value, "dry": True}

    def sample(self, ent, t, v):
        x = sqlite3.connect(self.tc); x.execute("INSERT INTO samples VALUES(?,?,?)", (t, ent, v)); x.commit(); x.close()

    def rate(self, r, n=10):
        return mock.patch.object(utlm, "learn_rate", lambda cfg, now, outdoor="auto": {"rate": r, "n": n, "fallback": False, "outdoor": 0, "method": "x"})

    def modes(self, night=None, away=None):
        if night: set_state(self.c, "utlm_mode", night)
        if away: set_state(self.c, "prec_mode", away)


class Vypnute(Base):
    def test_default_off(self):
        self.assertEqual(utlm.mode(self.c), "off"); self.assertEqual(utlm.mode(self.c, "prec_mode"), "off")
        self.assertEqual(utlm.tick(self.cfg, self.c, ts(*SUN, 23)), "vypnuté")
        self.assertEqual(self.writes, [])
        self.assertEqual(utlm.status_block(self.cfg, self.c, ts(*SUN, 23))["state"], "normal")
        self.assertEqual(utlm.load_cfg(self.c)["delta"], -1.0)

    def test_away_refused_when_off(self):
        ok, _ = utlm.away_start(self.cfg, self.c, None, ts(*SUN, 12))
        self.assertFalse(ok)


class Plan(Base):
    def test_weekday_and_weekend_wake(self):
        self.modes(night="advise")
        with self.rate(1.0):
            p = utlm.plan(self.cfg, self.c, ts(*SUN, 23))            # nedeľa večer -> pondelok 06:30
            self.assertEqual((p["state"], p["wake"]), ("night", ts(2026, 11, 23, 6, 30)))
            self.assertEqual(p["ramp_start"], ts(2026, 11, 23, 5, 30))     # 1 °C / 1 °C/h = 1 h
            p = utlm.plan(self.cfg, self.c, ts(2026, 11, 27, 23))    # piatok večer -> sobota 07:30
            self.assertEqual(p["wake"], ts(2026, 11, 28, 7, 30))
            p = utlm.plan(self.cfg, self.c, ts(2026, 11, 28, 23))    # sobota večer -> nedeľa 07:30
            self.assertEqual(p["wake"], ts(2026, 11, 29, 7, 30))
            p = utlm.plan(self.cfg, self.c, ts(2026, 11, 23, 12))    # cez deň
            self.assertEqual((p["state"], p["next_change"]), ("normal", ts(2026, 11, 23, 22)))
            p = utlm.plan(self.cfg, self.c, ts(2026, 11, 23, 6, 0))  # ráno po začiatku dohrevu
            self.assertEqual(p["state"], "ramp")

    def test_rate_fallback_and_lead(self):
        self.assertEqual(utlm.fit_rate([(1, 0, 2.0)] * 4, 0)["rate"], 1.0)
        self.assertTrue(utlm.fit_rate([], 0)["fallback"])
        self.assertEqual(utlm.lead_hours(1.0, 1.0), (1.0, False))
        self.assertEqual(utlm.lead_hours(0.1, 2.0), (0.5, False))        # spodný strop 0,5 h
        self.assertEqual(utlm.lead_hours(2.0, 0.25), (4.0, True))        # horný strop 4 h

    def test_rate_learning_from_history(self):
        base = ts(2026, 10, 1, 5)
        for i in range(8):                                  # 8 udalostí: nárast 2 °C/h, pri vonku −2 + i
            t0 = base + i * 86400
            self.sample(ENT, t0 - 600, 22.0); self.sample(ENT, t0, 23.0)
            self.sample(OUT, t0, -2.0 + i)
            for k in range(0, 31, 5):
                self.sample(REF, t0 + k * 60, 22.0 + k / 60.0 * 2.0)
        now = ts(2026, 10, 12, 12)
        ev = utlm.rate_events(self.cfg, now)
        self.assertEqual(len(ev), 8)
        r = utlm.fit_rate(ev, 0.0)
        self.assertFalse(r["fallback"]); self.assertAlmostEqual(r["rate"], 2.0, delta=0.2)
        self.assertEqual(utlm.learn_rate(self.cfg, now, 0.0)["n"], 8)

    def test_cap_reduces_depth(self):
        self.modes(night="advise")
        with self.rate(0.2):
            p = utlm.plan(self.cfg, self.c, ts(*SUN, 23))
            self.assertTrue(p["reduced"]); self.assertEqual(p["depth"], 0.5); self.assertEqual(p["note"], "reduced")
            self.assertEqual(p["lead_h"], 2.5)
        with self.rate(0.1):
            p = utlm.plan(self.cfg, self.c, ts(*SUN, 23))
            self.assertEqual((p["state"], p["depth"], p["note"]), ("normal", 0.0, "reduced0"))

    def test_validate(self):
        cur = utlm.load_cfg(self.c)
        self.assertIsNotNone(utlm.validate_cfg({"delta": -2.5}, cur)[0])
        self.assertIsNotNone(utlm.validate_cfg({"delta": -0.3}, cur)[0])
        self.assertIsNotNone(utlm.validate_cfg({"wake": "25:00"}, cur)[0])
        self.assertIsNotNone(utlm.validate_cfg({"start": "06:00"}, cur)[0])
        err, new = utlm.validate_cfg({"delta": "-2", "wake_we": "08:00"}, cur)
        self.assertIsNone(err); self.assertEqual((new["delta"], new["wake_we"]), (-2.0, "08:00"))


class Noc(Base):
    def test_night_apply_ramp_restore(self):
        self.modes(night="control")
        with self.rate(1.0):
            self.assertEqual(utlm.tick(self.cfg, self.c, ts(*SUN, 22, 30)), "útlm")
            self.assertEqual(self.writes, [(22.0, "utlm")]); self.assertEqual(self.cur, 22.0)
            self.assertEqual(utlm.tick(self.cfg, self.c, ts(2026, 11, 23, 2)), "drží")
            self.assertEqual(len(self.writes), 1)
            self.assertEqual(utlm.tick(self.cfg, self.c, ts(2026, 11, 23, 5, 31)), "dohrev")
            self.assertEqual(self.writes[-1], (23.0, "utlm"))
            self.assertEqual(utlm.tick(self.cfg, self.c, ts(2026, 11, 23, 6, 0)), "dohrev")
            self.assertEqual(len(self.writes), 2)
            self.assertEqual(utlm.tick(self.cfg, self.c, ts(2026, 11, 23, 7, 0)), "návrat")
            self.assertIsNone(utlm.load_run(self.c))
        n = self.c.execute("SELECT count(*) FROM runs WHERE status='info'").fetchone()[0]
        self.assertGreaterEqual(n, 2)

    def test_advise_writes_nothing(self):
        self.modes(night="advise")
        with self.rate(1.0):
            self.assertEqual(utlm.tick(self.cfg, self.c, ts(*SUN, 23)), "nič")
        self.assertEqual(self.writes, [])
        st = utlm.status_block(self.cfg, self.c, ts(*SUN, 23))
        self.assertEqual((st["state"], st["mode"]), ("night", "advise"))
        self.assertIn("ramp_start", st); self.assertIn("rate", st)

    def test_reduced_depth_applied(self):
        self.modes(night="control")
        with self.rate(0.4):         # 1,0 / 0,4 = 2,5 h je v limite, 2,0 by už bolo 5 h
            set_state(self.c, "utlm_cfg", '{"delta": -2.0}')
            utlm.tick(self.cfg, self.c, ts(*SUN, 23))
        self.assertEqual(self.writes, [(21.5, "utlm")])      # hĺbka 2,0 -> 1,5 (floor(0,4*4/0,5)*0,5)

    def test_manual_change_not_overwritten(self):
        self.modes(night="control")
        with self.rate(1.0):
            utlm.tick(self.cfg, self.c, ts(*SUN, 22, 30))
            self.cur = 23.5                                  # používateľ zmenil cieľ
            self.assertEqual(utlm.tick(self.cfg, self.c, ts(2026, 11, 23, 1)), "ručná zmena")
            utlm.tick(self.cfg, self.c, ts(2026, 11, 23, 5, 40))
            utlm.tick(self.cfg, self.c, ts(2026, 11, 23, 8))
        self.assertEqual(self.writes, [(22.0, "utlm")]); self.assertEqual(self.cur, 23.5)

    def test_room_min_guard_skips(self):
        self.modes(night="control")
        now = ts(*SUN, 22, 30)
        self.sample(S["jedalen"], now - 60, 22.9)            # minimum 22,5 -> po útlme 1,0 by bolo 21,9
        with self.rate(1.0):
            self.assertEqual(utlm.tick(self.cfg, self.c, now), "preskočené")
        self.assertEqual(self.writes, [])
        self.assertIn("minimum", self.c.execute("SELECT summary FROM runs WHERE status='info'").fetchone()[0])

    def test_room_below_min_during_night_ends_early(self):
        self.modes(night="control")
        now = ts(*SUN, 22, 30)
        with self.rate(1.0):
            utlm.tick(self.cfg, self.c, now)
            self.sample(S["jedalen"], ts(2026, 11, 23, 3) - 60, 22.0)
            self.assertEqual(utlm.tick(self.cfg, self.c, ts(2026, 11, 23, 3)), "izba pod minimom")
            self.assertEqual(self.writes[-1], (23.0, "utlm"))
            utlm.tick(self.cfg, self.c, ts(2026, 11, 23, 3, 5))
        self.assertEqual(len(self.writes), 2)

    def test_abtest_guard(self):
        self.modes(night="control", away="control")
        with self.rate(1.0), mock.patch("abtest.active", lambda c: True):
            self.assertEqual(utlm.tick(self.cfg, self.c, ts(*SUN, 23)), "abtest")
            ok, _ = utlm.away_start(self.cfg, self.c, None, ts(*SUN, 12))
            self.assertFalse(ok)
        self.assertEqual(self.writes, [])

    def test_agent_off_no_write(self):
        self.modes(night="control"); set_state(self.c, "mode", "off")
        with self.rate(1.0):
            utlm.tick(self.cfg, self.c, ts(*SUN, 23))
        self.assertEqual(self.writes, [])

    def test_off_mode_restores(self):
        self.modes(night="control")
        with self.rate(1.0):
            utlm.tick(self.cfg, self.c, ts(*SUN, 22, 30))
            set_state(self.c, "utlm_mode", "off")
            utlm.tick(self.cfg, self.c, ts(*SUN, 23))
        self.assertEqual(self.writes, [(22.0, "utlm"), (23.0, "utlm")])


class Prec(Base):
    def test_away_and_return_ramp(self):
        self.modes(away="control")
        now = ts(*SUN, 12)
        until = ts(2026, 11, 24, 18)
        self.assertTrue(utlm.away_start(self.cfg, self.c, until, now)[0])
        with self.rate(1.0):
            self.assertEqual(utlm.tick(self.cfg, self.c, now), "preč")
            self.assertEqual(self.writes, [(20.0, "prec")])      # 19,0 sa zdvihne na ROOM_TARGET_MIN
            p = utlm.plan(self.cfg, self.c, now)
            self.assertEqual(p["ramp_start"], until - 3 * H)     # rozdiel 3 °C / 1 °C/h
            self.assertEqual(utlm.tick(self.cfg, self.c, until - 4 * H), "drží")
            self.assertEqual(utlm.tick(self.cfg, self.c, until - 3 * H + 60), "dohrev")
            self.assertEqual(self.writes[-1], (23.0, "prec"))
            self.assertEqual(utlm.tick(self.cfg, self.c, until + 120), "návrat")
        self.assertIsNone(utlm.load_away(self.c))

    def test_away_cancel_restores(self):
        self.modes(away="control")
        with self.rate(1.0):
            utlm.away_start(self.cfg, self.c, None, ts(*SUN, 12)); utlm.tick(self.cfg, self.c, ts(*SUN, 12))
            utlm.away_cancel(self.c); utlm.tick(self.cfg, self.c, ts(*SUN, 13))
        self.assertEqual(self.writes, [(20.0, "prec"), (23.0, "prec")])

    def test_away_advise_only_shows(self):
        self.modes(away="advise")
        utlm.away_start(self.cfg, self.c, None, ts(*SUN, 12))
        with self.rate(1.0):
            utlm.tick(self.cfg, self.c, ts(*SUN, 13))
            st = utlm.status_block(self.cfg, self.c, ts(*SUN, 13))
        self.assertEqual((self.writes, st["state"]), ([], "away"))

    def test_presence_auto_away_and_return(self):
        self.modes(away="control")
        self.cfg.update(HA_PRITOMNOST="group.rodina", HA_TOKEN="x" * 30)
        t0 = ts(*SUN, 9)
        self.ha = "not_home"
        with self.rate(1.0):
            utlm.tick(self.cfg, self.c, t0)
            self.assertIsNone(utlm.load_away(self.c)); self.assertEqual(self.writes, [])
            utlm.tick(self.cfg, self.c, t0 + 2 * H - 60)
            self.assertIsNone(utlm.load_away(self.c))
            utlm.tick(self.cfg, self.c, t0 + 2 * H + 60)
            self.assertEqual(utlm.load_away(self.c)["src"], "auto"); self.assertEqual(self.writes, [(20.0, "prec")])
            self.ha = "unavailable"
            utlm.tick(self.cfg, self.c, t0 + 3 * H)
            self.assertIsNotNone(utlm.load_away(self.c))         # neznámy stav nič nemení
            self.ha = "home"
            utlm.tick(self.cfg, self.c, t0 + 4 * H)
        self.assertIsNone(utlm.load_away(self.c)); self.assertEqual(self.writes[-1], (23.0, "prec"))

    def test_presence_ignored_without_config(self):
        self.modes(away="control"); self.ha = "not_home"
        for i in range(5):
            utlm.tick(self.cfg, self.c, ts(*SUN, 9) + i * 3 * H)
        self.assertIsNone(utlm.load_away(self.c))
        self.assertEqual((utlm.presence_of("home"), utlm.presence_of("not_home"), utlm.presence_of("2"), utlm.presence_of("0"),
                          utlm.presence_of("unavailable")), ("home", "away", "home", "away", None))


class Api(Base):
    def post(self, body, role="admin"):
        with mock.patch.object(app, "save_cfg") as sv, mock.patch.object(app, "adb", lambda cfg: adb(self.cfg)):
            r = app.utlm_post(self.cfg, body, role)
            return r, sv

    def test_user_cannot_configure(self):
        (code, _), _ = self.post({"mode": "control"}, "user")
        self.assertEqual(code, 403)
        self.assertEqual(utlm.mode(self.c), "off")

    def test_admin_config(self):
        (code, out), sv = self.post({"mode": "control", "prec_mode": "advise", "delta": -1.5, "ha_pritomnost": "group.rodina"})
        self.assertEqual(code, 200, out)
        self.assertEqual((utlm.mode(self.c), utlm.mode(self.c, "prec_mode"), utlm.load_cfg(self.c)["delta"]), ("control", "advise", -1.5))
        sv.assert_called_once_with({"HA_PRITOMNOST": "group.rodina"})
        self.assertEqual(self.post({"delta": -3})[0][0], 400)
        self.assertEqual(self.post({"ha_pritomnost": "zle meno"})[0][0], 400)
        self.assertEqual(self.post({"mode": "x"})[0][0], 400)

    def test_user_away_and_cancel(self):
        self.modes(away="advise")
        (code, out), _ = self.post({"action": "away", "until": None}, "user")
        self.assertEqual(code, 200); self.assertEqual(out["utlm"]["away"]["src"], "manual")
        self.assertNotIn("cfg", out["utlm"])
        self.assertEqual(self.post({"action": "away", "until": "2020-01-01 10:00"}, "user")[0][0], 400)
        self.assertEqual(self.post({"action": "cancel"}, "user")[0][0], 200)
        self.assertIsNone(utlm.load_away(self.c))

    def test_status_contains_block(self):
        with mock.patch.object(app, "adb", lambda cfg: adb(self.cfg)):
            out = app.status_user(self.cfg, "user", None)
        self.assertIn("utlm", out); self.assertEqual(out["utlm"]["state"], "normal")


if __name__ == "__main__":
    unittest.main()
