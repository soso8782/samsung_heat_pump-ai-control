"""MCP server (mcp_api.py): kľúč, JSON-RPC (initialize, tools/list, tools/call) a napojenie na HTTP server app.py."""
import json, os, sqlite3, tempfile, threading, types, unittest, urllib.request, urllib.error

os.environ.setdefault("DRY_RUN", "1")
import mcp_api


def _db():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE state(k TEXT PRIMARY KEY, v TEXT)")
    return c


class Kluc(unittest.TestCase):
    def test_vytvorenie_overenie_zrusenie(self):
        c = _db()
        self.assertFalse(mcp_api.check(c, "Bearer nic"))
        k = mcp_api.new_key(c, now=1000)
        self.assertTrue(k.startswith("kmcp_"))
        self.assertTrue(mcp_api.check(c, "Bearer " + k))
        self.assertTrue(mcp_api.check(c, "bearer  %s " % k))
        for bad in (None, "", k, "Basic " + k, "Bearer ", "Bearer x" + k):
            self.assertFalse(mcp_api.check(c, bad), bad)
        self.assertEqual(mcp_api.key_info(c), {"set": True, "created": 1000, "path": "/mcp"})
        self.assertNotIn(k, json.dumps(mcp_api.key_info(c)))
        k2 = mcp_api.new_key(c)
        self.assertFalse(mcp_api.check(c, "Bearer " + k))
        self.assertTrue(mcp_api.check(c, "Bearer " + k2))
        mcp_api.revoke(c)
        self.assertFalse(mcp_api.check(c, "Bearer " + k2))
        self.assertEqual(mcp_api.key_info(c)["set"], False)


def fake_app(calls):
    def chat_reply(cfg, c, text, info=None, role=None, email=None, lang=None):
        calls.append(("chat", text, role))
        info["intervention"] = None
        return "Rozumiem.", 0.001, None
    return types.SimpleNamespace(
        os=os, ROOT=os.path.dirname(os.path.abspath(mcp_api.__file__)),
        i18n=types.SimpleNamespace(home_lang=lambda cfg: "sk"),
        status=lambda cfg, role, email, lang: {"now": 1, "mode": "control", "current": {"Izba Jedáleň": 22.7}, "balance": {"tajne": 1},
                                               "last_run": {"id": 5, "summary": "ok", "input": "dlhý vstup"}},
        day=lambda cfg, d, lang: (calls.append(("day", d)) or {"date": d or "2026-11-21", "summary": {}, "hours": [],
                                  "runs": [{"id": 1, "summary": "s", "input": "x"}], "events": [{"kind": "log"}, {"kind": "nastavenie"}]}),
        history=lambda cfg: {"days": [{"date": "d%d" % i} for i in range(30)], "lessons": {"items": ["a"]}},
        run_detail=lambda cfg, rid: {"input": "in", "output": "out"} if rid == 5 else {},
        messages=lambda cfg, limit=60: [{"text": "t"}] * min(limit, 3),
        adb=lambda cfg: _db(),
        interventions=types.SimpleNamespace(list_items=lambda c, n: [{"id": 1}]),
        spotreba=types.SimpleNamespace(report=lambda cfg, od, do, lang="sk": {"od": od, "do": do}),
        chat_reply=chat_reply)


def rpc(method, params=None, rid=1):
    b = {"jsonrpc": "2.0", "method": method, "id": rid}
    if params is not None:
        b["params"] = params
    return b


class Protokol(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.app = fake_app(self.calls)

    def call(self, name, args=None):
        code, out = mcp_api.handle({}, rpc("tools/call", {"name": name, "arguments": args or {}}), self.app)
        self.assertEqual(code, 200)
        return out["result"]

    def test_initialize(self):
        code, out = mcp_api.handle({}, rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}}), self.app)
        r = out["result"]
        self.assertEqual((code, r["protocolVersion"]), (200, "2025-03-26"))
        self.assertIn("tools", r["capabilities"])
        self.assertEqual(r["serverInfo"]["name"], "samsung-heat-pump-ai-control")
        _, out = mcp_api.handle({}, rpc("initialize", {"protocolVersion": "1999-01-01"}), self.app)
        self.assertEqual(out["result"]["protocolVersion"], mcp_api.PROTOCOLS[0])

    def test_notifikacia_a_chyby(self):
        self.assertEqual(mcp_api.handle({}, {"jsonrpc": "2.0", "method": "notifications/initialized"}, self.app), (202, None))
        self.assertEqual(mcp_api.handle({}, {"jsonrpc": "2.0", "id": 3, "result": {}}, self.app), (202, None))
        self.assertEqual(mcp_api.handle({}, {"method": "ping", "id": 1}, self.app)[0], 400)
        self.assertEqual(mcp_api.handle({}, rpc("nic"), self.app)[1]["error"]["code"], -32601)
        self.assertEqual(mcp_api.handle({}, rpc("tools/call", {"name": "zmaz_vsetko"}), self.app)[1]["error"]["code"], -32602)
        self.assertEqual(mcp_api.handle({}, rpc("ping"), self.app)[1]["result"], {})

    def test_zoznam_nastrojov(self):
        _, out = mcp_api.handle({}, rpc("tools/list"), self.app)
        tools = {t["name"]: t for t in out["result"]["tools"]}
        self.assertEqual(set(tools), {"stav", "den", "dni", "spotreba", "zasahy", "spravy", "beh_agenta", "poslat_spravu"})
        self.assertFalse(tools["poslat_spravu"]["annotations"]["readOnlyHint"])
        self.assertTrue(all(t["annotations"]["readOnlyHint"] for n, t in tools.items() if n != "poslat_spravu"))
        self.assertEqual(tools["poslat_spravu"]["inputSchema"]["required"], ["text"])

    def test_stav_bez_citlivych_poli(self):
        r = self.call("stav")
        d = json.loads(r["content"][0]["text"])
        self.assertFalse(r["isError"])
        self.assertEqual(d["current"], {"Izba Jedáleň": 22.7})
        self.assertNotIn("balance", d)
        self.assertNotIn("input", d["last_run"])

    def test_den(self):
        d = json.loads(self.call("den", {"datum": "2026-11-20"})["content"][0]["text"])
        self.assertEqual(self.calls[-1], ("day", "2026-11-20"))
        self.assertNotIn("events", d)
        self.assertNotIn("input", d["runs"][0])
        d = json.loads(self.call("den", {"udalosti": True})["content"][0]["text"])
        self.assertEqual(d["events"], [{"kind": "nastavenie"}])
        for bad in ({"datum": "20.11.2026"}, {"datum": "2026-13-01"}, {"datum": 5}, {"nic": 1}):
            self.assertTrue(self.call("den", bad)["isError"], bad)

    def test_dni_a_obmedzenia(self):
        d = json.loads(self.call("dni", {"pocet": 5})["content"][0]["text"])
        self.assertEqual(len(d["days"]), 5)
        d = json.loads(self.call("dni", {"pocet": 999})["content"][0]["text"])
        self.assertEqual(len(d["days"]), 30)
        self.assertTrue(self.call("dni", {"pocet": "5"})["isError"])
        self.assertTrue(self.call("dni", {"pocet": True})["isError"])

    def test_beh_agenta(self):
        self.assertEqual(json.loads(self.call("beh_agenta", {"id": 5})["content"][0]["text"]), {"input": "in", "output": "out"})
        self.assertTrue(self.call("beh_agenta", {"id": 6})["isError"])

    def test_poslat_spravu(self):
        d = json.loads(self.call("poslat_spravu", {"text": " Ako je v jedálni? "})["content"][0]["text"])
        self.assertEqual(d["reply"], "Rozumiem.")
        self.assertEqual(self.calls[-1], ("chat", "[MCP] Ako je v jedálni?", "admin"))
        for bad in ({"text": ""}, {"text": "x" * 2001}, {}):
            self.assertTrue(self.call("poslat_spravu", bad)["isError"], bad)

    def test_skratenie(self):
        s = mcp_api._text({"x": "a" * (mcp_api.MAX_TEXT + 10)})
        self.assertLess(len(s), mcp_api.MAX_TEXT + 200)
        self.assertIn("skrátený", s)


class Http(unittest.TestCase):
    """Skutočný HTTP server app.py: /mcp bez kľúča 401, s kľúčom JSON-RPC, notifikácia 202, GET 405."""
    @classmethod
    def setUpClass(cls):
        import app, common
        cls.app = app
        cls.d = tempfile.mkdtemp()
        cfg = {"AGENT_DB": os.path.join(cls.d, "agent.db"), "TC_DB": os.path.join(cls.d, "tc.db"), "STATS_DB": os.path.join(cls.d, "stats.db"),
               "HA_TOKEN": "", "OPENROUTER_API_KEY": "", "HA_URL": "http://ha.invalid:8123", "ESP_URL": "http://esp.invalid",
               "UPDATE_CHECK": "0"}
        cls._orig = app.load_cfg
        app.load_cfg = lambda: cfg
        c = common.adb(cfg)
        cls.key = mcp_api.new_key(c)
        c.close()
        from http.server import ThreadingHTTPServer
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d/mcp" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.app.load_cfg = cls._orig

    def post(self, body, key=True):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if key:
            h["Authorization"] = "Bearer " + self.key
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            return e.code, None

    def test_http(self):
        self.assertEqual(self.post(rpc("tools/list"), key=False)[0], 401)
        code, out = self.post(rpc("initialize", {"protocolVersion": "2025-06-18"}))
        self.assertEqual((code, out["result"]["protocolVersion"]), (200, "2025-06-18"))
        self.assertEqual(self.post({"jsonrpc": "2.0", "method": "notifications/initialized"}), (202, None))
        code, out = self.post(rpc("tools/list"))
        self.assertEqual(len(out["result"]["tools"]), 8)
        try:
            urllib.request.urlopen(self.url, timeout=10)
            self.fail("GET má vrátiť 405")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 405)


if __name__ == "__main__":
    unittest.main()
