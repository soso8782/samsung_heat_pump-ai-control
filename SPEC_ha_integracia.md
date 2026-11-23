# SPEC: integrácia do Home Assistanta „Samsung Heat Pump AI Control“ (verzia 2.3.0, 16. 11. 2026)

Cieľ: vlastná integrácia (custom component, inštalovateľná cez HACS ako custom repository) `custom_components/kurenie_ai/`,
ktorá sa pripojí na web aplikácie Samsung Heat Pump AI Control a v HA vytvorí zariadenie s entitami na sledovanie aj ovládanie.
Existujúce MQTT senzory (`ha_pub.py`, spotreba/COP pre Energy panel) ostávajú – integrácia ich neduplikuje
(spotrebu/COP neobsahuje, len odkaz v README).

## Strana aplikácie – modul `ha_api.py` (nový) + malé napojenie v app.py (napojenie urobí hlavný agent, ty nie)
API pre stroje, verzia `v1`, autorizácia hlavičkou `Authorization: Bearer <kľúč integrácie>`:
- Kľúč: 32 bajtov `secrets.token_urlsafe`, v `agent.db` (`state`) sa ukladá len SHA-256 (`ha_api_key_hash`) + čas vytvorenia.
  `ha_api.new_key(c) -> kľúč` (zobrazí sa raz), `ha_api.revoke(c)`, `ha_api.check(c, header) -> bool` (porovnanie cez
  `hmac.compare_digest`; bez nastaveného kľúča vždy False). Žiadne logovanie kľúča.
- `ha_api.build_state(st, now)` – z výstupu `app.status(cfg)` (slovník `st`; prečítaj si ho v app.py, nemeň app.py) zostaví:
```json
{"api": 1, "verzia": "<VERSION>", "ts": 0,
 "zariadenie": {"nazov": "", "model": "", "typ": "", "vykon_kw": 0, "kurenie": ""},
 "izby": [{"kluc": "", "nazov": "", "teplota": 0, "min": 0, "referencna": true}],
 "vonku": 0, "ciel_izba": 23.5, "posun": 0,
 "voda": {"vystup": 0, "spiatocka": 0, "ciel_krivky": 0, "delta_t": 0},
 "kompresor": {"frekvencia": 0, "bezi": true, "odmrazovanie": false},
 "tuv": {"teplota": 0, "ciel": 0, "ohrev_bezi": false} ,
 "agent": {"rezim": "off|advise|control", "posledny_beh": 0, "stav": "", "zhrnutie": "", "dalsi_beh": 0},
 "chyba_tc": 0, "poplachy": [{"druh": "", "text": ""}], "online": {"esp": true, "zberac": true, "ha": true}}
```
  Chýbajúce údaje = `null` (nie pád). `tuv` = `null`, ak inštalácia nemá TÚV (`profil_info.funkcie.tuv`).
- Akcie (POST, telo JSON) – `ha_api.ACTIONS` = mapa cesta → názov funkcie v app.py, ktorú zavolá hlavný agent:
  `/api/ha/v1/mode` {mode} → rovnaká logika ako `/api/mode`; `/api/ha/v1/room_target` {value} → `room_target_post`;
  `/api/ha/v1/tuv_boost` {} → `tuv_boost_post`; `/api/ha/v1/run_now` {} → ako `/api/run_now`.
  `ha_api.validate_action(path, body) -> (ok, chyba)` – validácia vstupov (mode v {off, advise, control}, value číslo 15–26).
- GET `/api/ha/v1/state` → `build_state`. Nesprávny/chýbajúci kľúč → 401 `{"error": "unauthorized"}`.
- Nastavenia na webe (urobí hlavný agent): karta „Integrácia Home Assistant“ – Vytvoriť kľúč / Zrušiť kľúč.
- Testy `test_ha_api.py`: kľúč (vytvorenie, overenie, zrušenie, nesprávny), `build_state` z ukážkového `st` (aj bez TÚV,
  s chýbajúcimi hodnotami), `validate_action`.

## Strana HA – `custom_components/kurenie_ai/`
Moderný HA (2025+), `DataUpdateCoordinator`, `aiohttp` cez `async_get_clientsession`, `config_flow` (UI), preklady sk + en
(`translations/sk.json`, `translations/en.json`, `strings.json`), `manifest.json` (`domain: kurenie_ai`, `iot_class: local_polling`,
`config_flow: true`, `version: "2.3.0"`, `codeowners: ["@soso8782"]`, `documentation`/`issue_tracker` na GitHub repo).
- **Config flow:** adresa aplikácie (napr. `http://10.10.40.30`) + kľúč integrácie; overenie GET `/api/ha/v1/state`
  (chyby: cannot_connect, invalid_auth); unique_id z adresy. Options flow: interval obnovy (15–300 s, predvolene 30).
  Reauth flow pri 401.
- **Zariadenie:** jedno „Samsung Heat Pump AI Control – <zariadenie.nazov>“ (manufacturer Samsung, model z `zariadenie.model`, sw_version = verzia aplikácie,
  configuration_url = adresa aplikácie).
- **Entity** (has_entity_name, translation_key, unique_id = `<entry_id>_<kľúč>`):
  - sensor: vonkajšia teplota, každá izba (teplota, atribút min a referenčná), výstup/spiatočka/cieľ krivky/ΔT vody,
    frekvencia kompresora, posun krivky, TÚV teplota (ak je), chybový kód, stav agenta (enum), posledné hodnotenie agenta
    (text skrátený na 255, celý v atribúte), počet poplachov (atribút zoznam).
  - binary_sensor: kompresor beží, odmrazovanie, ohrev TÚV, problém (poplach alebo chyba ≠ 0, device_class problem),
    pripojenia ESP / zberač / HA (connectivity).
  - select: režim agenta (off/advise/control) → POST mode.
  - number: cieľová izbová teplota (min/max z rozsahu aplikácie, krok 0,5) → POST room_target.
  - button: spustiť agenta teraz, ohrev TÚV (ak je TÚV).
- Po akcii okamžitý refresh koordinátora. Pri chybe akcie `HomeAssistantError` so zrozumiteľnou správou.
- `hacs.json` v koreni repozitára (`{"name": "Samsung Heat Pump AI Control", "render_readme": true}`) a `custom_components/kurenie_ai/README.md`
  (inštalácia cez HACS custom repository alebo ručne, vytvorenie kľúča v aplikácii, zoznam entít).
- Testy: `tests_ha/` – unit testy bez Home Assistanta (parsovanie stavu → hodnoty entít, mapovanie akcií); ak sa dá, ľahké
  overenie syntaxe všetkých .py (`python -m py_compile`) a platnosti JSON súborov. HA sa v tomto prostredí nespúšťa.

## Pravidlá
Súbory: `ha_api.py`, `test_ha_api.py`, `custom_components/kurenie_ai/**`, `hacs.json`, `tests_ha/**`. **Nemeň** app.py, ui.html,
locales ani iné existujúce súbory (súčasne beží iná úloha, ktorá ich mení). Nič nenasadzuj, žiadne ssh/scp/git.
Python: "C:\Program Files\KiCad\10.0\bin\python.exe" -X utf8 z priečinka gh_kurenie.
Odovzdanie: zoznam súborov, výstup testov, presný popis, ako má hlavný agent napojiť ha_api do app.py (routy, auth, akcie)
a čo pridať do ui.html, a čo si nevedel overiť.

## Doplnok: entity cez MQTT (`ha_ent.py`, bez integrácie)
Vlákno agenta `ha_ent` (vedľa `ha_pub`) publikuje cez MQTT discovery na **to isté zariadenie** (`identifiers: kurenie_tc`) ďalšie entity;
stav v `kurenie/tc/ent/state` (JSON, retained, raz za 30 s a hneď po príkaze), dostupnosť `kurenie/tc/ent/avail` (`online` / last will `offline`).
Beží v agentovi, lebo ten beží stále a drží `utlm.tick`; jedno dlhé spojenie, reconnect 5 -> 60 s. `MQTT_PUB=0` = nič sa neposiela.

| Entita | Typ | Poznámka |
|---|---|---|
| Preč z domu | switch (pri `MQTT_OVLADANIE=0` binary_sensor) | ON = `utlm.away_start` bez času návratu, OFF = zrušiť |
| Nočný útlm – režim, Preč z domu – režim | select off/advise/control (pri 0 enum senzor) | rovnaké režimy ako `/api/utlm` |
| Nočný útlm – stav / ďalšia zmena | sensor enum normal/night/ramp/away / timestamp | |
| Tarifa – fáza / aktívny posun | sensor norm/pred/vt / °C | |
| Zdravie čerpadla | sensor ok/warn/crit/nodata, atribúty `indikatory`, `chyby`, `nalezy` | obnova 10 min |
| Zdravie čerpadla – problém | binary_sensor (problem) | warn alebo crit |
| Odhad nákladov do konca mesiaca | sensor € (`naklady.odhad`) | najviac raz za 30 min |
| Verzia aplikácie, Dostupná aktualizácia | sensor, binary_sensor (`verzia.info`) | diagnostika |

Príkazové témy (iba pri `MQTT_OVLADANIE=1`; inak sa ani neodoberajú): `kurenie/tc/ent/cmd/away` (`ON`/`OFF`), `.../cmd/utlm_mode`, `.../cmd/prec_mode`
(`off`/`advise`/`control`). Poistky: neplatný payload sa ignoruje; retained príkazy sa ignorujú; pri `DRY_RUN=1` sa nič nevykoná (len záznam do runs);
vykonanie ide cez `utlm.away_start` / `away_cancel` / `state` ako web, takže platí A/B test aj vypnutý režim „Preč“ (odmietnutie sa zapíše do runs).
V logu je „kto“ = `Home Assistant (MQTT)`. Zmena `MQTT_OVLADANIE` mení doménu entity; stará retained konfigurácia sa vymaže.

Príklad automatizácie (všetci preč -> Preč z domu, návrat -> zrušiť):
```yaml
automation:
  - alias: "Kúrenie: preč z domu"
    trigger: {platform: numeric_state, entity_id: zone.home, below: 1, for: "01:00:00"}
    action: {service: switch.turn_on, target: {entity_id: switch.kurenie_tc_away}}
  - alias: "Kúrenie: niekto doma"
    trigger: {platform: numeric_state, entity_id: zone.home, above: 0}
    action: {service: switch.turn_off, target: {entity_id: switch.kurenie_tc_away}}
```
(`entity_id` závisí od názvu zariadenia v HA; skontroluj ho v detaile entity.)
