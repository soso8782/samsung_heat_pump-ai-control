# Zmeny 2.11.0 → 2.14.0 (22. 11. 2026)

Súhrn štyroch verzií, ktoré pridali **výber riadiacej izby** a nástroje okolo neho, týždenný súhrn kúrenia a bezpečnejšie
nasadzovanie. Podrobnosti po verziách sú v [CHANGELOG.md](../CHANGELOG.md).

## Pre používateľa

### Riadiaca izba (2.11.0 – 2.13.0)
- **Výber kliknutím (správca).** V karte *Izby* klikneš na izbu → *Nastaviť ako riadiacu*; izba s viacerými čidlami
  ponúkne výber čidla (Detská: ESPresense / Zigbee, Jedáleň: izbové čidlo / hlavica). Firmvér ESP má nový select
  **„Riadiaca izba“** s 9 možnosťami; podľa neho beží izbová regulácia (posun krivky aj vypínanie pri prehriatí).
- **Zmena izby** prepne aj referenčnú izbu v profile agenta (pôvodná dostane minimum 21 °C) a reštartuje služby.
  Zmena čidla v tej istej izbe reštart nepotrebuje. Počas A/B testu je zmena zakázaná.
- **História prepnutí v grafe izby** – zvislé značky „Riadiaca: …“, farebný pás so zvoleným čidlom a legenda.
  Zachytí aj prepnutie priamo v ESP alebo Home Assistante (zo zberača) a zlúči ho so zápisom z webu.
- **Nesúlad ESP a profilu** – keď niekto prepne select mimo webu, správca uvidí upozornenie s tlačidlom
  *Zosúladiť profil s ESP*; agent nesúlad uvedie vo svojom vstupe.
- **Výpadok riadiaceho čidla** – keď čidlo neposiela dáta alebo ESP hlási „Čakám na teplotu“ dlhšie ako
  `CIDLO_STALE_MIN` (60 min), príde jedno upozornenie a jedno pri obnovení.
- **Porovnanie čidiel** – tabuľka v karte *Izby*: pre každú možnosť hodiny, **rozptyl teplôt izieb** (hlavné kritérium),
  odchýlka riadiacej izby od cieľa, štarty kompresora a krátke cykly, kWh/deň, COP a priemer vonku. Pri menej ako 48 h
  dát alebo rozdielnom počasí (> 3 °C) zobrazí upozornenie.

- **Záložné čidlo (2.14.0)** – keď HA hlási hlavné čidlo ako nedostupné > 30 min (alebo 12 h bez hodnoty), firmvér
  sám prejde na druhé čidlo tej istej izby (Detská ESPresense ↔ Zigbee, Jedáleň ↔ hlavica) a pri prvej platnej hodnote
  sa vráti. Select sa nemení; web a HA ukážu „Záložné čidlo aktívne“.

### Vyregulovanie ventilov (2.14.0)
- Panel v karte *Izby*: zápis zmeny ventilu (izba, VK 33, −½ … +½ ot., poznámka). Po 24 a 48 h sa vyhodnotí rozptyl
  izieb pred/po, posun izby voči ostatným a či sa líšilo počasie; verdikt lepšie / horšie / bez zmeny.
- Upozorní, ak predošlá zmena ešte nie je vyhodnotená (jedna zmena naraz). Výsledky idú do týždenného súhrnu aj agentovi.

### Týždenný súhrn kúrenia (2.13.0, 2.14.0)
- Každý pondelok o 8:00 (nastaviteľné `SUHRN_DEN`, `SUHRN_HODINA`, vypnutie `SUHRN=0`) príde správcom súhrn
  predošlého týždňa: priemerné teploty a rozptyl izieb, vonku, spotreba a cena, COP, štarty kompresora, ohrevy TÚV,
  zásahy agenta, prepnutia riadiacej izby, zmeny ventilov s výsledkom, porovnanie riadiacich čidiel s odporúčaním,
  otvorené upozornenia a 2–3 vety odporúčania od AI.
- Ak agent v čase odoslania nebežal, súhrn dobehne neskôr v ten istý týždeň; nikdy nepríde dvakrát.
- Uloží sa do sekcie *Týždenný súhrn* na webe; `GET /api/suhrn?tyzden=YYYY-Www`.

### Firmvér ESP
- **Verzia firmvéru vo webe** (2.12.0) – ESP má text senzor „Verzia firmvéru“; pri staršom firmvéri ako `FW_MIN`
  web správcovi napíše, že treba nahrať nový `m5stack-tc.yaml`, namiesto tichého skrytia funkcií.
- **Základ + lokálny obal** (2.13.0) – všetky entity Home Assistant a IP sú v `substitutions`. Vlastné hodnoty sa
  dávajú do malého lokálneho súboru (`esphome/m5stack-tc.local.example.yaml`), základ z repa sa pri aktualizácii
  len nahradí – žiadne ručné prenášanie rozdielov.

## Pre správcu inštalácie

- **`update.sh`** – aktualizácia inštalácie z gitu: odmietne zmenené sledované súbory, len fast-forward, kontrola
  syntaxe, reštart 4 služieb, overenie webu; pri chybe vráti predchádzajúcu verziu. `--dry-run` ukáže, čo príde.
- **Monitor (port 8099)** berie izbové senzory z profilu (`izby[].esp` / `ha`) – netreba upravovať `monitor/web.py`.
  Riadok *riadiace čidlo* dopĺňa hodinové dáta z HA entity ESP, takže sleduje práve zvolené čidlo.
- Odpojenie prehliadača (BrokenPipe) sa už nevypisuje do logu ako chyba.
- `profil.json` je v `.gitignore`.

## Kvalita

- **CI** (`.github/workflows/testy.yml`) – pri každom pushi do `main` a pull requeste: `py_compile`, všetky `test_*.py`
  na Pythone 3.11 a 3.13, `tests_ha/` a kontrola syntaxe JavaScriptu (`ui.html`, `monitor/index.html`, `stats_ui.js`, `sw.js`).
- Nové testy: `test_cidlo.py`, `test_suhrn.py`, triedy `Riadiaca`, `RiadiacaHistoria`, `Nesulad`, `PorovnanieRiadiacich`
  v `test_v2_web.py`. `test_en_kompletne.py` už nezávisí od dnešného dátumu.

## Nové nastavenia (`config.env`)

| Kľúč | Predvolené | Význam |
|---|---|---|
| `CIDLO_STALE_MIN` | 60 | po koľkých minútach bez dát z riadiaceho čidla príde upozornenie (0 = vypnuté) |
| `SUHRN` | 1 | týždenný súhrn zapnutý |
| `SUHRN_DEN` | 1 | deň odoslania (1 = pondelok … 7 = nedeľa) |
| `SUHRN_HODINA` | 8 | hodina odoslania |

## Nové API

| Endpoint | Popis |
|---|---|
| `POST /api/riadiaca {moznost}` | prepne riadiacu izbu/čidlo (správca) |
| `GET /api/riadiaca/porovnanie?days=N` | porovnanie riadiacich čidiel (správca) |
| `GET /api/suhrn?tyzden=YYYY-Www` | týždenný súhrn (správca) |
| `GET /api/valves`, `POST /api/valve` | denník vyregulovania ventilov (správca) |
| `/api/status` | nové bloky `riadiaca` (+ `nesulad`, `zalozne`), `firmver`; upozornenie `cidlo` v `alerts` |
| `/api/series`, `/api/day` | blok `riadiaca` (prepnutia pre graf) |

## Upgrade

1. Firmvér: nahraj nový `esphome/m5stack-tc.yaml` (ideálne cez lokálny obal). Bez neho web ukáže výzvu na aktualizáciu
   a výber riadiacej izby nebude dostupný.
2. Server: `./update.sh` v inštalačnom adresári (alebo `git pull` + reštart služieb).
3. Voliteľne doplň nové kľúče do `config.env` (inak platia predvolené hodnoty).
