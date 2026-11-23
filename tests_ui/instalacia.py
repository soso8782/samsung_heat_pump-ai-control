"""Dočasná inštalácia aplikácie pre test v prehliadači: kópia kódu, config.env, profil, DB so syntetickými dátami,
beh app.py a monitor/web.py na voľných portoch, prihlásenie (admin cez /api/setup + /api/login, user cez zápis relácie do agent.db).
Iba štandardná knižnica; Playwright sa tu nepoužíva."""
import hashlib, os, secrets, shutil, socket, sqlite3, subprocess, sys, tempfile, textwrap, time, urllib.request, json

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COOKIE = "kurenie_session"
ADMIN_PW = "test-heslo-123"
SKIP_DIRS = {".git", ".github", "tests_ui", "tests_ha", "docs", "custom_components", "esphome", "__pycache__", "systemd"}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ignore(d, names):
    return [n for n in names if n in SKIP_DIRS or n.endswith((".md", ".pyc")) or n.startswith("test_")]


SEED = textwrap.dedent('''
    import math, os, sqlite3, sys, time
    sys.path.insert(0, os.getcwd())
    from common import load_cfg
    import stats, profil
    cfg = load_cfg()
    now = int(time.time()) // 300 * 300
    c = sqlite3.connect(cfg["TC_DB"])
    c.executescript("CREATE TABLE IF NOT EXISTS samples(ts INTEGER, entity TEXT, value REAL);"
                    "CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts);"
                    "CREATE TABLE IF NOT EXISTS events(ts INTEGER, kind TEXT, entity TEXT, old TEXT, new TEXT, text TEXT);"
                    "CREATE TABLE IF NOT EXISTS notes(ts INTEGER, author TEXT, title TEXT, body TEXT);"
                    "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);")
    base = {"vonku": 4.0, "tw2": 33.0, "tw1": 28.0, "wl": 32.0, "ciel": 22.0, "freq": 40.0, "prikon": 1500.0, "tuv": 48.0}
    rows = []
    for i in range(14 * 24 * 12):   # 14 dní po 5 minút
        ts = now - i * 300
        ph = math.sin(ts / 3600.0 / 24 * 2 * math.pi)
        for key, ent in stats.TC.items():
            if key in ("chyba",):
                continue
            b = base.get(key, 21.0 if key in stats.ROOM_KEYS else 1.0)
            on = (key not in ("freq", "prikon", "tw2")) or (i // 12) % 3 != 0
            rows.append((ts, ent, b + 2.0 * ph if on else (0.0 if key in ("freq", "prikon") else b)))
    c.executemany("INSERT INTO samples VALUES(?,?,?)", rows)
    c.commit(); c.close()
    stats.catch_up(cfg)
    # spotreba celého domu (elektromer): bez nej je záložka Spotreba skrytá (funkcie.spotreba_domu = False)
    import sqlite3 as _sq
    sd = _sq.connect(stats._db_path(cfg))
    sd.execute("UPDATE hourly SET kwh_dom = COALESCE(kwh, 0) + 0.35")
    sd.commit(); sd.close()
    stats.fault_add(cfg, now - 86400, now - 86400 + 1800, "E101", "Test porucha", "Syntetická porucha pre test UI")
''')


class Instalacia:
    """with Instalacia() as inst: inst.url, inst.cookie_admin, inst.cookie_user, inst.monitor_url"""

    def __init__(self, monitor=True):
        self.monitor = monitor
        self.procs = []

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="kurenie-ui-")
        self.root = os.path.join(self.dir, "app")
        shutil.copytree(REPO, self.root, ignore=_ignore)
        self.port, self.mport = free_port(), free_port()
        dead = free_port()   # zatvorený port: spojenie sa odmietne okamžite
        self.cfg = {
            "DRY_RUN": "1", "UPDATE_CHECK": "0", "WEB_PORT": str(self.port),
            "ESP_URL": "http://127.0.0.1:%d" % dead, "HA_URL": "http://127.0.0.1:%d" % dead, "HA_TOKEN": "", "OPENROUTER_API_KEY": "",
            "TC_DB": os.path.join(self.dir, "tc.db"), "AGENT_DB": os.path.join(self.dir, "agent.db"), "STATS_DB": os.path.join(self.dir, "stats.db"),
        }
        with open(os.path.join(self.root, "config.env"), "w", encoding="utf-8") as f:
            f.write("".join("%s=%s\n" % kv for kv in self.cfg.items()))
        ex = os.path.join(self.root, "profil.example.json")
        if os.path.exists(ex):
            with open(ex, encoding="utf-8") as f:
                prof = json.load(f)
            # TÚV zapnutá (záložka TÚV sa inak skryje): entity ako v predvolenom profile
            prof.setdefault("zariadenie", {})["tuv"] = True
            ent = prof.setdefault("esp", {}).setdefault("entity", {})
            ent["tuv"] = ent.get("tuv") or "Teplota TÚV (Bojler)"
            ent["tuv_ciel"] = ent.get("tuv_ciel") or "Cieľová teplota TÚV"
            with open(os.path.join(self.root, "profil.json"), "w", encoding="utf-8") as f:
                json.dump(prof, f, ensure_ascii=False, indent=1)
        self.env = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", TZ="Europe/Bratislava", TC_DB=self.cfg["TC_DB"], TC_PORT=str(self.mport))
        for k in ("DRY_RUN", "TC_ESP"):
            self.env.pop(k, None)
        self.env["DRY_RUN"] = "1"
        subprocess.run([sys.executable, "-X", "utf8", "-c", SEED], cwd=self.root, env=self.env, check=True, timeout=300)
        self._start([sys.executable, "-X", "utf8", "app.py"], self.root, self.port)
        if self.monitor:
            self._start([sys.executable, "-X", "utf8", "web.py"], os.path.join(self.root, "monitor"), self.mport)
        self.url = "http://127.0.0.1:%d" % self.port
        self.monitor_url = "http://127.0.0.1:%d" % self.mport
        self.cookie_admin = self._admin_cookie()
        self.cookie_user = self._user_cookie()
        return self

    def _start(self, cmd, cwd, port):
        log = open(os.path.join(self.dir, "%s-%d.log" % (os.path.basename(cmd[-1]), port)), "w")
        p = subprocess.Popen(cmd, cwd=cwd, env=self.env, stdout=log, stderr=subprocess.STDOUT)
        self.procs.append((p, log))
        for _ in range(100):
            if p.poll() is not None:
                raise RuntimeError("%s skončil s kódom %s:\n%s" % (cmd[-1], p.returncode, self.log(port)))
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("%s nenaštartoval:\n%s" % (cmd[-1], self.log(port)))

    def log(self, port):
        for p, log in self.procs:
            if log.name.endswith("-%d.log" % port):
                log.flush()
                return open(log.name, encoding="utf-8", errors="replace").read()[-3000:]
        return ""

    def post(self, path, body):
        req = urllib.request.Request(self.url + path, json.dumps(body).encode(), {"Content-Type": "application/json", "X-Kurenie": "1"})
        return urllib.request.urlopen(req, timeout=20)

    def _admin_cookie(self):
        r = self.post("/api/setup", {"password": ADMIN_PW})   # prvé nastavenie hesla správcu (ako v sprievodcovi)
        r = self.post("/api/login", {"password": ADMIN_PW})
        for h in r.headers.get_all("Set-Cookie") or []:
            if h.startswith(COOKIE + "="):
                return h.split(";", 1)[0].split("=", 1)[1]
        raise RuntimeError("login nevrátil cookie")

    def _user_cookie(self):
        """Relácia role `user` zapísaná priamo do agent.db (v appke vzniká len cez Cloudflare Access); rovnaký tvar ako create_session."""
        tok = secrets.token_hex(32)
        now = int(time.time())
        c = sqlite3.connect(self.cfg["AGENT_DB"], timeout=30)
        c.execute("INSERT OR REPLACE INTO users(email, role, added_ts) VALUES(?,?,?)", ("user@example.test", "user", now))   # relácia s e-mailom platí len pre e-mail v users
        c.execute("INSERT INTO sessions(token_hash, created, expires, role, email) VALUES(?,?,?,?,?)",
                  (hashlib.sha256(tok.encode()).hexdigest(), now, now + 86400, "user", "user@example.test"))
        c.commit(); c.close()
        return tok

    def __exit__(self, *a):
        for p, log in self.procs:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
            log.close()
        shutil.rmtree(self.dir, ignore_errors=True)
