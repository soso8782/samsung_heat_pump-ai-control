# Kúrenie – špecifikácia rozšírení (verzia 2)

Existujúci systém (čítaj najprv kód): `common.py` (konfigurácia, DB, hodinové/denné štatistiky), `agent.py` (hodinový beh, učenie,
správy, mantinely `ALLOWED`), `app.py` (HTTP API + servíruje `ui.html`), `ui.html` (celá stránka, vanilla JS), `collector.py`
(zberač, NEMENIŤ), `backup.sh`. Iba štandardná knižnica Pythonu 3.11+, žiadne pip balíky. Texty pre používateľa po slovensky.
Produkcia beží na LXC v `/opt/kurenie` (služby kurenie-collector/-monitor/-web/-agent). Agent je v režime „Riadi“ nad skutočným
tepelným čerpadlom – chyby v zápisoch do ESP sú reálne. Kredit OpenRouter je nízky (0,34 $): volania modelu len nevyhnutne.

Všeobecné pravidlá
- Nič z existujúcej funkčnosti nesmie prestať fungovať. Schémy DB len rozširovať (`CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ADD COLUMN`
  v try/except). Všetky časy unix sekundy, lokálny čas Europe/Bratislava.
- Každý zápis do ESP ide cez `agent.esp_set_number()`. Ak je v prostredí `DRY_RUN=1` (alebo v config.env), funkcia NIČ neodošle,
  len zaloguje a vráti 200; overenie spätným čítaním sa v DRY_RUN považuje za úspešné. To isté platí pre `notify.send()` (len log).
- Mantinely: žiadna nová funkcia nesmie meniť iné entity než `ALLOWED` + výslovne uvedené nižšie (TÚV cieľ pri plánovači).
- `max_tokens` pre model vždy veľkorysé (≥ 2500) – slovenčina spotrebuje veľa tokenov; skrátený JSON = chyba.

## 1. Upozornenia (`notify.py`, nový súbor)
- `send(cfg, title, message, kind, cooldown_s=0) -> (ok, err)`: POST JSON `{"title":..., "message":..., "kind":...}` na `cfg["NOTIFY_URL"]`
  (HA webhook; timeout 10 s). Prázdne `NOTIFY_URL` → `(False, "nenastavené")` bez chyby. Cooldown: v `agent.db` tabuľka
  `notify_log(ts, kind, title, message, ok, error)`; ak bola pre `kind` úspešná správa pred menej než `cooldown_s`, neposielať.
- Udalosti a ich `kind` / cooldown:
  - `agent_zmena` (0): agent zapísal zmenu nastavenia – „Agent zmenil X: a → b. Dôvod…“
  - `agent_problem` (3 h): hodinové hodnotenie má `stav == "problem"`.
  - `agent_chyba` (6 h): 3 chybové behy agenta po sebe (status chyba).
  - `zberac` (3 h): dáta zberača staršie ako 15 min; `zberac_ok` po obnovení (len ak predtým odišlo `zberac`).
  - `esp` (3 h): ESP neodpovedá 3 kontroly po sebe; `chyba_tc` (1 h): chybový kód TČ ≠ 0 (uveď kód; E911 = nízky prietok).
  - `dohrev` (6 h): elektrický dohrev kúrenia zapnutý dlhšie ako 30 min.
  - `kredit` (24 h): zostatok vystačí na < 7 dní alebo < 0,10 $.
  - `vyhodnotenie` (0): hotové vyhodnotenie zásahu (bod 3) – krátky verdikt.
  - `tyzden` (0): týždenný súhrn (bod 8) – prvé 2 vety.
  - `tuv` (6 h): plánovač TÚV zrušený kvôli booster ohrevu (bod 6).
  - `test`: testovacia správa z webu.

## 2. Strážca (`watchdog` vlákno v `agent.py`)
- Samostatné daemon vlákno spustené v `main()`, slučka každých 60 s, vlastné DB spojenie, všetko v try/except (nesmie zhodiť agenta).
- Kontroly: vek `meta.last_sample` z tc.db; `esp_get` jednej entity (počítadlo zlyhaní v pamäti); chybový kód; dohrev (čas zapnutia
  drž v `state`); posledné 3 behy `runs` (status chyba, okrem „info“ a „rutina“); kredit (funkcia `balance` presunúť z app.py do
  common.py aby ju vedel volať agent aj web; cache 5 min ostáva).
- Aktívne poplachy ukladaj do `state` kľúč `alerts` ako JSON zoznam `[{"kind","since","text"}]` (web ich zobrazí). Poplach zmizne,
  keď podmienka pominie.

## 3. Zásahy a ich vyhodnotenie (`interventions.py`, nový súbor)
Tabuľka `interventions(id INTEGER PRIMARY KEY, ts, source, kind, room, text, status, due_ts, result TEXT, notified INTEGER DEFAULT 0)`.
- `source`: `user_message` | `user_manual` | `agent` | `settings`. `kind`: `ventil` | `krivka` | `nastavenie` | `vetranie` | `ine`.
  `room`: kľúč izby (`detska`, `jedalen`, `kuchyna`, `obyvacka`, `spalna`, `pracovna`, `kupelna`) alebo NULL. `status`: `caka` | `hotove` | `zrusene`.
- Vznik:
  a) Správa majiteľa: `chat_reply` prejde na JSON odpoveď modelu `{"odpoved": "...", "zasah": null | {"typ": "ventil|vetranie|ine",
     "izba": "<kľúč alebo null>", "popis": "krátko", "cas": "YYYY-MM-DD HH:MM alebo null"}}`. Ak je `zasah` vyplnený a typ je `ventil`
     alebo `ine` s trvalým účinkom, vlož zásah (`ts` = uvedený čas, inak čas správy; nikdy nie v budúcnosti). Do DB `messages` ukladaj
     ako doteraz len text odpovede. Pri zlyhaní JSON použi celý text ako odpoveď a zásah nevytváraj.
  b) Agent zapísal zmenu (`run_once`, vykonané akcie) → zásah `source=agent`, `kind=krivka` (FSV 2031/2032) alebo `nastavenie`.
  c) Ručná zmena sledovaných nastavení: pri každom behu agenta pozri `events` v tc.db pre entity `SETTINGS` (old IS NOT NULL) novšie
     než `state.last_settings_scan`; ak zmena nie je do ±3 min od `actions.applied=1` tej istej entity, vlož zásah `source=settings`.
     Viac zmien v okne 30 min zlúč do jedného zásahu (text vymenuje všetky).
- `due_ts = ts + 24 h`. Vyhodnotenie (`evaluate_due(cfg, c)`, volá sa pri každom behu agenta aj z watchdogu raz za 10 min):
  pre zásahy `caka` s `due_ts <= now` a s aspoň 20 h dát v oboch oknách:
  - okno PRED = `[ts-24h, ts)`, okno PO = `[ts, ts+24h)`; použi `hour_stats`.
  - pre každé okno: priemer vonku, priemer každej izby, `rozdiel` izba − detská, kWh, `kwh_na_stupen` (kWh / (23 − vonku), ak > 3 °C
    rozdiel), priemer `dt_beh`, počet štartov, beh %.
  - `delta` = PO − PRED pre izby, rozdiely, kwh_na_stupen, dt_beh.
  - `verdict_text` deterministicky po slovensky, 2–3 vety: ktorá izba sa najviac zmenila voči detskej (prah 0,2 °C), či sa cieľová izba
    zásahu pohla očakávaným smerom, zmena kWh/°C v %, upozornenie ak sa vonkajší priemer líši o > 3 °C („porovnanie je menej spoľahlivé“)
    alebo ak dt_beh PO > 6,5 („podozrenie na nízky prietok“).
  - ulož `result` JSON `{"before":{...}, "after":{...}, "delta":{...}, "verdict_text": "..."}`, `status=hotove`, pošli `vyhodnotenie`.
- Hotové vyhodnotenia za 14 dní a čakajúce zásahy pridaj do vstupu hodinového behu aj denného učenia (sekcia „ZÁSAHY A ICH VÝSLEDKY“).
  Kým zásah `ventil` čaká, agent nesmie meniť krivku: `check_action` odmietne FSV 2031/2032 s poznámkou „čaká sa na vyhodnotenie zásahu“.

### Denník vyregulovania ventilov
Záznam `kind=ventil` s `detail` (JSON: `label` default „VK 33“, `turns`, `change`, `note`); stĺpce `interventions.detail`, `interventions.result48` (ALTER v `common.py`).
Vyhodnotenie po 24 h (`result`) a po 48 h (`result48`, spoľahlivejšie; len záznamy s `detail`). Výsledok má blok `valve`: `spread_before/after/delta`
(priemer hodinového max−min cez izby), `room_vs_others_*` (izba − priemer ostatných), `vonku_delta`, `weather_differs` (> 3 °C), `verdict`
(`lepsie`/`horsie` pri zmene rozptylu ≥ 0,2 °C, inak `bez_zmeny`). Týždenný súhrn (`ventily`) a vstup modelu obsahujú zmeny ventilov s 48 h výsledkom.

## 4. Šetrenie volaní modelu („rutina“)
V `main()` pred `run_once`: `need, reasons = needs_llm(cfg, c, cur, hrs)`. Model sa volá, ak platí aspoň jedno:
- vynútený beh (`run_now`), nová správa majiteľa od posledného behu s modelom, nový zásah alebo hotové vyhodnotenie od posledného behu;
- od posledného behu s modelom ubehlo ≥ `LLM_MAX_GAP_H` hodín (config, predvolene 4, rozsah 1–12);
- chybový kód ≠ 0; dohrev zapnutý; dáta staré;
- posledná uzavretá hodina: detská mimo cieľa o > 0,5 °C; `dt_beh` > 6,5; štarty > 3; posun krivky ≥ max. kladný posun − 0,05
  alebo ≤ max. záporný + 0,05; niektorá izba pod svojím `min` (bod 7) o > 0,3 °C;
- vonkajší hodinový priemer sa od posledného behu s modelom zmenil o > 4 °C;
- zmena sledovaných nastavení od posledného behu s modelom.
Inak ulož do `runs` riadok `status="rutina"`, `stav="ok"`, `model=""`, cena 0, `summary` deterministická jedna veta
(„Bez zmien: detská 23,6 °C (cieľ 23,5), vonku 2,4 °C, ΔT 5,1 °C, kompresor 98 % hodiny. Model nebolo treba volať.“) a `input` = zoznam
skontrolovaných podmienok. Dôvody volania modelu pridaj na začiatok vstupu („PREČO SA TERAZ ROZHODUJEŠ: …“).

## 5. Záložný model
`OPENROUTER_FALLBACK_MODEL` (config, predvolene `google/gemini-2.5-flash-lite`, prázdne = vypnuté). `call_llm`: pri HTTP chybe 5xx/429,
timeoute alebo nevalidnom JSON (pri `as_json`) skús raz záložný model; do `runs.model` zapíš skutočne použitý. Chyba 401/402 (kľúč,
kredit) sa neopakuje.

## 6. Plánovač ohrevu vody (`dhw.py`, nový súbor) – predvolene VYPNUTÝ (`state.dhw_plan` = "0")
Cieľ: prihriať zásobník v najteplejších hodinách dňa, aby čerpadlo menej dohrievalo večer. Deterministicky, bez modelu.
- Raz denne po 06:00 z predpovede (open-meteo, rovnaký zdroj ako `forecast`) vyber 2-hodinové okno s najvyšším priemerom teploty medzi
  10:00–16:00 (bez polohy: 12:00–14:00). Ulož `state.dhw_window` JSON `{"date","start","end"}`.
- Na začiatku okna: ak plánovač zapnutý, agent nie je `off`, chybový kód 0, dáta čerstvé, booster vypnutý → prečítaj
  „Cieľová teplota TÚV“ (base), ulož `state.dhw_base`, nastav `min(base + 2, 52)` cez `esp_set_number`; `state.dhw_boosted="1"`.
  Ak base ≥ 52 alebo < 40 → nerob nič.
- Na konci okna (alebo ak je `dhw_boosted` a okno už neplatí, aj po reštarte): vráť `base`, `dhw_boosted="0"`. Zapíš do `actions`
  (reason „plánovač TÚV“), nie je to zásah na vyhodnotenie.
- Poistka: ak je počas okna zapnutý „Elektrický dohrev TÚV (Booster heater)“ → okamžite vráť base, vypni plánovač (`dhw_plan="0"`),
  pošli `tuv`. Volá sa z watchdog slučky (každú minútu). Vypnutie plánovača z webu počas okna musí base vrátiť.

## 7. Želané teploty izieb
`state.targets` JSON `{"jedalen":{"min":22.5}, "kuchyna":{"min":22.5}, "obyvacka":{"min":22.0}, "spalna":{"min":21.0},
"pracovna":{"min":21.0}, "kupelna":{"min":22.0}}` (predvolené; detská sa riadi cieľom v čerpadle a tu sa neuvádza). Hodnoty 15–26, krok 0,5.
V `SYSTEM` promptu nahraď napevno písané ciele izieb textom generovaným z `targets`; pridaj do vstupu a do `needs_llm`.

## 8. Týždenný súhrn
V nedeľu po 18:00 (raz, `state.last_weekly` = dátum): model dostane 7 denných súhrnov, zásahy s výsledkami, zmeny agenta, cenu za týždeň
(súčet `runs.cost` + `messages.cost` + `lessons.cost`), poučenia. Odpoveď JSON `{"suhrn": "5-8 viet", "odporucania": ["..."]}` →
tabuľka `reports(id, ts, week_from, week_to, text, cost)` (text = súhrn + odrážky odporúčaní), pošli `tyzden`. Bez kľúča preskoč.

## 9. Vrátenie zmeny
`actions` dostane stĺpec `undone INTEGER DEFAULT 0`; výsledky akcií v `runs.actions` JSON dostanú `"action_id"` (rowid z `actions`).
`undo(cfg, c, action_id)`: len `applied=1`, `undone=0`, entita v `ALLOWED`, nie staršie ako 72 h a iba ak je aktuálna hodnota v ESP stále
`new` (inak chyba „hodnota sa medzitým zmenila“). Zapíše `old`, overí, vloží nový riadok `actions` (reason „vrátené používateľom“,
applied=1), pôvodnému nastaví `undone=1`. Limity „raz za N hodín“ sa na vrátenie nevzťahujú; vrátená zmena sa do limitu nepočíta.

## 10. Čistenie databázy (`backup.sh`)
Po zálohe: v tc.db zmaž `samples` staršie ako `RETENTION_FULL_DAYS` (config, predvolene 60), ktoré nespĺňajú `ts % 300 < 60`
(ostane 5-min raster). Po dávkach (max 200 000 riadkov na beh), `timeout=60`, potom `PRAGMA wal_checkpoint(TRUNCATE)`. `events`, `notes`
sa nemažú. Vypíš počet zmazaných riadkov.

## 11. Prihlásenie na web
- Heslo nastaví majiteľ pri prvej návšteve. `state.auth_hash` = `pbkdf2_sha256$200000$<salt hex>$<hash hex>`. Min. dĺžka 8.
- Tabuľka `sessions(token_hash TEXT PRIMARY KEY, created, expires)`; cookie `kurenie_session` (32 B náhodný token, hex),
  `HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000`. V DB len SHA-256 tokenu. Expirované mazať pri prihlásení.
- 5 neúspešných pokusov z jednej IP za 10 min → 60 s blokovanie (v pamäti procesu).
- `GET /` a `GET /api/auth` sú vždy dostupné. Všetky ostatné `/api/*` bez platnej session → `401 {"error":"auth"}`.
  POSTy naďalej vyžadujú hlavičku `X-Kurenie: 1`.

## API – kontrakt medzi `app.py` a `ui.html` (JSON, UTF-8)
Existujúce endpointy ostávajú. Nové / zmenené:

| Metóda a cesta | Telo | Odpoveď |
|---|---|---|
| GET `/api/auth` | – | `{"setup_needed": bool, "logged_in": bool}` |
| POST `/api/setup` | `{"password"}` | len ak heslo ešte nie je; `{"ok":true}` + cookie; 400 `{"error"}` |
| POST `/api/login` | `{"password"}` | `{"ok":true}` + cookie; 401 `{"error":"Nesprávne heslo."}`; 429 `{"error":"Priveľa pokusov…"}` |
| POST `/api/logout` | `{}` | `{"ok":true}`, zmaže session |
| POST `/api/password` | `{"old","new"}` | `{"ok":true}` / 400 `{"error"}`; ostatné session zruší |
| GET `/api/status` | – | doterajšie polia + `alerts: [{"kind","since","text"}]`, `targets: {key:{"min":x}}`, `notify: {"configured":bool,"last_ok":ts|null,"last_error":str|null}`, `dhw: {"enabled":bool,"window":{"date","start","end"}|null,"boosted":bool,"base":x|null}`, `fallback_model: str`, `llm_max_gap_h: int`, `retention_days: int`, `runs_today_llm: int`, `runs_today_rutina: int` |
| POST `/api/settings` | navyše voliteľne `fallback_model` (str, môže byť ""), `llm_max_gap_h` (1–12), `notify_url` (http/https alebo ""), `dhw_plan` (bool) | `{"ok":true}` / 400 |
| POST `/api/notify_test` | `{}` | `{"ok":bool,"error":str|null}` |
| GET `/api/targets` | – | `{"rooms":[{"key","name","min"}]}` (poradie: jedáleň, kuchyňa, obývačka, spálňa, pracovňa, kúpeľňa) |
| POST `/api/targets` | `{"targets":{"jedalen":{"min":22.5},…}}` | `{"ok":true}` / 400 |
| GET `/api/interventions?limit=20` | – | `{"items":[{"id","ts","source","kind","room","text","status","due_ts","result":null|{before,after,delta,verdict_text}}]}` najnovšie prvé |
| POST `/api/intervention` | `{"text","kind"?,"room"?,"ts"?}` | `{"ok":true,"id"}` (source `user_manual`) |
| POST `/api/intervention_cancel` | `{"id"}` | `{"ok":true}` (len status `caka`) |
| GET `/api/valves?limit=20` | – | `{"items":[...]}` zásahy `kind=ventil` (navyše `detail:{label,turns,change,note}`, `result48`), len admin |
| POST `/api/valve` | `{"room","turns","label"?,"note"?,"ts"?}` | `{"ok":true,"id","warning":null\|text}` (`turns` záporné = priviera; varovanie, ak predchádzajúca zmena ventilu ešte čaká na vyhodnotenie) |
| POST `/api/undo` | `{"action_id"}` | `{"ok":true}` / 400 `{"error"}` |
| GET `/api/reports?limit=8` | – | `{"items":[{"id","ts","week_from","week_to","text","cost"}]}` |
| POST `/api/message` | `{"text"}` | doterajšie + `"intervention": null|{"id","kind","room","due_ts"}` |
| GET `/api/day` | – | doterajšie + `interventions` (zásahy s `ts` v danom dni, rovnaký tvar ako vyššie) |

`result.before` / `result.after`: `{"a","b","vonku","rooms":{key:x},"diff":{key:x},"kwh","kwh_na_stupen","dt_beh","starty","beh"}`;
`result.delta`: `{"vonku","rooms":{},"diff":{},"kwh_na_stupen","kwh_na_stupen_pct","dt_beh"}`.
Riadok v `runs`: `status` môže byť aj `"rutina"`; akcie majú `"action_id"` a `"vratene": bool`.

## Stránka (`ui.html`) – čo pribudne (zachovaj vizuálny jazyk: tokeny, písma, panely, pilulky; funguje vo svetlom aj tmavom režime, aj na mobile)
1. Prihlásenie: pri štarte `GET /api/auth`. `setup_needed` → obrazovka „Nastav si heslo“ (heslo 2×, min. 8 znakov). Neprihlásený →
   prihlasovacia obrazovka. Akýkoľvek `401` z API → späť na prihlásenie. V hornej lište tlačidlo „Odhlásiť“. V Nastaveniach zmena hesla.
   Počas prihlasovacej obrazovky sa nenačítavajú žiadne dáta a neukazuje zvyšok stránky.
2. Pás poplachov pod hornou lištou, keď `alerts` nie je prázdne (kritická farba, text + odkedy).
3. Karta Agent: „dnes N× model, M× rutina“; riadky `rutina` majú neutrálnu pilulku „Rutina“ (v tabuľke hodín aj v detaile).
   Pri vykonaných akciách mladších ako 72 h a nevrátených tlačidlo „Vrátiť“ (potvrdenie priamo v stránke, nie `confirm()`), po vrátení
   pilulka „Vrátené“.
4. Nový panel „Zásahy a ich výsledky“ (medzi Správami a Učením): zoznam zásahov; čakajúci ukazuje „vyhodnotí sa <deň hod>“ a tlačidlo
   „Zrušiť“; hotový ukazuje verdikt a rozbaľovaciu tabuľku pred / po / zmena pre izby (teplota aj rozdiel voči detskej), vonku, kWh/°C,
   ΔT. Formulár „Pridať zásah ručne“ (text, typ, izba, čas – predvolene teraz).
5. Nastavenia rozdeľ do prehľadných skupín: Model (kľúč, model, záložný model, interval, najdlhšia pauza medzi volaniami modelu 1–12 h
   s vysvetlením), Želané teploty izieb (číselné polia s krokom 0,5), Upozornenia (URL webhooku, „Poslať testovacie upozornenie“, stav
   posledného odoslania), Ohrev vody (prepínač plánovača s vysvetlením, dnešné okno, či práve prebieha), Heslo.
6. Panel „Týždenný súhrn“ (najnovší rozbalený, staršie zbalené), ak nejaký je.
7. Odpoveď na správu: ak `intervention` nie je null, pod odpoveďou agenta malá poznámka „Zapísané ako zásah, vyhodnotí sa <kedy>“.

## Kúrenie a TÚV podľa tarify (predvolene vypnuté)
- `tarifa.py`, `tarifa.tick` sa volá z watchdogu každú minútu. Config: `TARIFA_KURENIE=0`, `TARIFA_VT_INTERVALY` (napr. `06:00-08:00,17:00-19:00`;
  prázdne = tarifa neznáma = nič sa nerobí), `TARIFA_PREDSTIH_H=2`, `TARIFA_PREDKURENIE_C=0.5`, `TARIFA_UTLM_C=0.5` (obe 0–1,0, krok 0,5),
  `TARIFA_BEZ_KURENIA_C=12`.
- Pred blokom VT (`PREDSTIH_H`) sa cieľ izby zvýši o `PREDKURENIE_C`, počas VT zníži o `UTLM_C`, po VT sa vráti pôvodný. Zapisuje sa iba
  `ROOM_TARGET_ENT` cez `agent.esp_set_number` (rozsah profilu, krok 0,5), každá zmena je riadok v `actions` s dôvodom „tarifa: …“.
- Poistky: predhrev sa preskočí pri teplej predpovedi (priemer okna >= `BEZ_KURENIA_C`), v lete a bez predpovede; útlm sa nerobí (a prebiehajúci
  sa ruší), ak je niektorá izba pod minimom + 0,3 °C alebo nie sú čerstvé teploty izieb; pri A/B teste, vypnutom agentovi sa nič nemení
  (nami upravený cieľ sa vráti); ručnú zmenu cieľa používateľom nikdy neprepíše; po neúspešnom zápise sa o 5 min skúsi znova.
- Režim `advise`: len odporúčanie (`state.tarifa_advice`, `status.tarifa.advice`, riadok `runs` pri zmene fázy). `control`: zápis.
- Vyhodnotenie: `GET /api/tarifa` -> `{stav, vyhodnotenie}`; kWh čerpadla vo VT/NT po dňoch (state `tarifa_dni`), podiel VT pred / po zapnutí
  (`tarifa_od`), odhad úspory = (podiel pred - po) x kWh po zapnutí x (cena VT - cena NT), najmenej 3 dni pred aj po. Riadok v záložke Spotreba.
- TÚV v lacnej tarife: `TUV_TARIFA=0` (vyžaduje `TARIFA_VT_INTERVALY`). `dhw.pick_window_tarifa` vyberie 2-hodinové okno úplne v NT s najvyšším
  priemerom predpovede (najprv štart 10..14, potom 0..22); stále jedno okno za deň ako doteraz. Bez NT okna / predpovede / známej tarify platí
  pôvodný `pick_window`. Okno má pole `reason` (`nt_teplo`, `nt`, `teplo`), zobrazené v Nastaveniach pri plánovači TÚV.
