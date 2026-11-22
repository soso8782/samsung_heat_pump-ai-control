#!/usr/bin/env python3
"""Testy verzie 2.4.0 (SPEC_en_kompletne.md): úplná angličtina pre UI_LANG=en.
 1. slovenský vstup agenta (systémové prompty + zostavený vstup pre hodinový beh, učenie, týždenný súhrn, chat, udalosť)
    ostáva byte po byte rovnaký ako pred zavedením angličtiny (snapshot test_en_snapshot_sk.json, vytvorený pred úpravou),
 2. v angličtine (anglický profil, anglické dáta) vstup modelu neobsahuje slovenskú diakritiku (okrem pevných názvov entít ESP
    z firmvéru), rovnako texty, ktoré agent ukladá a posiela,
 3. JSON kľúče / enumy výstupu modelu sú v oboch jazykoch rovnaké, anglické texty zachovávajú čísla,
 4. locales: `en.json` má všetky kľúče `sk.json` a rovnaké premenné.
Scenár beží v samostatnom procese (en_worker.py: dočasné databázy, zafixovaný čas a časová zóna, mock ESP/OpenRouter/notify).
Spustenie: python -X utf8 -m unittest test_en_kompletne   (v priečinku kurenia; nepoužíva produkčné dáta)"""
import difflib, json, os, re, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SK_CHARS = re.compile("[áäčďéíĺľňóôŕšťúýžÁČĎÉÍĽŇÓŠŤÚÝŽ]")
# pevné názvy entít zo firmvéru ESP (esphome/): nemenia sa, v texte modelu sú údajom, nie textom aplikácie
FIRMWARE_NAMES = ("Kúrenie", "Ohrev TÚV", "Režim TÚV", "Režim izbovej regulácie", "Stav izbovej regulácie", "Režim vonkajšej jednotky",
                  "Obehové čerpadlo", "Elektrický dohrev kúrenia (Backup heater)", "Elektrický dohrev TÚV (Booster heater)")
SNAPSHOT = os.path.join(HERE, "test_en_snapshot_sk.json")

_cache = {}


def run_worker(lang, english_profile):
    """Spustí en_worker.py a vráti jeho JSON. Slovenský beh používa predvolený profil (nezávisle od profil.json inštalácie)."""
    key = (lang, english_profile)
    if key in _cache:
        return _cache[key]
    tmp = tempfile.mkdtemp()
    pf = os.path.join(tmp, "profil.json")
    import profil
    from en_profile import english_profile as mk
    with open(pf, "w", encoding="utf-8") as f:
        json.dump(mk() if english_profile else profil.default(), f, ensure_ascii=False)
    out = os.path.join(tmp, "out.json")
    wd = os.path.join(tmp, "w")
    os.mkdir(wd)
    env = dict(os.environ)
    for k in ("DRY_RUN", "UI_LANG"):
        env.pop(k, None)
    r = subprocess.run([sys.executable, "-X", "utf8", os.path.join(HERE, "en_worker.py"), out, lang, pf, wd],
                       capture_output=True, env=env, cwd=wd, timeout=300)
    if r.returncode:
        raise RuntimeError("worker zlyhal:\n" + r.stderr.decode("utf-8", "replace")[-3000:])
    with open(out, encoding="utf-8") as f:
        _cache[key] = json.load(f)
    return _cache[key]


def strip_fw(text):
    for n in FIRMWARE_NAMES:
        text = text.replace(n, "")
    return text


# Dátum v riadku „DNES:“ pre učenie (agent.learn) sa berie z reálneho času (time.strftime bez argumentu), nie zo zafixovaných
# hodín worker-a, takže sa mení s dňom spustenia. Porovnáva sa text okolo neho; samotný dátum sa normalizuje na oboch stranách.
DATE_LINE = re.compile(r"^(DNES: )\d{2}\.\d{2}\.\d{4}", re.M)


def no_today(text):
    return DATE_LINE.sub(r"\1<dátum>", text)


def diff(a, b, n=3):
    return "\n".join(list(difflib.unified_diff(a.split("\n"), b.split("\n"), "snapshot", "teraz", lineterm="", n=n))[:60])


class SkSnapshot(unittest.TestCase):
    """Slovenský vstup agenta sa nesmie zmeniť (ani o znak)."""

    @classmethod
    def setUpClass(cls):
        with open(SNAPSHOT, encoding="utf-8") as f:
            cls.old = json.load(f)
        cls.new = run_worker("sk", False)

    def test_constants_unchanged(self):
        self.assertEqual(self.new["consts"], self.old["consts"])

    def test_calls_unchanged(self):
        self.assertEqual([c["label"] for c in self.new["calls"]], [c["label"] for c in self.old["calls"]])
        for o, n in zip(self.old["calls"], self.new["calls"]):
            self.assertEqual(n["system"], o["system"], "system prompt %s sa zmenil:\n%s" % (o["label"], diff(o["system"], n["system"])))
            ou, nu = no_today(o["user"]), no_today(n["user"])
            self.assertEqual(nu, ou, "vstup %s sa zmenil:\n%s" % (o["label"], diff(ou, nu)))

    def test_results_unchanged(self):
        self.assertEqual(self.new["results"], self.old["results"])
        self.assertEqual(self.new["notifications"], self.old["notifications"])
        self.assertEqual(self.new["input_summer"], self.old["input_summer"])

    def test_prompt_for_sk_is_old_behaviour(self):
        import agent
        for name, base in (("learn", agent.LEARN_SYSTEM), ("chat", agent.CHAT_SYSTEM), ("weekly", agent.WEEKLY_SYSTEM)):
            self.assertEqual(agent.prompt_for(name, "sk"), agent.with_lang(base, "sk"))
        self.assertEqual(agent.prompt_for("system", "sk", allowed="A", targets="T"), agent.with_lang(agent.SYSTEM.format(allowed="A", targets="T"), "sk"))
        self.assertEqual(agent.prompt_for("chat_user", "sk", email="a{b}@x.sk"),
                         agent.with_lang(agent.CHAT_SYSTEM_USER.replace("{email}", "ab@x.sk"), "sk"))
        # iný jazyk ako sk/en: doterajšie správanie (slovenský prompt + pokyn jazyka)
        self.assertEqual(agent.prompt_for("learn", "de"), agent.LEARN_SYSTEM + "\n\nOdpovedaj v jazyku: de")


class EnglishInput(unittest.TestCase):
    """UI_LANG=en: celý vstup modelu je po anglicky."""

    @classmethod
    def setUpClass(cls):
        cls.en = run_worker("en", True)

    def test_no_slovak_in_model_input(self):
        bad = []
        for c in self.en["calls"]:
            for part in ("system", "user"):
                for i, line in enumerate(strip_fw(c[part]).split("\n")):
                    if SK_CHARS.search(line):
                        bad.append("%s/%s:%d: %s" % (c["label"], part, i, line[:160]))
        self.assertEqual(bad, [], "slovenský text vo vstupe modelu:\n" + "\n".join(bad[:30]))

    def test_no_slovak_in_stored_and_sent_texts(self):
        txt = json.dumps({k: v for k, v in self.en["results"].items()}, ensure_ascii=False) + json.dumps(self.en["notifications"], ensure_ascii=False)
        bad = sorted(set(m.group(0) for m in re.finditer(r"[^\"]{0,40}[áäčďéíĺľňóôŕšťúýžÁČĎÉÍĽŇÓŠŤÚÝŽ][^\"]{0,40}", strip_fw(txt))))
        self.assertEqual(bad, [], "slovenský text vo výsledkoch / upozorneniach:\n" + "\n".join(bad[:20]))

    def test_every_call_has_english_prompt_and_note(self):
        for c in self.en["calls"]:
            self.assertTrue(c["system"].endswith("\n\nReply in English."), c["label"])
            self.assertNotIn("Odpovedaj", c["system"])
            self.assertIn("English", c["system"].split("\n")[0] + c["system"][:400] + c["system"][-40:])

    def test_sections_translated(self):
        u = [c for c in self.en["calls"] if c["label"] == "run:1"][0]["user"]
        for h in ("WHY YOU ARE DECIDING NOW", "WINDOWS AND DOORS (season: winter):", "TEMPERATURE SENSORS FROM HOME ASSISTANT",
                  "CURRENT STATE (from the ESP):", "LAST 24 HOURS", "EVENTS IN THE LAST 6 HOURS:", "FORECAST", "DAILY SUMMARIES",
                  "LESSONS FROM PREVIOUS DAYS", "SETTING CHANGES IN THE LAST 21 DAYS:", "MESSAGES FROM THE OWNER", "DESIRED ROOM TEMPERATURES",
                  "INTERVENTIONS AND THEIR RESULTS", "AGENT CHANGES IN THE LAST 7 DAYS:", "YOUR PREVIOUS ASSESSMENTS:"):
            self.assertIn(h, u)
        ab = [c for c in self.en["calls"] if c["label"] == "run:ab"][0]["user"]
        self.assertIn("COMPARISON TEST:", ab)
        # čísla: desatinná bodka, nie čiarka
        self.assertNotRegex(u, r"\d,\d")

    def test_outputs_in_english(self):
        r = self.en["results"]
        self.assertEqual(r["run:1"]["actions"][0]["poznamka"], "DRY_RUN: write skipped (simulated) step clipped to 2.0")
        self.assertEqual(r["run:1"]["actions"][1]["poznamka"], "rejected: waiting for an intervention to be evaluated")
        self.assertEqual(r["run:2"]["actions"][0]["poznamka"], "rejected: open window – the temperature drop has a clear cause")
        self.assertEqual(r["run:3"]["actions"][0]["poznamka"], "rejected: summer season")
        self.assertEqual(r["run:4"]["actions"][0]["poznamka"], "not applied: the ESP is not responding reliably")
        self.assertEqual(r["run:nokey"]["error"], "RuntimeError: OPENROUTER_API_KEY is missing in /opt/kurenie/config.env")
        self.assertTrue(r["event_reason"].startswith("alarm: "))
        self.assertEqual(r["undo"][0], True)
        self.assertEqual(r["undo"][1], "Reverted to 46.0.")


class PromptContract(unittest.TestCase):
    """Anglické prompty zachovávajú formát výstupu a čísla slovenských."""

    @classmethod
    def setUpClass(cls):
        import agent
        cls.agent = agent
        cls.keys = {"system": ("stav", "hodnotenie", "akcie", "entita", "hodnota", "dovod", "odporucanie", "ok", "pozor", "problem"),
                    "learn": ("zhrnutie_dna", "poucenia", "sledovat"),
                    "chat": ("odpoved", "zasah", "typ", "izba", "popis", "cas", "ventil", "vetranie", "ine"),
                    "chat_user": ("odpoved", "ciel"),
                    "weekly": ("suhrn", "odporucania")}

    def test_json_keys_same(self):
        for name, ks in self.keys.items():
            sk, en = self.agent.SK_PROMPTS[name], self.agent.EN_PROMPTS[name]
            for k in ks:
                self.assertIn('"%s"' % k, sk, (name, k))
                self.assertIn('"%s"' % k, en, (name, k))

    def test_numbers_preserved(self):
        num = lambda t: set(x.replace(",", ".") for x in re.findall(r"\d+(?:[.,]\d+)?", t))
        for name in self.keys:
            sk = num(self.agent.SK_PROMPTS[name])
            en = num(self.agent.EN_PROMPTS[name])
            self.assertEqual(sk - en, set(), "v EN prompte %s chýbajú čísla %s" % (name, sorted(sk - en)))
            self.assertEqual(en - sk, set(), "v EN prompte %s sú čísla navyše %s" % (name, sorted(en - sk)))

    def test_en_prompts_have_no_slovak_except_profile_names(self):
        for name, p in self.agent.EN_PROMPTS.items():
            t = p.replace(self.agent.REF_NAME, "").replace(self.agent.DEVICE_LINE_EN, "")
            self.assertIsNone(SK_CHARS.search(t), (name, SK_CHARS.search(t) and t[max(0, SK_CHARS.search(t).start() - 40):SK_CHARS.search(t).start() + 40]))

    def test_en_system_formats(self):
        out = self.agent.prompt_for("system", "en", allowed="- a", targets="- t")
        self.assertIn("- a", out)
        self.assertIn('{"stav": "ok" | "pozor" | "problem",', out)
        self.assertNotIn("{{", out)
        self.assertIn("e-mail: x@y.sk", self.agent.prompt_for("chat_user", "en", email="x@y.sk"))


class Locales(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(HERE, "locales", "sk.json"), encoding="utf-8") as f:
            cls.sk = json.load(f)
        with open(os.path.join(HERE, "locales", "en.json"), encoding="utf-8") as f:
            cls.en = json.load(f)

    def test_en_has_every_sk_key(self):
        self.assertEqual(sorted(set(self.sk) - set(self.en)), [])
        self.assertEqual(sorted(set(self.en) - set(self.sk)), [])

    def test_same_placeholders(self):
        ph = lambda t: sorted(set(re.findall(r"\{(\w+)\}", t)))
        bad = [k for k in self.sk if k.startswith("srv.") and ph(self.sk[k]) != ph(self.en[k])]
        self.assertEqual(bad, [])

    def test_en_server_texts_without_slovak(self):
        bad = [(k, v) for k, v in self.en.items() if k.startswith("srv.") and SK_CHARS.search(v)]
        self.assertEqual(bad, [], bad[:10])

    def test_files_are_clean_json(self):
        for lg in ("sk", "en"):
            raw = open(os.path.join(HERE, "locales", lg + ".json"), "rb").read()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            self.assertEqual(json.dumps(json.loads(raw.decode("utf-8")), ensure_ascii=False, indent=1) + "\n", raw.decode("utf-8"))


class Helpers(unittest.TestCase):
    def test_event_text_both_directions(self):
        import i18n
        sk = ["Zberač spustený", "ESP znova dostupné", "ESP nedostupné (URLError)", "Nenulový chybový kód TČ",
              "START (pauza 12 min) - vonku 3.2 °C, Tw2 40.0 °C, ventil Heating, ODU Idle, termostat ON, odmraz. OFF",
              "STOP (chod 40 min, max 45 Hz) - vonku 3.1 °C, termostat OFF, odmraz. ON", "[I] log z ESP"]
        for t in sk:
            e = i18n.event_text(t, "en")
            self.assertEqual(i18n.event_text(e, "sk"), t)
            self.assertEqual(i18n.event_text(t, "sk"), t)
            if t != "[I] log z ESP":
                self.assertIsNone(SK_CHARS.search(e), e)
        self.assertTrue(i18n.event_text(sk[4], "en").startswith("START ("))      # SQL `text LIKE 'STOP%'` ostáva platné
        self.assertEqual(i18n.event_entity("zberač", "en"), "collector")
        self.assertEqual(i18n.event_entity("zberač", "sk"), "zberač")

    def test_direction_understands_english(self):
        import interventions
        self.assertEqual(interventions.direction("Throttled the valve in the bedroom 6->5"), -1)
        self.assertEqual(interventions.direction("Closed the nursery valve by one turn"), -1)
        self.assertEqual(interventions.direction("Opened the return valve by 1 turn"), 1)
        self.assertEqual(interventions.direction("valve from 6 to 5"), -1)
        self.assertEqual(interventions.direction("Priškrtený ventil v spálni 6->5"), -1)     # slovenčina ako doteraz
        self.assertEqual(interventions.direction("Pootvorený spätný ventil"), 1)
        self.assertEqual(interventions.direction("aired the room"), 0)

    def test_labels(self):
        import contacts, interventions
        self.assertEqual(contacts.group_label("Ostatné", "sk"), "Ostatné")
        self.assertEqual(contacts.group_label("Ostatné", "en"), "Other")
        self.assertEqual(contacts.group_label("Chodba", "en"), "Hallway")
        self.assertEqual(contacts.group_label("Moja skupina", "en"), "Moja skupina")
        self.assertEqual(interventions.kind_label("ventil", "sk"), "ventil")
        self.assertEqual(interventions.kind_label("ventil", "en"), "valve")
        self.assertEqual(interventions.kind_label("nieco", "en"), "nieco")

    def test_ha_ws_errors(self):
        import ha_ws
        try:
            ha_ws.ws_target("ftp://x")
        except ha_ws.HAError as e:
            self.assertEqual(str(e), "Adresa Home Assistanta (HA_URL) musí začínať na http://, https://, ws:// alebo wss://.")
            self.assertEqual(ha_ws.err_text(e, "en"), "The Home Assistant address (HA_URL) must start with http://, https://, ws:// or wss://.")
        else:
            self.fail("očakávaná výnimka")

    def test_ha_api_validation_language(self):
        import ha_api
        self.assertEqual(ha_api.validate_action("/api/ha/v1/mode", {"mode": "x"}), (False, "neplatný režim"))
        self.assertEqual(ha_api.validate_action("/api/ha/v1/mode", {"mode": "x"}, "en"), (False, "invalid mode"))
        self.assertEqual(ha_api.validate_action("/api/ha/v1/nic", {}, "en"), (False, "unknown action"))


if __name__ == "__main__":
    unittest.main()
