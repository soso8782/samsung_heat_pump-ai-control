# Zmeny

Formát podľa [Keep a Changelog](https://keepachangelog.com/sk/1.1.0/), čísla verzií podľa [Semantic Versioning](https://semver.org/).

## [2.10.1] – 2026-11-21

### Opravené
- **Stav agenta v Home Assistante už nie je „unknown“ po informačnom zázname.** Zmena cieľovej teploty, režimu či sezóny
  zapíše do `runs` záznam bez stavu; `GET /api/ha/v1/state` teraz berie `agent.stav`, `zhrnutie` a `posledny_beh`
  z posledného skutočného hodnotenia agenta (`stav` ok / pozor / problem), nie z posledného záznamu. `app.status`
  pridáva `last_assessment`. Test v `test_ha_api.py`.
- **Nová cieľová izbová teplota sa ukáže hneď.** Po potvrdenom zápise do ESP sa hodnota zapíše ako `live:` do meta
  zberača (`agent._live_value`), takže web aj Home Assistant ju zobrazia okamžite, nie až po ďalšej vzorke zberača.
  Test v `test_mqtt_izby.py`.

## [2.10.0] – 2026-11-21

### Pridané
- **MCP server pre Claude** (`mcp_api.py`, `POST /mcp`, Streamable HTTP s JSON odpoveďami): nástroje `stav`, `den`
  (hodina po hodine, voliteľne udalosti), `dni`, `spotreba`, `zasahy`, `spravy`, `beh_agenta` a `poslat_spravu` (správa
  agentovi, označená „[MCP]“). Nastavenia čerpadla cez MCP meniť nejde. Vlastný kľúč (`Authorization: Bearer`, v DB len SHA-256)
  v Nastaveniach → „MCP server pre Claude“ alebo `python3 mcp_api.py --novy-kluc`. `test_mcp_api.py`.

## [2.9.0] – 2026-11-19

### Pridané
- **Izbové teploty priamo zo Zigbee2MQTT:** zberač (`collector.py`) sa pripojí na MQTT broker z `config.env` (rovnaké nastavenie
  ako okamžité kontakty okien) a teplotu izby zapíše hneď po správe teplomera. Párovanie: HA entita izby z profilu
  `sensor.<slug>_temperature` ↔ z2m meno so slugom `<slug>`. Web ukáže čerstvú hodnotu okamžite (`meta` `live:<izba>`,
  `app.live_overlay`), bez čakania na cestu HA → ESP → REST (1–3 min). Hodnota z MQTT má 5 min prednosť pred kópiou v ESP;
  bez MQTT alebo pri výpadku sa číta z ESP ako doteraz. Retained správy sa ignorujú. Regulácia v ESP sa nemení.
- `test_mqtt_izby.py`.

## [2.8.0] – 2026-11-16

### Pridané
- **Ohrev TÚV vo vstupe AI agenta:** `common.dhw_intervals()` z udalostí 3-cestného ventilu (Tank = TÚV, úseky < 2 min sa
  nepočítajú); hodinová tabuľka má stĺpec „TÚV min“, nová sekcia „OHREVY TÚV ZA 7 DNÍ“ (časy od–do), denné súhrny „TÚV n/min“
  (pre staršie uložené dni sa dopočíta). Prompt agenta (sk/en): beh, štarty a kWh počas ohrevu TÚV nepatria kúreniu.
- Testy ohrevov TÚV v `test_prevadzka.py`; slovenský snapshot vstupu agenta zámerne aktualizovaný.

## [2.7.0] – 2026-11-16

### Pridané
- **Štítok režimu vedľa loga:** Kúrenie / Ohrev TÚV / Odmrazovanie / Stojí, s časom od kedy (v popise). Určuje sa z frekvencie
  kompresora a z udalostí 3-cestného ventilu a odmrazovania; pri starých dátach (> 10 min) sa skryje. `/api/status` →
  `prevadzka` ({`rezim`, `od`}) pre všetky roly. `test_prevadzka.py`.

## [2.6.0] – 2026-11-16

### Pridané
- **23 nových modelov** v `modely/` (spolu 54) z dátových kníh výrobcu, tabuľky Maximum Heating Capacity (Peak):
  Mono HT Quiet R32 `AE080/120/140BXYDEG/GG` (voda 25–70 °C, vonku −30…43 °C), Mono R410A gen. J
  `AE050JXYDEH`, `AE090/120/140/160JXYDEH/GH`, Split R32 DXED `AE125/160DXEDEG/GG`, Split R410A rad A `AE120/160AXEDEH/GH`.
  Hodnoty vyčítané skriptom a namátkovo overené proti PDF; žiadny z nich nie je overený na reálnom zariadení.

## [2.5.0] – 2026-11-16

### Zmenené
- **Nový názov aplikácie: Samsung Heat Pump AI Control** (predtým Kúrenie / Kúrenie AI): titulok webu, PWA (krátky názov
  na ploche „Heat Pump AI“), upozornenia (predpona „Heat Pump AI:“), zariadenie MQTT v Home Assistante, dokumentácia.
  ID entít MQTT v HA sa nemenia.
- Integrácia do Home Assistanta sa volá rovnako, doména `samsung_heat_pump_ai_control`, má vlastné logo (HA 2026.3+).

### Pridané
- Odkazy na podporu projektu (PayPal, Ko-fi) v päte webu a v README; logo v README.

## [2.4.0] – 2026-11-16

Úplná angličtina pre anglicky hovoriacich používateľov: s `UI_LANG=en` už web, upozornenia, texty pre Home Assistant
ani chybové hlášky neobsahujú slovenský text vytvorený aplikáciou a AI agent dostane celý vstup po anglicky.
Pri `UI_LANG=sk` je všetko (najmä prompty a vstup agenta) byte po byte rovnaké ako vo verzii 2.3.0.

### Pridané
- **AI agent po anglicky:** systémové prompty (hodinový beh, učenie, týždenný súhrn, chat správcu aj používateľa) majú
  anglické verzie (`SYSTEM_EN`, `LEARN_SYSTEM_EN`, `CHAT_SYSTEM_EN`, `CHAT_SYSTEM_USER_EN`, `WEEKLY_SYSTEM_EN`, výber cez
  `agent.prompt_for()`). Anglicky je aj zostavený vstup: nadpisy sekcií, tabuľky, údaje o oknách, čidlách, zásahoch,
  teste A/B, predpoveď, dôvody behu s modelom, udalosti a poznámky o vykonaných akciách. Kľúče a hodnoty JSON odpovede
  (`stav`, `hodnotenie`, `akcie`, `zasah`, `ciel`, `ok`/`pozor`/`problem`) sú v oboch jazykoch rovnaké.
- Texty udalostí zberača sa ukladajú v jazyku domácnosti; staré slovenské riadky v databáze sa pri zobrazení a pre model
  prekladajú (`i18n.event_text`, `i18n.event_entity`). Skupiny „Ostatné“ / „Chodba“ a druhy čidiel sa zobrazujú podľa jazyka.
- Prekladané sú aj chyby spojenia s Home Assistantom (`ha_ws`), akcie HA API, výsledky plánovača TÚV, dôvody zastavenia
  A/B testu, chyby Web Push, názvy štatistík v Energy paneli HA, názov PWA a výrobca zariadenia v HA.
- `test_en_kompletne.py` – snapshot slovenského vstupu agenta (`test_en_snapshot_sk.json`), anglický vstup bez slovenskej
  diakritiky (s anglickým profilom), zhoda kľúčov `sk`/`en`; pomocné súbory `en_worker.py`, `en_profile.py`.
- **Dokumentácia po anglicky:** `README.md`, `docs/PROFIL.md`, `docs/MODELY.md`, `esphome/README.md` sú anglicky,
  slovenské verzie v `*.sk.md`; na vrchu každého dokumentu prepínač English / Slovenčina. Komentáre v `config.env.example` po anglicky.
- Integrácia do Home Assistanta má vlastný repozitár
  [samsung_heat_pump-ai-control-home-assistant-integration](https://github.com/soso8782/samsung_heat_pump-ai-control-home-assistant-integration)
  (HACS používa ten); kópia v `custom_components/` ostáva len pre ručnú inštaláciu.

### Zmenené
- `UI_LANG=en`: prompty agenta sú anglické (v 2.3.0 ostávali slovenské s pokynom jazyka). Pri iných jazykoch ako `sk`/`en`
  platí doterajšie správanie (slovenské prompty + pokyn „Odpovedaj v jazyku: …“).
- Texty a dôvody uložené v databáze ostávajú v jazyku, v akom vznikli (staré hodnotenia, správy, poučenia); interné značky
  (napr. `vrátené používateľom`) sa nemenia, prekladajú sa len pri zobrazení.

## [2.3.0] – 2026-11-16

### Pridané
- **Preklad textov zo servera:** chybové a informačné hlášky API, názvy metrík štatistík, upozornenia, výsledky agenta
  bez modelu a názvy HA senzorov (MQTT discovery) sa prekladajú cez nový modul `i18n.py` (kľúče `srv.*` v tých istých
  `locales/sk.json` a `locales/en.json`). Jazyk požiadavky z webu je v hlavičke `X-Lang`; texty bez požiadavky
  (upozornenia, agent, týždenný súhrn, HA senzory) idú v jazyku domácnosti `UI_LANG` (`sk` / `en`, Nastavenia → Model).
- Systémové prompty agenta ostávajú po slovensky, pridáva sa im pokyn „Odpovedaj v jazyku: …“.
- `test_server_i18n.py`.
## [2.2.0] – 2026-11-15

### Pridané
- **Jazyky webu:** všetky texty rozhrania (ui.html aj štatistiky) sú v `locales/sk.json` a `locales/en.json` (1432 kľúčov).
  Prepínač **SK / EN** v hornej lište; predvolený jazyk podľa prehliadača (sk/cs → slovenčina, inak angličtina),
  voľba sa pamätá. Čísla a dátumy podľa jazyka (v angličtine desatinná bodka, „15 Nov“).
  Nový jazyk = nový súbor `locales/xx.json` s rovnakými kľúčmi. Server posiela `/locales/<xx>.json`.
- `test_i18n.py` – zhoda kľúčov medzi jazykmi, množné čísla, použité kľúče, žiadny natvrdo zadaný slovenský text.

### Ostáva po slovensky
- Texty zo servera: hodnotenia a správy AI agenta, chybové hlášky API, upozornenia, názvy metrík štatistík,
  a údaje z profilu (názvy izieb, entity ESP).

## [2.1.1] – 2026-11-15
### Pridané
- Prehľad: riadok s typom tepelného čerpadla z profilu (názov, Split/Mono, výkon, fázy, chladivo, typ kúrenia, TÚV, model
  a či sa COP počíta z overenej tabuľky výrobcu); správca sa z neho preklikne do Nastavení. `/api/status` → `profil_info.zariadenie_info`.

## [2.1.0] – 2026-11-15

### Pridané
- **25 nových modelov** v `modely/` z dátových kníh výrobcu (tabuľky Maximum Heating Capacity, Peak): Mono R32
  `AE050/080/120/160RXYD*G`, Mono R290 `AE050/080/120/160CXYD*K` a `…CXYB*K` (so zabudovaným čerpadlom), Split R32
  `AE040/060/090RXED*G`. Spolu 31 modelov; overený na reálnom zariadení zostáva len AE120JXEDGH.
- `docs/MODELY.md` – zoznam modelov so zdrojmi a návod, ako pridať ďalší.

### Opravené
- Hodinové štatistiky: chyba formátovania SQL pri načítaní ďalších teplôt (chodba, kotolňa) z `agent.db`.
- Test predvoleného profilu sa preskočí, ak má inštalácia vlastný `profil.json`.

## [2.0.0] – 2026-11-15

Aplikáciu možno nastaviť pre iné tepelné čerpadlo Samsung EHS a iný dom bez úprav kódu. Bez `profil.json` sa správa
presne ako 1.0.0.

### Pridané
- **Profil inštalácie** (`profil.json`, modul `profil.py`): model a typ zariadenia, typ kúrenia (radiátory / podlahové /
  kombinované), názvy entít v ESP, izby (kľúč, názov, ESP entita, HA senzory, minimum, referenčná izba), ďalšie teploty,
  zdroje histórie z HA, mantinely agenta s pevnými absolútnymi hranicami, elektromer. Podrobnosti: `docs/PROFIL.md`,
  vzor `profil.example.json`.
- **Sprievodca „Zariadenie a dom“** v Nastaveniach (admin): 4 kroky (zariadenie, entity ESP s automatickým návrhom,
  izby, mantinely), uloženie s chybami pri poliach, návrat na predvolené, reštart služieb z webu.
  Nové API `/api/profil`, `/api/profil/predvoleny`, `/api/profil/esp_entity`, `/api/profil/ha_entity`, `/api/restart`.
- **Knižnica modelov** `modely/*.json`: tabuľky Maximum Heating Capacity (Peak) pre Samsung EHS Split
  AE040/060/090/120/140/160JXED*H z dokumentácie výrobcu (overený na reálnom zariadení je len AE120JXEDGH).
- **ESPHome šablóna** `esphome/samsung-ehs.yaml` s názvami entít podľa predvoleného profilu a návod `esphome/README.md`.
- `profil_info` v `/api/status`: zariadenie, izby a dostupné funkcie.

### Zmenené
- Izby, referenčná izba, názvy entít, mantinely agenta, stĺpce hodinovej štatistiky a import histórie z HA sa berú z profilu
  (konštanty ostávajú pod pôvodnými menami); `hourly` v `stats.db` sa pri novej izbe automaticky rozšíri o stĺpec.
- Prompty agenta používajú izby a referenčnú izbu z profilu a vedia, či ide o radiátory alebo podlahovku.
- Tabuľka výrobcu pre COP a kalibráciu prietoku sa číta z `modely/`; bez tabuľky sa použije prietok zo senzora alebo
  z konfigurácie, inak sa výkon a COP nezobrazia (bez chyby).
- Webové rozhranie skrýva funkcie, ktoré nie sú k dispozícii (TÚV, spotreba domu, agent bez kľúča), namiesto chýb.
- Adresa ESP: `TC_ESP`, inak `ESP_URL` z `config.env`; elektromer: prednosť má pole `elektrina` v profile.

## [1.0.0] – 2026-11-15

Prvé verejné vydanie.

### Funkcie
- Zberač dát z ESPHome mosta na Samsung NASA zbernici (SSE + REST, minútové vzorky).
- Webová aplikácia / PWA: prehľad, izby, TÚV, deň po hodinách, štatistiky, čidlá, analýzy, nastavenia; tmavý/svetlý režim.
- **Ako kúri podľa vonku** – nastavenie ekvitermickej krivky (FSV), história zmien, skutočná teplota vody a izieb podľa vonkajšej teploty.
- **Spotreba** – denná spotreba domu a tepelného čerpadla s odhadom ceny podľa cenníka (DD5, energopomoc), mesačné súhrny.
- Presný výpočet COP (prietok kalibrovaný z tabuliek výrobcu, príkon kalibrovaný na nočnú spotrebu domu).
- Home Assistant: senzory cez MQTT discovery, externé štatistiky pre Energy panel, okenné kontakty a teplotné čidlá.
- AI agent cez OpenRouter (režimy Radí / Riadi) s pevnými mantinelmi, denné učenie, správy agentovi, týždenné súhrny, A/B test.
- Plánovač a manuálny ohrev TÚV, zásahy s vyhodnotením.
- Upozornenia cez Web Push a HA webhook; prihlásenie heslom, Cloudflare Tunnel + Access (JWT), role admin/user.

[2.8.0]: https://github.com/soso8782/samsung_heat_pump-ai-control/releases/tag/v2.8.0
[2.7.0]: https://github.com/soso8782/samsung_heat_pump-ai-control/releases/tag/v2.7.0
[2.6.0]: https://github.com/soso8782/samsung_heat_pump-ai-control/releases/tag/v2.6.0
[2.5.0]: https://github.com/soso8782/samsung_heat_pump-ai-control/releases/tag/v2.5.0
[2.4.0]: https://github.com/soso8782/samsung_heat_pump-ai-control/releases/tag/v2.4.0
[2.3.0]: https://github.com/soso8782/samsung_heat_pump-ai-control/releases/tag/v2.3.0
[2.2.0]: https://github.com/soso8782/samsung_heat_pump-ai-control/releases/tag/v2.2.0
[2.1.1]: https://github.com/soso8782/samsung_heat_pump-ai-control/releases/tag/v2.1.1
[2.1.0]: https://github.com/soso8782/samsung_heat_pump-ai-control/releases/tag/v2.1.0
[2.0.0]: https://github.com/soso8782/samsung_heat_pump-ai-control/releases/tag/v2.0.0
[1.0.0]: https://github.com/soso8782/samsung_heat_pump-ai-control/releases/tag/v1.0.0
