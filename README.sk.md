<p align="center"><img src="assets/logo-transparent.png" width="120" alt="Logo"></p>

# Samsung Heat Pump AI Control – inteligentné riadenie tepelného čerpadla Samsung EHS

[![English](https://img.shields.io/badge/lang-English-blue.svg)](README.md) [![Slovenčina](https://img.shields.io/badge/jazyk-Sloven%C4%8Dina-green.svg)](README.sk.md)
[![Testy](https://github.com/soso8782/samsung_heat_pump-ai-control/actions/workflows/testy.yml/badge.svg)](https://github.com/soso8782/samsung_heat_pump-ai-control/actions/workflows/testy.yml)

[![Podporiť cez PayPal](https://img.shields.io/badge/Podpori%C5%A5-PayPal-00457C?logo=paypal&logoColor=white)](https://www.paypal.com/paypalme/sonics007) [![Podporiť cez Ko-fi](https://img.shields.io/badge/Ko--fi-Podpori%C5%A5-FF5E5B?logo=ko-fi&logoColor=white)](https://ko-fi.com/soso8782)
Ak ti projekt šetrí peniaze alebo čas, môžeš ma podporiť kávou cez PayPal alebo Ko-fi. Ďakujem! ☕

[![Verzia](https://img.shields.io/github/v/tag/soso8782/samsung_heat_pump-ai-control?label=verzia&sort=semver)](https://github.com/soso8782/samsung_heat_pump-ai-control/tags)
![Python](https://img.shields.io/badge/python-3.11%2B%20(stdlib)-blue)

**Aktuálna verzia: 2.8.0** (16. 11. 2026) – zoznam zmien v [CHANGELOG.md](CHANGELOG.md).

Domáci systém, ktorý zbiera dáta z tepelného čerpadla **Samsung EHS** (cez NASA zbernicu a ESPHome), počíta skutočnú spotrebu
a COP, sleduje teploty v izbách a pomocou jazykového modelu (cez [OpenRouter](https://openrouter.ai)) **radí alebo priamo
nastavuje ekvitermickú krivku** v bezpečných mantineloch. Všetko beží na malom Linux kontajneri (LXC), iba so štandardnou
knižnicou Pythonu 3 – žiadne pip balíčky.

Webové rozhranie, upozornenia aj AI agent sú dostupné **po slovensky a po anglicky**. Web sa prepína tlačidlom SK/EN v hornej
lište; jazyk upozornení a agenta sa nastavuje cez `UI_LANG` v `config.env` alebo na webe v Nastaveniach → „Jazyk upozornení
a agenta“. *English documentation: [README.md](README.md).*

## Čo to vie

- **Prehľad v reálnom čase** – teplota vody (výstup/spiatočka, ΔT), kompresor, odmrazovanie, 3-cestný ventil, TÚV, chybové kódy.
- **Izby** – teploty z Home Assistant (Zigbee/ESPHome čidlá), cieľové minimá, detail izby s grafom, otvorené okná/dvere.
- **Riadiaca izba** – kliknutím vyberieš izbu a čidlo, podľa ktorého ESP reguluje (select „Riadiaca izba“ vo firmvéri);
  história prepnutí v grafe izby, upozornenie na nesúlad ESP a profilu, upozornenie pri výpadku riadiaceho čidla
  a porovnanie čidiel (rozptyl teplôt izieb, odchýlka od cieľa, cyklovanie, kWh/deň, COP). Pri výpadku čidla firmvér
  sám prejde na záložné čidlo tej istej izby.
- **Vyregulovanie ventilov** – denník zmien ventilov (VK 33) s vyhodnotením rozptylu izieb po 24 a 48 h.
- **Týždenný súhrn kúrenia** – každý pondelok ráno: teploty a rozptyl izieb, vonku, spotreba a cena, COP, štarty kompresora,
  TÚV, zásahy agenta a krátke odporúčanie AI (napr. k vyregulovaniu ventilov).
- **Ako kúri podľa vonku** – nastavenie krivky (FSV 2011/2012/2021/2022/2031/2032/4013, posun, zosilnenie), história zmien FSV,
  graf a tabuľka skutočnej teploty vody pri danej vonkajšej teplote (len minúty, keď naozaj kúri do radiátorov) a min/max
  teploty izieb podľa vonkajšej teploty.
- **Spotreba a cena** – denná spotreba celého domu (z elektromera / portálu distribúcie) a tepelného čerpadla, odhad ceny podľa
  cenníka (DD5, VT/NT, mesačná platba, adresná energopomoc), mesačné súhrny, fakturačné obdobie.
- **Presný výpočet COP** – tepelný výkon z prietoku a ΔT (prietok kalibrovaný podľa tabuliek výrobcu), príkon kalibrovaný
  na nočnú spotrebu domu; denné/týždenné/mesačné/ročné COP.
- **Home Assistant integrácia** – senzory cez MQTT discovery (príkon, spotreba, teplo, COP, obdobia), externé štatistiky
  do Energy panelu (spotreba domu, náklady, čerpadlo). Samostatná integrácia (entity, ovládanie agenta) je v inom repozitári,
  pozri [Integrácia do Home Assistanta](#integrácia-do-home-assistanta).
- **AI agent** – každú hodinu (alebo pri udalosti) vyhodnotí stav, predpoveď počasia a históriu; v režime *Radí* len odporúča,
  v režime *Riadi* mení povolené nastavenia. Denné „učenie“ z predošlých dní, správy agentovi, týždenné súhrny, A/B test.
- **TÚV** – plánovač a manuálny ohrev teplej vody, voliteľne v lacnej tarife a najteplejších hodinách.
- **Tarifa, útlm, Preč z domu** – voliteľné prikúrenie pred drahou tarifou, nočný útlm s naučeným ranným nábehom
  a režim Preč z domu (aj podľa prítomnosti v HA); všetko predvolene vypnuté a v medziach agenta.
- **Zdravie čerpadla** – trendy COP, odmrazovania, prietoku a cyklovania pri rovnakom počasí s upozornením na zhoršenie.
- **Odhad nákladov a sezóna** – odhad do konca mesiaca a sezóny, prehľad sezóny prepočítaný na dennostupne.
- **Upozornenia** – Web Push (PWA na iPhone/Android) a HA webhook: chyba TČ, dohrev, okno otvorené pri kúrení, kredit, …
- **Prihlásenie** – heslo pre správcu, voliteľne Cloudflare Tunnel + Access (overenie JWT), role admin/user.
- **Štatistiky** – hodinová databáza, cykly kompresora, poruchy, porovnania, export.
- **Údržba** – `update.sh` (bezpečná aktualizácia z gitu s návratom pri chybe), verzia firmvéru ESP vo webe, testy v CI na GitHube.

## Architektúra

```
Samsung EHS ──NASA bus──> ESPHome (M5Stack Atom, m5stack-tc) ──REST/SSE──> collector.py ──> tc.db (minútové vzorky)
                                                                                   │
Home Assistant (izby, kontakty, elektromer) <──WS/REST/MQTT──> agent.py / stats.py ─┴─> stats.db (hodinové), agent.db
                                                                   │
                                          OpenRouter (LLM) <───────┤
                                                                   └──> app.py + ui.html  (web/PWA, port 80)
```

| Súbor | Účel |
|---|---|
| `collector.py` | zberač z ESP (SSE + REST, každú minútu) do `tc.db` |
| `agent.py` | hodinový beh agenta, mantinely, učenie, upozornenia, MQTT publikovanie |
| `app.py`, `ui.html`, `sw.js` | webový server (http.server) a jednostránková aplikácia / PWA |
| `stats.py`, `stats_ui.js/css`, `stats_backfill.py` | hodinové štatistiky, COP, kalibrácia, import histórie z HA |
| `spotreba.py`, `ha_cost.py` | denná spotreba a cena, cenníky, import do HA štatistík |
| `vonku.py` | „Ako kúri podľa vonku“ – krivka, voda a izby podľa vonkajšej teploty |
| `ha_pub.py` | senzory pre Home Assistant cez MQTT discovery |
| `ha_ws.py` | minimálny WebSocket klient pre Home Assistant |
| `contacts.py`, `contacts_mqtt.py`, `temps.py` | okenné/dverné kontakty a teplotné čidlá |
| `dhw.py` | teplá úžitková voda (plánovač, manuálny ohrev) |
| `interventions.py`, `abtest.py` | zásahy (ventily…) s vyhodnotením, A/B test nastavení |
| `notify.py`, `webpush.py` | upozornenia (HA webhook, Web Push s VAPID) |
| `cftunnel.py`, `cfjwt.py` | Cloudflare Tunnel a overovanie Cloudflare Access JWT |
| `locales/sk.json`, `locales/en.json` | texty webového rozhrania (slovenčina, angličtina); nový jazyk = nový súbor s rovnakými kľúčmi |
| `profil.py`, `profil.example.json`, `modely/` | profil inštalácie (zariadenie, entity, izby, mantinely) a knižnica modelov |
| `esphome/`, `docs/` | vzorová konfigurácia ESPHome a dokumentácia profilu |
| `monitor/` | pôvodný jednoduchý monitor (port 8099) |
| `SPEC*.md` | návrhy jednotlivých funkcií |
| `test_*.py`, `t_*.py` | testy (unittest) |

## Požiadavky

- Tepelné čerpadlo Samsung EHS (testované: Split gen. J, AE120JXEDGH + hydrobox AE160JNYDGH, 2018, R410A).
- ESPHome most na NASA zbernici, napr. M5Stack Atom s komponentom
  [`Beormund/esphome-samsung-nasa`](https://github.com/Beormund/esphome-samsung-nasa) a `web_server` (REST + SSE).
  Názvy entít, ktoré kód očakáva, sú v profile (`profil.json`, predvolené v `profil.py`); vzorový YAML je v `esphome/samsung-ehs.yaml`.
- Linux (Debian 12/13 LXC stačí), Python 3.11+, systemd. Žiadne ďalšie balíčky.
- Voliteľne: Home Assistant (izby, kontakty, elektromer, Energy panel), MQTT broker, OpenRouter kľúč, Cloudflare účet.

## Inštalácia

```bash
git clone https://github.com/soso8782/samsung_heat_pump-ai-control.git /opt/kurenie
cd /opt/kurenie
cp config.env.example config.env && chmod 600 config.env
nano config.env          # ESP_URL, LAT/LON, prípadne HA_URL, NOTIFY_URL …
sh install.sh            # vytvorí a spustí systemd služby kurenie-collector/-web/-agent/-monitor
```

Potom otvor `http://<ip-kontajnera>/`, nastav heslo správcu a v **Nastaveniach** vlož kľúč OpenRouter, token Home Assistant,
MQTT a pod. Tieto tajomstvá sa ukladajú len lokálne do `config.env` (chmod 600) a späť sa nezobrazujú.

Denná záloha databáz: `systemd/kurenie-backup.service` + `.timer` (spúšťa `backup.sh`). Vzorové jednotky sú v `systemd/`.

### Aktualizácia

Spúšťaj ako root v inštalačnom adresári (`/opt/kurenie`). `update.sh` skončí, ak sú zmenené sledované súbory, urobí len
fast-forward `git merge` z upstreamu (alebo `--ref <tag/vetva>`), skontroluje syntax Python súborov, reštartuje štyri služby,
overí ich a pri zlyhaní sa vráti na predchádzajúci commit. Lokálne súbory (`config.env`, databázy, `backup/`) nie sú sledované
gitom a nemení ich.

```bash
./update.sh --dry-run    # ukáže prichádzajúce commity a rozsah zmien, nič nemení
./update.sh              # aktualizácia; alebo --ref v2.12.0 pre konkrétny tag
```

Testy:

```bash
python3 -m unittest test_cop test_spotreba test_vonku test_mqtt
```

## Integrácia do Home Assistanta

Integrácia pre Home Assistant (zariadenie s entitami na sledovanie a ovládanie agenta) sa už nevyvíja v tomto repozitári,
ale vo vlastnom: **[samsung_heat_pump-ai-control-home-assistant-integration](https://github.com/soso8782/samsung_heat_pump-ai-control-home-assistant-integration)**.

- Nainštaluj ju cez **HACS → Vlastné repozitáre** s adresou tohto druhého repozitára (nie tohto).
- Vyžaduje aplikáciu Samsung Heat Pump AI Control **verzie 2.3.0 alebo novšej**.
- Kľúč vytvoríš v aplikácii: **Nastavenia → Integrácia do Home Assistanta → Vytvoriť kľúč**.

Podrobný postup je v README integrácie. Senzory pre panel Energia (spotreba, COP) naďalej posiela táto aplikácia cez MQTT (`ha_pub.py`).

## MCP server pre Claude

Aplikácia má vstavaný **MCP server** (Model Context Protocol) na adrese `http://<adresa-aplikácie>/mcp`. Claude sa cez neho
pýta na aktuálny stav, jednotlivé dni (hodina po hodine), denné súhrny, spotrebu, zásahy a behy agenta a môže agentovi poslať
správu. **Nastavenia tepelného čerpadla cez MCP meniť nejde** – požiadavku posúdi agent v rámci svojich mantinelov.

- Kľúč vytvoríš v aplikácii: **Nastavenia → MCP server pre Claude → Vytvoriť kľúč** (alebo na serveri `python3 mcp_api.py --novy-kluc`).
- Claude Code: `claude mcp add --transport http kurenie http://<adresa-aplikácie>/mcp --header "Authorization: Bearer <kľúč>"`
- Claude Desktop: v `claude_desktop_config.json` cez most `mcp-remote`
  (`"command": "npx", "args": ["mcp-remote", "http://<adresa-aplikácie>/mcp", "--allow-http", "--header", "Authorization: Bearer <kľúč>"]`).
- Nástroje: `stav`, `den`, `dni`, `spotreba`, `zasahy`, `spravy`, `beh_agenta` (len čítanie) a `poslat_spravu`.
- Cez Cloudflare tunel s Access prihlásením treba pre cestu `/mcp` výnimku: v Zero Trust → Access → Applications pridaj
  self-hosted aplikáciu s doménou `<hostname>/mcp` a politikou **Bypass** (Everyone). Zvyšok webu ostane za Access,
  `/mcp` chráni kľúč aplikácie. Potom funguje aj `https://<hostname>/mcp` (bez `--allow-http` v mcp-remote).

## Bezpečnosť a mantinely

- Agent smie meniť len nastavenia zo zoznamu `ALLOWED` v `agent.py` (napr. FSV 2031 42–50 °C, FSV 2032 32–38 °C, FSV 4013,
  maximálny kladný posun krivky) a len o malý krok za 12–24 h, nanajvýš 2 zmeny na beh, nikdy pri chybe TČ alebo starých dátach.
- `DRY_RUN=1` – nič sa neodošle do ESP ani na webhooky (len log).
- V repozitári nie sú žiadne kľúče, tokeny ani databázy (`.gitignore`). `config.env`, `cf_token`, `vapid.json`, `*.db`
  a história spotreby zostávajú len na serveri.

## Prispôsobenie

Inštalačne špecifické veci (izby, názvy entít v ESP, typ kúrenia, mantinely agenta, zdroje histórie z HA, model zariadenia)
sú od verzie 2.0 v **profile** (`profil.json`), nie v kóde – pozri [docs/PROFIL.sk.md](docs/PROFIL.sk.md) a sekciu
[Iné tepelné čerpadlo / vlastný dom](#iné-tepelné-čerpadlo--vlastný-dom). Zvyšok:
ID elektromera (`ENERGY_EXT_ID`, `ENERGY_METER_IDS` v `ha_ws.py` alebo pole `elektrina` v profile),
cenníky `TARIFF` v `ha_cost.py` (cenník SSE/SSD na Slovensku) a adresa ESP (`ESP_URL` v `config.env`).

## Iné tepelné čerpadlo / vlastný dom

Aplikáciu možno nasadiť aj na iné Samsung EHS (Mono/Split, iný výkon, iné izby, podlahové kúrenie) bez úprav kódu:

1. **ESPHome most** – použi [`esphome/samsung-ehs.yaml`](esphome/samsung-ehs.yaml) (názvy entít sú rovnaké ako v predvolenom
   profile; zapojenie a nahratie popisuje [`esphome/README.sk.md`](esphome/README.sk.md)). Čísla niektorých NASA správ treba doplniť
   podľa modelu (riadky `TODO`).
2. **Profil** – v Nastaveniach → **Zariadenie a dom** (len správca) prejdi 4 krokmi: zariadenie, entity ESP, izby, mantinely.
   Sprievodca načíta entity z ESP a teplotné senzory z HA a navrhne priradenie. Po uložení reštartuj služby.
   Súbor `profil.json` môžeš upraviť aj ručne – vzor je [`profil.example.json`](profil.example.json), popis položiek
   v [docs/PROFIL.sk.md](docs/PROFIL.sk.md). Bez `profil.json` platí pôvodná inštalácia (Samsung AE120JXEDGH, radiátory, 7 izieb).
3. **Model zariadenia** – v `modely/` sú tabuľky výkonu a príkonu (Maximum Heating Capacity, Peak) z dokumentácie výrobcu
   pre 54 modelov Samsung EHS:

   | Rad | Modely (vonkajšia jednotka) |
   |---|---|
   | Split R410A (gen. J) | `AE040/060JXEDEH`, `AE090/120/140/160JXEDGH` |
   | Split R410A (rad A) | `AE120/160AXEDEH/GH` |
   | Split R32 | `AE040/060RXEDEG`, `AE090RXEDEG/GG` |
   | Split R32 (DXED) | `AE125/160DXEDEG/GG` |
   | Mono R410A (gen. J) | `AE050JXYDEH`, `AE090/120/140/160JXYDEH/GH` |
   | Mono R32 | `AE050RXYDEG`, `AE080/120/160RXYDEG/GG` |
   | Mono HT Quiet R32 | `AE080/120/140BXYDEG/GG` |
   | Mono R290 | `AE050CXYDEK`, `AE080/120/160CXYDEK/GK` |
   | Mono R290 so zabudovaným čerpadlom | `AE050CXYBEK`, `AE080/120/160CXYBEK/GK` |

   Overený na reálnom zariadení je len `AE120JXEDGH`, ostatné majú `"overene": false` a presný zdroj (dokument a URL)
   v poli `zdroj`. Ako pridať ďalší model: [docs/MODELY.sk.md](docs/MODELY.sk.md).
   Pre model bez tabuľky COP funguje, ak je k dispozícii
   prietok zo senzora alebo z konfigurácie (`STATS_FLOW_LPM`); inak sa tepelný výkon a COP nezobrazia.
4. **Mantinely** – predvolené hranice agenta sa prispôsobia typu kúrenia (radiátory / podlahové / kombinované) a profil ich
   môže len zúžiť v rámci pevných absolútnych hraníc.

Funkcie, ktoré pre tvoju inštaláciu nie sú k dispozícii (TÚV, Home Assistant, agent bez kľúča OpenRouter, spotreba domu,
COP bez tabuľky), sa v aplikácii skryjú namiesto chýb.

## Upozornenie

Projekt je hobby, bez záruky. Zmeny nastavení tepelného čerpadla robíš na vlastnú zodpovednosť – začni v režime *Radí*
alebo s `DRY_RUN=1`.
