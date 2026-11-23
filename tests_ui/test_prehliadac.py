#!/usr/bin/env python3
"""Smoke test webu v skutočnom prehliadači (Playwright, Chromium). Beží v CI (job „prehliadac“); lokálne sa preskočí,
ak Playwright alebo prehliadač nie je nainštalovaný.

Podnet: po zlúčení sa v stats_ui.js zduplikovali `const` deklarácie; syntaktická kontrola to nezachytila, no prehliadač
skript odmietol a záložka Štatistiky sa nenačítala. Test spustí skutočný app.py (+ monitor/web.py) nad dočasnou
inštaláciou (tests_ui/instalacia.py: DRY_RUN, mŕtve ESP/HA, syntetické DB), prihlási sa a prejde všetky záložky.

Pre každú záložku sa overí: žiadne `pageerror`, žiadne chyby v konzole mimo zoznamu ALLOWED_* nižšie, žiadne zlyhané
požiadavky ani HTTP >= 400 mimo zoznamu, panel je viditeľný, v texte nie je „undefined“ / „NaN“ / „[object Object]“
a nezobrazuje sa surový prekladový kľúč (text zhodný s kľúčom z locales/sk.json) ani varovanie „i18n: missing key“.

Spustenie: pip install playwright && python -m playwright install chromium && python tests_ui/test_prehliadac.py"""
import json, os, re, sys, unittest
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    from playwright.sync_api import sync_playwright, Error as PWError
    PW_OK = True
except ImportError:   # lokálne bez Playwrightu: čisté preskočenie (trieda nižšie má skipUnless)
    PW_OK, sync_playwright, PWError = False, None, Exception
from instalacia import Instalacia, COOKIE, REPO

IN_CI = bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))
LOCAL_HOSTS = {"127.0.0.1", "localhost"}

# --- ZOZNAM POVOLENÝCH VÝNIMIEK (zámerne úzky; všetko ostatné test zlyhá) ---------------------------------------
# Externé hosty (Google Fonts) sa v teste neťahajú: odpoveď sa podvrhne (route.fulfill), takže nevznikne žiadna chyba.
# Povolené HTTP chyby: (metóda, regex cesty, množina stavov). Pri mŕtvom ESP/HA app.py odpovedá 200 s prázdnymi/„neznámymi“
# údajmi (overené lokálne), preto je tu iba Web Push, ktorý vráti 503, keď nie je nainštalovaný pywebpush.
ALLOWED_HTTP = [
    ("GET", re.compile(r"^/api/push/(key|status)$"), {503}),
]
# Povolené chybové hlásenia konzoly (regex na celý text). Hlásenie „Failed to load resource“ sa tu NErieši: kontroluje sa
# cez ALLOWED_HTTP podľa URL a stavu, nie podľa textu.
ALLOWED_CONSOLE = []
# Ďalšie povolené chyby stránky (pageerror): žiadne.
ALLOWED_PAGEERROR = []

BAD_TEXT = re.compile(r"\bundefined\b|\bNaN\b|\[object Object\]")
with open(os.path.join(REPO, "locales", "sk.json"), encoding="utf-8") as _f:
    I18N_KEYS = {k for k in json.load(_f) if not k.startswith("_")}
KEY_LIKE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")

ADMIN_TABS = ["prehlad", "spotreba", "izby", "tuv", "den", "statistiky", "cidla", "analyzy", "nastavenia"]
USER_TABS = ["prehlad", "spotreba", "izby", "tuv"]
STATS_VIEWS = ["den", "porovnanie", "obdobie", "sezona", "poruchy", "zdravie"]

# JS: viditeľné textové uzly a atribúty (placeholder/title/aria-label) zhodné s prekladovým kľúčom; text s „undefined“/„NaN“
JS_SCAN = """(rootSel) => {
  const root = document.querySelector(rootSel); if (!root) return {text: "", nodes: []};
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && getComputedStyle(el).visibility !== "hidden";
  const nodes = [];
  const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode(); n; n = w.nextNode()) {
    const el = n.parentElement; if (!el || ["SCRIPT", "STYLE", "TEMPLATE"].includes(el.tagName) || !vis(el)) continue;
    const s = n.nodeValue.trim(); if (s) nodes.push(s);
  }
  for (const el of root.querySelectorAll("[placeholder],[title],[aria-label]")) {
    if (!vis(el)) continue;
    for (const a of ["placeholder", "title", "aria-label"]) { const s = (el.getAttribute(a) || "").trim(); if (s) nodes.push(s); }
  }
  return {text: root.innerText, nodes};
}"""


class Zber:
    """Zbiera chyby stránky počas jednej relácie a vyhodnocuje ich proti zoznamom výnimiek."""

    def __init__(self, page):
        self.problems = []
        self.where = "štart"
        page.on("pageerror", lambda e: self._pageerror(e))
        page.on("console", self._console)
        page.on("requestfailed", self._reqfailed)
        page.on("response", self._response)

    def add(self, kind, text):
        self.problems.append("[%s] %s: %s" % (self.where, kind, text))

    def _pageerror(self, e):
        txt = "%s: %s" % (getattr(e, "name", "Error"), getattr(e, "message", e))
        if not any(re.search(p, txt) for p in ALLOWED_PAGEERROR):
            self.add("pageerror", txt)

    def _console(self, m):
        txt = m.text
        if m.type == "error":
            if txt.startswith("Failed to load resource"):
                return   # rieši _response podľa URL a stavu
            if not any(re.search(p, txt) for p in ALLOWED_CONSOLE):
                self.add("console.error", "%s (%s)" % (txt, (m.location or {}).get("url")))
        elif m.type == "warning" and txt.startswith("i18n: missing key"):
            self.add("chýbajúci preklad", txt)

    def _reqfailed(self, r):
        u = urlparse(r.url)
        if u.hostname in LOCAL_HOSTS:
            self.add("requestfailed", "%s %s (%s)" % (r.method, u.path, r.failure))

    def _response(self, r):
        if r.status < 400:
            return
        u = urlparse(r.url)
        if any(r.request.method == m and rx.match(u.path) and r.status in st for m, rx, st in ALLOWED_HTTP):
            return
        self.add("HTTP %d" % r.status, "%s %s" % (r.request.method, u.path + ("?" + u.query if u.query else "")))


def settle(page, ms=700):
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except PWError:
        pass
    page.wait_for_timeout(ms)


def check_text(zber, page, selector, label):
    """Panel je viditeľný a bez „undefined“/„NaN“ a surových prekladových kľúčov."""
    el = page.locator(selector)
    if not el.is_visible():
        zber.add("neviditeľné", "%s (%s)" % (label, selector))
        return ""
    r = page.evaluate(JS_SCAN, selector)
    for m in BAD_TEXT.finditer(r["text"]):
        a = max(0, m.start() - 40)
        zber.add("zlý text", "%s: ...%s..." % (label, r["text"][a:m.end() + 40].replace("\n", " | ")))
    for s in r["nodes"]:
        if KEY_LIKE.match(s) and (s in I18N_KEYS or s.split(".")[0] in {k.split(".")[0] for k in I18N_KEYS}):
            zber.add("surový prekladový kľúč", "%s: %r" % (label, s))
    return r["text"]


def open_app(browser, inst, cookie, locale):
    ctx = browser.new_context(locale=locale, viewport={"width": 1280, "height": 900})
    ctx.add_cookies([{"name": COOKIE, "value": cookie, "url": inst.url}])
    # externé zdroje (Google Fonts) sa nesťahujú: podvrhnutá prázdna odpoveď
    ctx.route(re.compile(r"^https?://(?!(127\.0\.0\.1|localhost)[:/])"), lambda route: route.fulfill(status=200, body="", content_type="text/css"))
    page = ctx.new_page()
    zber = Zber(page)
    page.goto(inst.url + "/#prehlad", wait_until="domcontentloaded")
    page.wait_for_selector("#tab-prehlad:not([hidden])", timeout=20000)
    settle(page, 1200)
    return ctx, page, zber


def visit_tab(zber, page, tab):
    zber.where = "záložka " + tab
    if not page.is_visible("#tabl-" + tab):
        cls = page.get_attribute("#tabl-" + tab, "class") or ""
        raise AssertionError("záložka %s nie je viditeľná (class=%r) – vypnutá funkcia v testovacej inštalácii?" % (tab, cls))
    page.click("#tabl-" + tab, timeout=10000)
    page.wait_for_selector("#tab-%s:not([hidden])" % tab, timeout=10000)
    settle(page)
    check_text(zber, page, "#tab-" + tab, tab)
    if page.get_attribute("#tabl-" + tab, "aria-selected") != "true":
        zber.add("záložka", "%s nie je aria-selected" % tab)


def visit_stats(zber, page):
    zber.where = "záložka statistiky"
    page.click("#tabl-statistiky")
    page.wait_for_selector("#st-views", timeout=15000)   # stats_ui.js sa načítalo a vykreslilo ovládanie
    settle(page, 1000)
    if page.locator("#st-boot").count():
        zber.add("štatistiky", "ostalo „Načítavam štatistiky“ (stats_ui.js sa nenačítalo)")
    if not page.evaluate("!!window.StatsUI"):
        zber.add("štatistiky", "window.StatsUI neexistuje")
    for v in STATS_VIEWS:
        zber.where = "štatistiky/" + v
        page.click('#st-views button[data-v="%s"]' % v)
        page.wait_for_selector("#st-v-%s:not([hidden])" % v, timeout=10000)
        settle(page, 900)
        txt = check_text(zber, page, "#st-v-" + v, "statistiky/" + v)
        if not txt.strip():
            zber.add("prázdny pohľad", "statistiky/" + v)


def visit_extras(zber, page):
    """Podzáložky a rozbaľovacie časti, ktoré sa neotvárajú cez hlavné záložky."""
    zber.where = "izby/ako kúri podľa vonku"
    page.click("#tabl-izby")
    page.wait_for_selector("#tab-izby:not([hidden])")
    page.click("#vkbtn")
    page.wait_for_selector("#vkbox:not([hidden])", timeout=10000)
    settle(page)
    check_text(zber, page, "#vkbox", "izby/vkbox")
    if page.locator("#tab-cidla").count() and page.locator("#tabl-cidla").is_visible():
        for sub in ("okna", "teploty"):
            zber.where = "čidlá/" + sub
            page.click("#tabl-cidla")
            page.click('#subseg button[data-sub="%s"]' % sub)
            settle(page, 500)
            check_text(zber, page, "#tab-cidla", "cidla/" + sub)


@unittest.skipUnless(PW_OK, "playwright nie je nainštalovaný (test beží v CI, job „prehliadac“)")
class Prehliadac(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except PWError as e:
            cls.pw.stop()
            if IN_CI:
                raise
            raise unittest.SkipTest("Chromium nie je nainštalovaný: %s" % str(e).splitlines()[0])
        cls.inst = Instalacia().__enter__()

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "inst", None):
            cls.inst.__exit__(None, None, None)
        if getattr(cls, "browser", None):
            cls.browser.close()
        if getattr(cls, "pw", None):
            cls.pw.stop()

    def finish(self, zber, ctx):
        ctx.close()
        self.assertEqual(zber.problems, [], "\n" + "\n".join(zber.problems) + "\n--- log app.py ---\n" + self.inst.log(self.inst.port))

    def run_admin(self, locale):
        ctx, page, zber = open_app(self.browser, self.inst, self.inst.cookie_admin, locale)
        zber.where = "štart"
        if page.evaluate("document.body.classList.contains('role-user')"):
            zber.add("rola", "admin je zobrazený ako user")
        for tab in ADMIN_TABS:
            if tab == "statistiky":
                visit_stats(zber, page)
            else:
                visit_tab(zber, page, tab)
        visit_extras(zber, page)
        self.finish(zber, ctx)

    def test_admin_sk(self):
        self.run_admin("sk-SK")

    def test_admin_en(self):
        self.run_admin("en-GB")

    def test_user_sk(self):
        ctx, page, zber = open_app(self.browser, self.inst, self.inst.cookie_user, "sk-SK")
        if not page.evaluate("document.body.classList.contains('role-user')"):
            zber.add("rola", "user nie je zobrazený s role-user")
        for tab in USER_TABS:
            visit_tab(zber, page, tab)
        for tab in set(ADMIN_TABS) - set(USER_TABS):   # admin záložky nesmú byť pre rolu user viditeľné
            if page.locator("#tabl-" + tab).is_visible():
                zber.add("oprávnenia", "záložka %s je viditeľná pre rolu user" % tab)
        self.finish(zber, ctx)

    def test_monitor(self):
        ctx = self.browser.new_context(locale="sk-SK", viewport={"width": 1280, "height": 900})
        # monitor/index.html potrebuje Chart.js z CDN (cdnjs, jsdelivr): v CI sa sťahuje naozaj, nič sa nepodvrhuje
        page = ctx.new_page()
        zber = Zber(page)
        zber.where = "monitor"
        page.goto(self.inst.monitor_url + "/", wait_until="domcontentloaded")
        settle(page, 1500)
        txt = check_text(zber, page, "body", "monitor")
        if len(txt.strip()) < 20:
            zber.add("monitor", "stránka je prázdna")
        self.finish(zber, ctx)


if __name__ == "__main__":
    unittest.main(verbosity=2)
