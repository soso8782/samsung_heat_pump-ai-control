#!/usr/bin/env python3
"""Entity pre Home Assistant cez MQTT (ha_ent.py): discovery, stav, príkazy (MQTT_OVLADANIE, DRY_RUN, neplatné vstupy), slučka s falošným
brokerom (socketpair, bez siete). Dočasná DB, siete a čas sú mockované."""
import json, os, socket, sqlite3, sys, tempfile, threading, time, unittest
from unittest import mock

os.environ.setdefault("DRY_RUN", "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import contacts_mqtt as cm
import ha_ent, ha_pub, utlm
from common import adb, get_state, set_state


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        tc = os.path.join(self.d, "tc.db")
        t = sqlite3.connect(tc)
        t.executescript("CREATE TABLE samples(ts INTEGER, entity TEXT, value REAL);CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);")
        t.commit(); t.close()
        self.cfg = {"TC_DB": tc, "AGENT_DB": os.path.join(self.d, "agent.db"), "DRY_RUN": "0", "UI_LANG": "sk", "MQTT_HOST": "x", "MQTT_OVLADANIE": "1"}
        self.c = adb(self.cfg)
        utlm._status_cache.clear()
        p = mock.patch.object(utlm, "tick", lambda *a, **k: None)
        p.start(); self.addCleanup(p.stop)

    def cmd(self, key, payload, retain=False, **cfg):
        c2 = dict(self.cfg, **cfg)
        return ha_ent.handle_command(c2, self.c, ha_ent.CMD[key], payload, retain)

    def runs(self):
        return [r[0] for r in self.c.execute("SELECT summary FROM runs ORDER BY id")]


class Discovery(Base):
    def test_ovladanie_on(self):
        msgs = {t: (json.loads(p) if p else None) for t, p, r in ha_ent.discovery(self.cfg)}
        sw = msgs["homeassistant/switch/kurenie_tc/away/config"]
        self.assertEqual(sw["name"], "Preč z domu")
        self.assertEqual(sw["command_topic"], ha_ent.CMD["away"])
        self.assertEqual(sw["unique_id"], "kurenie_tc_away")
        self.assertEqual(sw["device"], ha_pub.device_block("sk"))
        self.assertEqual(sw["device"]["identifiers"], ["kurenie_tc"])
        self.assertEqual(sw["availability_topic"], ha_ent.AVAIL_TOPIC)
        sel = msgs["homeassistant/select/kurenie_tc/utlm_mode/config"]
        self.assertEqual(sel["options"], ["off", "advise", "control"])
        self.assertEqual(sel["command_topic"], ha_ent.CMD["utlm_mode"])
        self.assertEqual(sel["name"], "Nočný útlm – režim")
        self.assertIsNone(msgs["homeassistant/sensor/kurenie_tc/utlm_mode/config"])        # stará doména sa maže
        self.assertTrue(all(r for _t, _p, r in ha_ent.discovery(self.cfg)))                 # všetko retained
        self.assertEqual(msgs["homeassistant/sensor/kurenie_tc/utlm_next/config"]["device_class"], "timestamp")
        self.assertEqual(msgs["homeassistant/binary_sensor/kurenie_tc/zdravie_problem/config"]["device_class"], "problem")
        self.assertEqual(msgs["homeassistant/sensor/kurenie_tc/naklady_mesiac/config"]["unit_of_measurement"], "€")
        self.assertIn("json_attributes_topic", msgs["homeassistant/sensor/kurenie_tc/zdravie/config"])

    def test_ovladanie_off_je_len_stav(self):
        msgs = {t: (json.loads(p) if p else None) for t, p, r in ha_ent.discovery(dict(self.cfg, MQTT_OVLADANIE="0"))}
        self.assertEqual(msgs["homeassistant/binary_sensor/kurenie_tc/away/config"]["unique_id"], "kurenie_tc_away")
        self.assertNotIn("command_topic", json.dumps(msgs))
        self.assertIsNone(msgs["homeassistant/switch/kurenie_tc/away/config"])
        self.assertEqual(msgs["homeassistant/sensor/kurenie_tc/prec_mode/config"]["device_class"], "enum")

    def test_unikatne_id_a_en(self):
        ids = [json.loads(p)["unique_id"] for t, p, r in ha_ent.discovery(self.cfg) if p]
        self.assertEqual(len(ids), len(set(ids)))
        en = [json.loads(p)["name"] for t, p, r in ha_ent.discovery(self.cfg, "en") if p]
        self.assertIn("Away from home", en)


class Stav(Base):
    def test_stav(self):
        mem = {"slow": {"naklady_mesiac": 41.5, "verzia": "2.9.0", "update": "ON"}, "slow_ts": time.time(), "health_ts": time.time(),
               "health": {"zdravie": "warn", "zdravie_problem": "ON", "zdravie_attr": {"indikatory": []}}}
        set_state(self.c, "prec_mode", "advise")
        s = ha_ent.build_state(self.cfg, self.c, mem)
        self.assertEqual((s["away"], s["prec_mode"], s["utlm_mode"]), ("OFF", "advise", "off"))
        self.assertIn(s["utlm_state"], ha_ent.UTLM_STATES)
        self.assertEqual((s["zdravie"], s["zdravie_problem"], s["naklady_mesiac"], s["verzia"], s["update"]), ("warn", "ON", 41.5, "2.9.0", "ON"))
        self.assertIn(s["tarifa_faza"], ha_ent.PHASES)
        utlm._jset(self.c, "utlm_away", {"src": "manual", "since": 1, "until": None})
        utlm._status_cache.clear()
        self.assertEqual(ha_ent.build_state(self.cfg, self.c, mem)["away"], "ON")

    def test_slow_cache_30_min(self):
        mem = {}
        with mock.patch("naklady.odhad", return_value={"dost_dat": True, "mesiac": {"spolu_eur": 12.34}}) as o, \
             mock.patch("verzia.info", return_value={"app": "2.9.0", "novsia": False}), \
             mock.patch("zdravie.api", return_value={"enabled": True, "stav": "crit", "indikatory": [{"kod": "cop", "nazov": "COP", "stav": "crit", "zmena_pct": -20}],
                                                      "chyby": {"kody": []}, "nalezy": [{"text": "x"}]}):
            r = ha_ent.collect_slow(self.cfg, mem, 1000)
            self.assertEqual((r["naklady_mesiac"], r["update"], r["zdravie"], r["zdravie_problem"]), (12.34, "OFF", "crit", "ON"))
            self.assertEqual(r["zdravie_attr"]["indikatory"][0]["kod"], "cop")
            ha_ent.collect_slow(self.cfg, mem, 1000 + 600)
            self.assertEqual(o.call_count, 1)
            ha_ent.collect_slow(self.cfg, mem, 1000 + 1800)
            self.assertEqual(o.call_count, 2)

    def test_chyba_zdroja_nezhodi(self):
        with mock.patch("naklady.odhad", side_effect=RuntimeError("x")), mock.patch("verzia.info", side_effect=RuntimeError("x")), \
             mock.patch("zdravie.api", side_effect=RuntimeError("x")):
            r = ha_ent.collect_slow(self.cfg, {}, 1000)
        self.assertEqual(r, {})


class Prikazy(Base):
    def test_away_on_off(self):
        set_state(self.c, "prec_mode", "control")
        self.assertEqual(self.cmd("away", b"ON"), "applied")
        a = utlm.load_away(self.c)
        self.assertEqual((a["src"], a["until"]), ("manual", None))
        self.assertTrue(any("Home Assistant (MQTT)" in r and "Preč" in r for r in self.runs()))
        self.assertEqual(self.cmd("away", b"off"), "applied")
        self.assertIsNone(utlm.load_away(self.c))

    def test_away_pri_vypnutom_rezime_odmietne(self):
        self.assertTrue(self.cmd("away", b"ON").startswith("refused:"))      # prec_mode = off (web vráti 409)
        self.assertIsNone(utlm.load_away(self.c))
        self.assertTrue(any("odmietnut" in r for r in self.runs()))

    def test_away_abtest(self):
        set_state(self.c, "prec_mode", "control")
        with mock.patch("abtest.active", return_value=True):
            self.assertTrue(self.cmd("away", b"ON").startswith("refused:"))
        self.assertIsNone(utlm.load_away(self.c))

    def test_selecty(self):
        self.assertEqual(self.cmd("utlm_mode", b"control"), "applied")
        self.assertEqual(self.cmd("prec_mode", b"advise"), "applied")
        self.assertEqual((utlm.mode(self.c), utlm.mode(self.c, "prec_mode")), ("control", "advise"))
        self.assertTrue(any("Nočný útlm – režim" in r and "control" in r for r in self.runs()))

    def test_ovladanie_0_nic(self):
        for k, p in (("away", b"ON"), ("utlm_mode", b"control"), ("prec_mode", b"control")):
            self.assertEqual(self.cmd(k, p, MQTT_OVLADANIE="0"), "ignored:ovladanie")
        self.assertEqual((utlm.mode(self.c), utlm.mode(self.c, "prec_mode"), utlm.load_away(self.c)), ("off", "off", None))
        self.assertEqual(self.cmd("away", b"ON", MQTT_OVLADANIE=""), "ignored:ovladanie")

    def test_neplatne(self):
        set_state(self.c, "prec_mode", "control")
        for k, p in (("away", b"maybe"), ("away", b""), ("away", b"\xff\xfe"), ("away", b"ON" * 20), ("utlm_mode", b"full"), ("utlm_mode", b"{}"),
                     ("prec_mode", b"1")):
            self.assertEqual(self.cmd(k, p), "ignored:payload", (k, p))
        self.assertEqual((utlm.mode(self.c), utlm.load_away(self.c)), ("off", None))

    def test_retain_sa_ignoruje(self):
        set_state(self.c, "prec_mode", "control")
        self.assertEqual(self.cmd("away", b"ON", retain=True), "ignored:retain")
        self.assertIsNone(utlm.load_away(self.c))

    def test_dry_run(self):
        set_state(self.c, "prec_mode", "control")
        self.assertEqual(self.cmd("away", b"ON", DRY_RUN="1"), "ignored:dry")
        self.assertEqual(self.cmd("utlm_mode", b"control", DRY_RUN="1"), "ignored:dry")
        self.assertEqual((utlm.mode(self.c), utlm.load_away(self.c)), ("off", None))
        self.assertTrue(any("DRY_RUN" in r for r in self.runs()))

    def test_neznamy_topic(self):
        self.assertEqual(ha_ent.handle_command(self.cfg, self.c, "kurenie/tc/ent/cmd/iny", b"ON"), "ignored:topic")

    def test_enabled(self):
        self.assertTrue(ha_ent.enabled({"MQTT_HOST": "h"}))
        self.assertFalse(ha_ent.enabled({"MQTT_HOST": "h", "MQTT_PUB": "0"}))
        self.assertFalse(ha_ent.enabled({}))


class Slucka(Base):
    def test_spojenie_s_falosnym_brokerom(self):
        cli, srv = socket.socketpair()
        got, stop = [], threading.Event()
        set_state(self.c, "prec_mode", "control")
        slow = {"slow": {"naklady_mesiac": 1.0, "verzia": "2.9.0", "update": "OFF"}, "slow_ts": 1e12, "health_ts": 1e12, "health": {"zdravie": "ok"}}

        def broker():
            try:
                srv.settimeout(10)
                t, _f, body = cm.read_packet(srv)
                got.append(("connect", body))
                srv.sendall(b"\x20\x02\x00\x00")
                seen = 0
                while True:
                    t, f, body = cm.read_packet(srv)
                    if t == 8:
                        got.append(("sub", body))
                    elif t == 3:
                        topic, data, _r = cm.parse_publish(f, body)
                        got.append(("pub", topic, data))
                        if topic == ha_ent.STATE_TOPIC and not seen:
                            seen = 1
                            srv.sendall(ha_pub.build_publish(ha_ent.CMD["away"], "ON"))       # príkaz z HA
                        elif topic == ha_ent.STATE_TOPIC and seen == 1:
                            stop.set()
                            return
            except Exception as e:
                got.append(("err", repr(e)))
                stop.set()
        th = threading.Thread(target=broker); th.start()
        with mock.patch.object(ha_ent, "collect_slow", lambda cfg, mem, now, lang="sk": slow["slow"] | slow["health"]):
            ha_ent.run_once(dict(self.cfg), stop=stop, connect=lambda addr, timeout=None: cli)
        th.join(5)
        cli.close(); srv.close()
        self.assertFalse([g for g in got if g[0] == "err"], got)
        kinds = [g[0] for g in got]
        self.assertEqual(kinds[0], "connect")
        self.assertIn(b"offline", got[0][1])                                   # last will
        self.assertEqual(kinds.count("sub"), 3)
        pubs = {g[1]: g[2] for g in got if g[0] == "pub"}
        self.assertEqual(pubs[ha_ent.AVAIL_TOPIC] in (b"online", b"offline"), True)
        self.assertIn("homeassistant/switch/kurenie_tc/away/config", pubs)
        states = [json.loads(g[2]) for g in got if g[0] == "pub" and g[1] == ha_ent.STATE_TOPIC]
        self.assertEqual([s["away"] for s in states], ["OFF", "ON"])            # stav hneď po príkaze
        self.assertIsNotNone(utlm.load_away(self.c))


if __name__ == "__main__":
    unittest.main()
