# ESPHome most pre Samsung EHS (NASA zbernica)

[![English](https://img.shields.io/badge/lang-English-blue.svg)](README.md) [![Slovenčina](https://img.shields.io/badge/jazyk-Sloven%C4%8Dina-green.svg)](README.sk.md)

Aplikácia Samsung Heat Pump AI Control nekomunikuje s tepelným čerpadlom priamo. Číta ho cez malý ESP32 modul s ESPHome
([`Beormund/esphome-samsung-nasa`](https://github.com/Beormund/esphome-samsung-nasa)), ktorý je pripojený na
dátovú zbernicu **NASA** (svorky **F1/F2**) a cez `web_server` (REST + SSE) sprístupňuje entity po sieti.

**Odporúčaný začiatok:** [`m5stack-tc.yaml`](m5stack-tc.yaml) je **overená, reálne bežiaca konfigurácia** referenčnej inštalácie
(Split AE120JXEDGH, gen. J) – vrátane regulácie posunu krivky podľa izbovej teploty, časového plánu a hybridného režimu.
Needituj ju: všetky hodnoty špecifické pre inštaláciu (entity Home Assistant izbových čidiel, IP adresa) sú v `substitutions` na začiatku a prepisujú sa z lokálneho obalu (pozri nižšie); heslá sú v `secrets.yaml`.

Súbor [`samsung-ehs.yaml`](samsung-ehs.yaml) je zjednodušený **návrh** (neoverený) – vzorová konfigurácia, ktorej **názvy entít sú rovnaké ako v predvolenom profile
aplikácie** (`profil.default()["esp"]`). Ak ju použiješ bez zmeny názvov, v sprievodcovi „Zariadenie a dom“ stačí potvrdiť návrh.

> Súbor nebol skompilovaný pre tvoj model. Čísla NASA správ označené „overené“ pochádzajú z príkladu komponentu,
> riadky `TODO` je treba doplniť podľa tvojho modelu (viď nižšie). Chýbajúca entita nevadí – funkcia, ktorá ju potrebuje, sa vypne.

## Čo budeš potrebovať

- M5Stack Atom (Lite/Matrix) a **Atomic RS485 Base** (alebo iná doska ESP32 + prevodník TTL–RS485).
- Napájanie 5 V (z `V1`/`V2` na riadiacej doske jednotky cez menič, alebo samostatný USB adaptér).
- Počítač s ESPHome (`pip install esphome` alebo doplnok ESPHome v Home Assistant) a USB kábel na prvé nahratie.

## Zapojenie

Zbernica NASA je RS485 (9600 baud, parita even). Na riadiacej doske (MIM) vnútornej jednotky:

| RS485 základňa | svorka na doske |
|---|---|
| A (žltá) | F1 |
| B (zelená) | F2 |
| V+ (červená) | V1 |
| G (čierna) | V2 |

- F3/F4 sú určené pre drôtové ovládače – nepripájaj tam nič.
- V pôvodnej doske sú `V1`/`V2` zvyčajne zoskupené vpravo hore. Over napätie meraním, než pripojíš modul.
- Pri zapojení vypni istič jednotky. Zásahy do riadiacej dosky robíš na vlastnú zodpovednosť.
- UART piny sú v YAML ako premenné: `uart_tx: GPIO19`, `uart_rx: GPIO22` (platí pre Atomic RS485 Base). Pri inej doske ich zmeň.

## Nahratie

1. Skopíruj `samsung-ehs.yaml` do priečinka ESPHome a vedľa vytvor `secrets.yaml`:

   ```yaml
   wifi_ssid: "moja-wifi"
   wifi_password: "..."
   ap_password: "..."
   api_key: "<32 bajtov base64, vygeneruje ESPHome>"
   ota_password: "..."
   web_user: "kurenie"
   web_password: "..."
   ```

2. Prvé nahratie cez USB: `esphome run samsung-ehs.yaml`. Ďalšie aktualizácie idú cez Wi‑Fi (OTA).
3. Zisti IP adresu modulu (rezervuj ju v routeri) a v `config.env` aplikácie nastav `ESP_URL=http://<ip>`.
   Ak zapneš `web_server.auth`, treba, aby aplikácia vedela prihlasovacie údaje – alebo `auth` v YAML vynechaj a modul
   nechaj len v domácej sieti.
4. Otvor `http://<ip>/` – v tabuľke by si mal vidieť entity so slovenskými názvami.

## Lokálny obal (aktualizácia bez ručného porovnávania)

`m5stack-tc.yaml` nechaj ako nezmenený základ a vlastné hodnoty daj do malého obalu vedľa neho:

1. Skopíruj `m5stack-tc.local.example.yaml` na `m5stack-tc.local.yaml` (vedľa `m5stack-tc.yaml` a `secrets.yaml`) a nastav `substitutions` (`esp_ip`, `ha_izba_jedalen`, `ha_izba_obyvacka`, ... - zoznam je na začiatku `m5stack-tc.yaml`). Uveď len tie, ktoré sa líšia od predvolených.
2. Kompiluj a nahrávaj **obal**: `esphome run m5stack-tc.local.yaml`. Substitúcie v hlavnom (obalovom) súbore majú prednosť pred tými v pripojenom balíku.
3. Pri aktualizácii firmvéru stačí nahradiť `m5stack-tc.yaml` novou verziou z repozitára a znova nahrať; lokálny súbor ostáva nedotknutý. Skontroluj, či pribudli nové názvy v bloku `substitutions`.

Hodnoty `!secret` sa čítajú zo `secrets.yaml` v priečinku obalu.

## Dopĺňanie čísel správ (TODO riadky)

Číslo NASA správy závisí od modelu a firmvéru. Postup:

1. V `samsung_nasa:` nastav `debug_log_undefined_messages: true`, v `logger.logs` daj `samsung_nasa: DEBUG`.
2. Sleduj log (`esphome logs samsung-ehs.yaml`) a zmeň na jednotke hodnotu (napr. spusti odmrazovanie, zmeň cieľ), aby si videl,
   ktorá správa sa mení.
3. Priraď číslo ku zakomentovanej entite (názov nemeň) a odkomentuj ju. Po overení vráť logovanie na `ERROR`.

Nepovinné entity, ktoré v YAML zostanú zakomentované, v profile nechaj prázdne (sprievodca to urobí sám, ak ich v zozname ESP nenájde).

## Povinné entity pre aplikáciu

`vonku`, `tw2` (výstup), `tw1` (spiatočka), `freq` (frekvencia kompresora), `wl` (cieľová voda), `posun` (posun krivky),
`fsv2031`, `fsv2032`. Bez `freq`/`wl`/`posun` sa nezobrazia niektoré grafy a agent nemá z čoho posudzovať krivku – doplň ich.

## Izby

Izbové teploty (pole `esp` v izbách profilu) môžu pochádzať z ľubovoľného zdroja, ktorý ESP vystaví (Zigbee, BLE, entity
z Home Assistantu cez `platform: homeassistant`). Alternatíva: v profile nechaj `esp` prázdne a použi len HA senzory
(pole `ha`) – izba potom nemá živú hodnotu z ESP, ale história a štatistiky z HA fungujú.

**Riadiaca izba.** Select „Riadiaca izba“ v `m5stack-tc.yaml` určuje, podľa ktorého čidla beží izbová regulácia; web ho prepína kliknutím na izbu v karte Izby (správca). Aplikácia páruje možnosť s izbou podľa názvu – „<názov izby>“ alebo „<názov izby> (<čidlo>)“, ak má izba viac čidiel. Pri pridaní izby/čidla doplň možnosť do selectu aj vetvu v skripte `riadiaca_nastav` (rovnaké poradie) a čidlu daj `id` a `on_value` ako ostatným.
