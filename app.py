#!/usr/bin/env python3
"""Web agenta kúrenia: stav, vypínač agenta, denný kalendár po hodinách, zásahy, upozornenia, prihlásenie."""
import datetime, hashlib, hmac, html as _html, json, os, re, secrets, shutil, socket, sqlite3, sys, threading, time, unicodedata, urllib.request
import profil
import i18n
from i18n import tr, tn
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from common import (REF_ROOM, ROOM_NAMES, ROOMS, ROOT, S, TARGET_ROOMS, adb, balance, day_bounds, day_summary, get_state, get_targets,
                    hour_stats, is_dry, load_cfg, local_midnight, save_cfg, set_state, tcq, cfg_int)
from agent import ALLOWED, ROOM_TARGET_ENT, UNDO_MAX_AGE_S, call_llm, chat_reply, parse_room_target, set_room_target, undo, update_days
import agent as _agent_mod
import abtest, ha_api, ha_auth, cfjwt, cftunnel, contacts, contacts_mqtt, dhw, interventions, mcp_api, notify, spotreba, temps, vonku
try:   # štatistiky (stats.py): ak modul chýba, web ostane funkčný a /api/stats/* vráti 503
    import stats
except Exception:
    stats = None

COOKIE = "kurenie_session"
SESSION_TTL = 2592000
PBKDF2_ITER = 200000


# Zmena cieľovej izbovej teploty (web správcu aj chat používateľa idú cez agent.set_room_target): po úspešnej zmene sa vo vlákne
# pošle upozornenie `cielova_zmena` všetkým zariadeniam. agent.py sa nemení - funkcia sa obalí tu (chat volá agent.set_room_target
# cez globálnu premennú modulu agent, takže obal platí aj pre chat).
def _set_room_target_notify(cfg, c, value, who, lang="sk"):
    res = _orig_set_room_target(cfg, c, value, who, lang)
    try:
        threading.Thread(target=notify.target_changed, args=(dict(cfg), res.get("old"), res.get("new"), who),
                         name="notify-cielova-zmena", daemon=True).start()
    except Exception:
        pass
    return res


_orig_set_room_target = set_room_target
set_room_target = _agent_mod.set_room_target = _set_room_target_notify


def messages(cfg, limit=60, a=None, b=None):
    c = adb(cfg)
    if a is not None:
        rows = c.execute("SELECT id, ts, role, text, cost, author FROM messages WHERE ts>=? AND ts<? ORDER BY ts", (a, b)).fetchall()
    else:
        rows = c.execute("SELECT id, ts, role, text, cost, author FROM messages ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()[::-1]
    c.close()
    return [dict(zip(("id", "ts", "role", "text", "cost", "author"), r)) for r in rows]

MODES = ("off", "advise", "control")


def run_row(r):
    keys = ("id", "ts", "mode", "status", "stav", "summary", "advice", "model", "actions", "error", "tok_in", "tok_out", "cost")
    d = dict(zip(keys, r))
    try:
        d["actions"] = json.loads(d["actions"] or "[]")
    except ValueError:
        d["actions"] = []
    return d


def enrich_actions(c, runs):
    """Doplní k akciám v behoch `action_id` (aj pre staré behy podľa ts+entity) a `vratene` (podľa actions.undone)."""
    if not any(r["actions"] for r in runs):
        return runs
    lo = min(r["ts"] for r in runs if r["actions"])
    rows = c.execute("SELECT rowid, ts, entity, coalesce(undone,0) FROM actions WHERE ts>=? AND reason!='vrátené používateľom'", (lo,)).fetchall()
    by_id = {r[0]: r for r in rows}
    by_key = {}
    for rid, ts, ent, und in rows:
        by_key.setdefault((ts, ent), rid)
    for run in runs:
        for a in run["actions"]:
            rid = a.get("action_id") or by_key.get((run["ts"], a.get("entita")))
            a["action_id"] = rid
            a["vratene"] = bool(rid in by_id and by_id[rid][3])
            a["vratit_do"] = (run["ts"] + UNDO_MAX_AGE_S) if rid else None
    return runs


RUN_COLS = "id,ts,mode,status,stav,summary,advice,model,actions,error,tok_in,tok_out,cost"


def prevadzka(cfg, now=None):
    """Čo tepelné čerpadlo práve robí (štítok vedľa loga): {"rezim": "kurenie"|"tuv"|"odmrazovanie"|"stoji"|None, "od": ts|None}.
    Kompresor podľa frekvencie v poslednej vzorke, smer z udalostí 3-cestného ventilu (Tank = TÚV, Room = kúrenie),
    odmrazovanie z udalostí. Pri starých dátach (zberač ticho > 10 min) alebo chybe vráti rezim None."""
    now = int(now or time.time())
    out = {"rezim": None, "od": None}
    try:
        ls = int(dict(tcq(cfg, "SELECT k, v FROM meta")).get("last_sample") or 0)
        if not ls or now - ls > 600:
            return out
        r = tcq(cfg, "SELECT value FROM samples WHERE ts=? AND entity=?", (ls, S.get("freq", "")))
        hz = float(r[0][0]) if r and r[0][0] is not None else None

        def last_ev(key):
            ent = profil.device_entity(key)
            if not ent:
                return None, None
            r = tcq(cfg, "SELECT new, ts FROM events WHERE entity=? ORDER BY ts DESC LIMIT 1", (ent,))
            return (str(r[0][0] or ""), int(r[0][1])) if r else (None, None)
        ventil, v_ts = last_ev("ventil")
        odmraz, o_ts = last_ev("odmrazovanie")
        if odmraz and odmraz.strip().upper() == "ON":
            out.update(rezim="odmrazovanie", od=o_ts)
        elif hz is None:
            return out
        elif hz <= 0:
            r = tcq(cfg, "SELECT max(ts) FROM samples WHERE entity=? AND value>0 AND ts>?", (S.get("freq", ""), now - 86400))
            out.update(rezim="stoji", od=int(r[0][0]) + 60 if r and r[0][0] else None)
        else:
            tuv = bool(ventil) and "Tank" in ventil
            r = tcq(cfg, "SELECT max(ts) FROM samples WHERE entity=? AND value<=0 AND ts>?", (S.get("freq", ""), now - 86400))
            start = int(r[0][0]) + 60 if r and r[0][0] else None
            od = max(x for x in (start, v_ts) if x) if (start or v_ts) else None
            out.update(rezim="tuv" if tuv else "kurenie", od=od)
    except Exception:
        return {"rezim": None, "od": None}
    return out


LIVE_MAX_AGE = 900   # s: izbová teplota priamo z MQTT (meta `live:<izba>` od zberača) sa ukáže, ak je novšia než posledná vzorka


def live_overlay(meta, cur, names, ls, now):
    """Prepíše aktuálne hodnoty izieb čerstvejšími z MQTT (zapisuje collector.mqtt_room). Staršie než vzorka alebo než LIVE_MAX_AGE sa ignorujú."""
    for k, v in meta.items():
        if not k.startswith("live:") or k[5:] not in names:
            continue
        try:
            d = json.loads(v)
            t, ts = float(d["v"]), int(d["ts"])
        except (ValueError, TypeError, KeyError):
            continue
        if ts > ls and now - ts <= LIVE_MAX_AGE:
            cur[k[5:]] = t
    return cur


def status(cfg, role="admin", email=None, lang="sk"):
    if role != "admin":
        return status_user(cfg, role, email, lang)
    c = adb(cfg)
    now = int(time.time())
    last = c.execute("SELECT %s FROM runs ORDER BY ts DESC LIMIT 1" % RUN_COLS).fetchone()
    mid = local_midnight()
    cost_day = c.execute("SELECT coalesce(sum(cost),0), count(*) FROM runs WHERE ts>=?", (mid,)).fetchone()
    cost_30 = c.execute("SELECT coalesce(sum(cost),0) FROM runs WHERE ts>=?", (now - 30 * 86400,)).fetchone()[0]
    last_run = enrich_actions(c, [run_row(last)])[0] if last else None
    # Posledné hodnotenie agenta: záznamy „info“ (zmena cieľa, režimu, sezóny…) nemajú stav, nesmú ho prekryť.
    ev = c.execute("SELECT %s FROM runs WHERE stav IN ('ok','pozor','problem') ORDER BY ts DESC LIMIT 1" % RUN_COLS).fetchone()
    last_assessment = run_row(ev) if ev else None
    out = {"role": role, "email": email,
           "now": now, "mode": get_state(c, "mode", "advise"), "running": get_state(c, "running") == "1",
           "next_run": float(get_state(c, "next_run", "0") or 0), "model": cfg["OPENROUTER_MODEL"],
           "key_set": bool(cfg["OPENROUTER_API_KEY"]), "interval": int(cfg["INTERVAL_MIN"] or 60),
           "avg_cost": c.execute("SELECT avg(cost) FROM (SELECT cost FROM runs WHERE status='ok' AND cost>0 ORDER BY ts DESC LIMIT 24)").fetchone()[0],
           "key_hint": ("…" + cfg["OPENROUTER_API_KEY"][-4:]) if len(cfg["OPENROUTER_API_KEY"]) > 12 else "", "last_run": last_run, "last_assessment": last_assessment,
           "cost_today": cost_day[0], "runs_today": cost_day[1], "cost_30d": cost_30,
           "limits": {k: {"min": v[0], "max": v[1], "krok": v[2], "hodin": v[3]} for k, v in ALLOWED.items()}}
    # nové polia (SPEC – kontrakt)
    try:
        out["alerts"] = json.loads(get_state(c, "alerts", "") or "[]")
    except ValueError:
        out["alerts"] = []
    out["targets"] = get_targets(c)
    out["notify"] = notify.status(cfg, c)
    win = dhw.today_window(c)
    boosted = get_state(c, "dhw_boosted", "0") == "1"
    try:
        base = float(get_state(c, "dhw_base")) if boosted else None
    except (TypeError, ValueError):
        base = None
    out["dhw"] = {"enabled": dhw.enabled(c), "window": win, "boosted": boosted, "base": base}
    out["tuv"] = dhw.manual_status(c)
    out["fallback_model"] = (cfg.get("OPENROUTER_FALLBACK_MODEL") or "").strip()
    out["llm_max_gap_h"] = cfg_int(cfg, "LLM_MAX_GAP_H", 4, 1, 12)
    out["retention_days"] = cfg_int(cfg, "RETENTION_FULL_DAYS", 60, 1, 3650)
    out["runs_today_llm"] = c.execute("SELECT count(*) FROM runs WHERE ts>=? AND status='ok' AND coalesce(model,'')!=''", (mid,)).fetchone()[0]
    out["runs_today_rutina"] = c.execute("SELECT count(*) FROM runs WHERE ts>=? AND status='rutina'", (mid,)).fetchone()[0]
    out["dry_run"] = is_dry(cfg)
    out["ui_lang"] = i18n.home_lang(cfg)
    try:
        out["season"] = contacts.season(c)
        out["windows_open"] = [{"label": o["label"], "group": o["group"], "minutes": o["minutes"], "room": o["room"]} for o in contacts.open_now(c, now)]
        out["ha"] = contacts.ha_status(cfg, c, lang)
    except Exception:
        out["season"], out["windows_open"], out["ha"] = "zima", [], {"configured": False, "ok": False, "last_ok": None, "error": None, "count": None, "token_hint": ""}
    try:
        out["abtest"] = abtest.status_brief(c, now)
    except Exception:
        out["abtest"] = None
    c.close()
    try:
        out["balance"] = balance(cfg)
    except Exception:
        out["balance"] = None
    try:
        meta = dict(tcq(cfg, "SELECT k, v FROM meta"))
        ls = int(meta.get("last_sample") or 0)
        out["collector_age"] = now - ls
        out["first_day"] = time.strftime("%Y-%m-%d", time.localtime(int(meta.get("start") or now)))
        names = list(S.values()) + list(ALLOWED) + ["Cieľová izbová teplota", "Maximálny posun krivky", TUV_CIEL_ENT]
        rows = tcq(cfg, "SELECT entity, value FROM samples WHERE ts=? AND entity IN (%s)" % ",".join("?" * len(names)),
                   (ls, *names))
        out["current"] = live_overlay(meta, {e: v for e, v in rows}, names, ls, now)
    except Exception as e:
        out["collector_error"] = str(e)
    out["stats"] = stats_brief(cfg, lang)
    out["profil_info"] = profil_info(cfg)
    out["riadiaca"] = riadiaca_info(cfg)
    out["prevadzka"] = prevadzka(cfg, now)
    try:
        ca = adb(cfg)
        try:
            out["ha_api"] = ha_api.key_info(ca)
            out["mcp"] = mcp_api.key_info(ca)
        finally:
            ca.close()
    except Exception:
        out["ha_api"] = None
    try:   # živé hodnoty (COP teraz, výkon, spotreba); bez modulu štatistík alebo pri chybe None
        out["live"] = stats.live(cfg) if stats is not None and hasattr(stats, "live") else None
    except Exception:
        out["live"] = None
    try:
        out["tunnel"] = tunnel_status(cfg)
    except Exception:
        out["tunnel"] = tunnel_status()
    return out


# kľúče izieb pre rolu user: vonku, izby z profilu a „_z“ (zigbee) variant referenčnej izby; len tie, ktoré majú entitu v ESP (S)
ROOM_KEYS = tuple(k for k in ("vonku",) + tuple(ROOMS) + (REF_ROOM + "_z",) if k in S)


def status_user(cfg, role, email, lang="sk"):
    """Filtrovaný stav pre rolu `user`: teploty, sezóna, otvorené okná, krátky text posledného behu. Žiadne kredity, kľúče, HA, tunel."""
    now = int(time.time())
    c = adb(cfg)
    try:
        last = c.execute("SELECT summary FROM runs WHERE status IN ('ok','rutina') AND coalesce(summary,'')!='' ORDER BY ts DESC LIMIT 1").fetchone()
        out = {"now": now, "mode": get_state(c, "mode", "advise"), "role": role, "email": email,
               "last": (last[0] or "")[:300] if last else ""}
        try:
            out["season"] = contacts.season(c)
            out["windows_open"] = [{"label": o["label"], "group": o["group"], "minutes": o["minutes"], "room": o["room"]}
                                   for o in contacts.open_now(c, now)]
        except Exception:
            out["season"], out["windows_open"] = "zima", []
        try:
            out["tuv"] = dhw.manual_status(c)
        except Exception:
            out["tuv"] = {"active": False, "until": None, "target": None, "base": None, "last": None}
    finally:
        c.close()
    out["profil_info"] = profil_info(cfg)
    out["prevadzka"] = prevadzka(cfg, now)
    out["current"] = {}
    try:
        meta = dict(tcq(cfg, "SELECT k, v FROM meta"))
        ls = int(meta.get("last_sample") or 0)
        names = [S[k] for k in ROOM_KEYS] + [x for x in (S.get("ciel"), ROOM_TARGET_ENT, S.get("bojler"), TUV_CIEL_ENT) if x]
        rows = tcq(cfg, "SELECT entity, value FROM samples WHERE ts=? AND entity IN (%s)" % ",".join("?" * len(names)), (ls, *names))
        out["current"] = live_overlay(meta, {e: v for e, v in rows}, names, ls, now)
    except Exception as e:
        out["collector_error"] = tr("srv.app.teploty_zberac", lang)
    return out


# --- Cloudflare tunel (cloudflared ako systemd služba kurenie-tunnel, token v súbore s právami 600) ---
CF_TOKEN_FILE = os.path.join(ROOT, "cf_token")
CF_API_FILE = os.path.join(ROOT, "cf_api")   # voliteľne uložený Cloudflare API token (správa e-mailov v Access); nikdy sa nevracia v API
CF_UNIT = "kurenie-tunnel"


def _systemctl(*args):
    import subprocess
    try:
        r = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=30)
        return r.returncode, (r.stdout or "").strip()
    except Exception as e:
        return 1, str(e)


def write_secret(path, text):
    """Atomicky zapíše tajomstvo do súboru s právami 600."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text + "\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def read_api_token():
    try:
        with open(CF_API_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def user_emails(c):
    return [r[0] for r in c.execute("SELECT email FROM users ORDER BY added_ts, email")]


def tunnel_status(cfg=None):
    import shutil
    configured = os.path.exists(CF_TOKEN_FILE)
    st = _systemctl("is-active", CF_UNIT)[1] if configured else "inactive"
    out = {"installed": bool(shutil.which("cloudflared")), "configured": configured, "active": st == "active", "state": st,
           "api_key_saved": os.path.exists(CF_API_FILE), "host": "", "access": False, "emails": []}
    if cfg is not None:
        c = adb(cfg)
        try:
            out["host"] = get_state(c, "tunnel_host", "") or ""
            out["access"] = bool(get_state(c, "cf_access_app_id", ""))
            out["emails"] = user_emails(c)
            td, aud = access_settings(c)
            out["access_verified"] = bool(td and aud)
            out["team_domain"] = td
            out["aud_set"] = bool(aud)
        finally:
            c.close()
    return out


def access_settings(c):
    """(team_domain, aud) uložené v state; prázdny reťazec = nenastavené. Neplatné hodnoty sa považujú za nenastavené (fail-closed)."""
    td = (get_state(c, "cf_team_domain", "") or "").strip().lower()
    aud = (get_state(c, "cf_aud", "") or "").strip()
    return (td if re.fullmatch(cfjwt.TEAM_RE, td) else ""), (aud if re.fullmatch(cfjwt.AUD_RE, aud) else "")


def access_config(cfg, body, lang="sk"):
    """POST /api/access_config {team_domain, aud} (len správca): ručné nastavenie overovania Cloudflare Access; prázdne = zmazať."""
    td = body.get("team_domain")
    aud = body.get("aud")
    if td is None:
        td = ""
    if aud is None:
        aud = ""
    if not isinstance(td, str) or not isinstance(aud, str):
        return 400, {"error": tr("srv.app.neplatne_hodnoty", lang)}
    td = td.strip().lower()
    aud = aud.strip()
    td = re.sub(r"^https?://", "", td).rstrip("/")
    if td and not re.fullmatch(cfjwt.TEAM_RE, td):
        return 400, {"error": tr("srv.app.team_domain", lang)}
    if aud and not re.fullmatch(cfjwt.AUD_RE, aud):
        return 400, {"error": tr("srv.app.aud_tag", lang)}
    if bool(td) != bool(aud):
        return 400, {"error": tr("srv.app.vyplnit_obe", lang)}
    c = adb(cfg)
    try:
        set_state(c, "cf_team_domain", td)
        set_state(c, "cf_aud", aud)
    finally:
        c.close()
    return 200, {"ok": True, "tunnel": tunnel_status(cfg)}


def tunnel_auto(cfg, body, lang="sk"):
    """POST /api/tunnel_setup {cf_token, hostname, emails, no_access, save_api_token}: nastaví tunel, Access a DNS cez Cloudflare API."""
    if is_dry(cfg):
        return 400, {"error": tr("srv.app.dry_tunel", lang)}
    log = []
    try:
        tk, host, ids = cftunnel.setup(body.get("cf_token"), body.get("hostname"), body.get("emails"), bool(body.get("no_access")), log, lang)
    except cftunnel.CfError as e:
        return 400, {"error": str(e), "steps": log}
    code, out = tunnel_save(cfg, {"token": tk}, lang)
    if code != 200:
        return code, dict(out, steps=log)
    c = adb(cfg)
    try:
        set_state(c, "tunnel_host", host)
        for k in ("account_id", "zone_id", "tunnel_id", "access_app_id"):
            set_state(c, "cf_" + k, ids.get(k) or "")
        if ids.get("aud") and ids.get("team_domain"):   # len ak sa obe podarilo zistiť; inak ostáva doterajšie nastavenie
            set_state(c, "cf_aud", ids["aud"])
            set_state(c, "cf_team_domain", ids["team_domain"])
        # bootstrap používateľov: prvý e-mail = správca, ostatní = používatelia; existujúce záznamy sa neprepisujú
        now = int(time.time())
        for i, e in enumerate(x.strip().lower() for x in re.split(r"[\s,;]+", body.get("emails") or "") if x.strip()):
            c.execute("INSERT OR IGNORE INTO users(email, role, added_ts) VALUES(?,?,?)", (e, "admin" if i == 0 else "user", now))
        c.commit()
    finally:
        c.close()
    if body.get("save_api_token") in (True, 1, "1", "true"):
        write_secret(CF_API_FILE, str(body.get("cf_token")).strip())
        log.append(tr("srv.app.api_token_ulozeny", lang))
    out["tunnel"] = tunnel_status(cfg)
    out["steps"] = log
    out["host"] = host
    return 200, out


def tunnel_api(cfg, body, lang="sk"):
    """POST /api/tunnel_api {cf_token}: uloží/prepíše Cloudflare API token (po overení); prázdny reťazec ho zmaže. Token sa nevracia."""
    if is_dry(cfg):
        return 400, {"error": tr("srv.app.dry_api_kluc", lang)}
    tk = str(body.get("cf_token") or "").strip()
    if not tk:
        try:
            os.remove(CF_API_FILE)
        except FileNotFoundError:
            pass
        return 200, {"ok": True, "tunnel": tunnel_status(cfg)}
    try:
        cftunnel.verify_token(tk, lang)
    except cftunnel.CfError as e:
        return 400, {"error": str(e)}
    write_secret(CF_API_FILE, tk)
    return 200, {"ok": True, "tunnel": tunnel_status(cfg)}


# --- používatelia a synchronizácia Cloudflare Access ---

def sync_result(ok, text, skipped=False):
    d = {"ok": ok, "text": text}
    if skipped:
        d["skipped"] = True
    return d


def users_sync(cfg, c, lang="sk"):
    """Prenesie zoznam e-mailov do Access aplikácie. Nikdy nevyhodí výnimku; výsledok sa uloží do state.users_sync."""
    emails = user_emails(c)
    app_id, acc = get_state(c, "cf_access_app_id", ""), get_state(c, "cf_account_id", "")
    tk = read_api_token()
    if not app_id:
        res = sync_result(False, tr("srv.app.cf_access_vypnuty", lang), True)
    elif not tk:
        res = sync_result(False, tr("srv.app.cf_ulozit_token", lang), True)
    elif not emails:
        res = sync_result(False, tr("srv.app.cf_prazdny_zoznam", lang), True)
    elif is_dry(cfg):
        res = sync_result(False, tr("srv.app.dry_sync", lang), True)
    else:
        try:
            res = sync_result(True, cftunnel.sync_emails(tk, acc, app_id, emails, lang))
        except cftunnel.CfError as e:
            res = sync_result(False, tr("srv.app.cf_aktualizacia_chyba", lang, err=e))
        except Exception as e:
            res = sync_result(False, tr("srv.app.cf_aktualizacia_chyba", lang, err=type(e).__name__))
    set_state(c, "users_sync", json.dumps(dict(res, ts=int(time.time())), ensure_ascii=False))
    return res


def users_list(c):
    return [{"email": r[0], "role": r[1], "added_ts": r[2]} for r in c.execute("SELECT email, role, added_ts FROM users ORDER BY added_ts, email")]


def users_get(cfg, c, lang="sk"):
    try:
        sync = json.loads(get_state(c, "users_sync", "") or "null")
    except ValueError:
        sync = None
    if not sync:
        sync = sync_result(None, tr("srv.app.zatial_nesync", lang))
    return {"users": users_list(c), "sync": sync}


def users_set(cfg, c, body, lang="sk"):
    """POST /api/users {email, role}: pridá používateľa alebo zmení rolu."""
    try:
        em = cftunnel.norm_email(body.get("email"), lang)
    except ValueError as e:
        return 400, {"error": str(e)}
    role = body.get("role")
    if role not in ("admin", "user"):
        return 400, {"error": tr("srv.app.neplatna_rola", lang)}
    cur = c.execute("SELECT role FROM users WHERE email=?", (em,)).fetchone()
    if cur and cur[0] == "admin" and role != "admin" and c.execute("SELECT count(*) FROM users WHERE role='admin'").fetchone()[0] <= 1:
        return 400, {"error": tr("srv.app.posl_spravca_degrad", lang)}
    if cur:
        c.execute("UPDATE users SET role=? WHERE email=?", (role, em))
    else:
        c.execute("INSERT INTO users(email, role, added_ts) VALUES(?,?,?)", (em, role, int(time.time())))
    c.commit()
    return 200, {"ok": True, "users": users_list(c), "sync": users_sync(cfg, c, lang)}


def users_delete(cfg, c, body, lang="sk"):
    """POST /api/users/delete {email}."""
    try:
        em = cftunnel.norm_email(body.get("email"), lang)
    except ValueError as e:
        return 400, {"error": str(e)}
    cur = c.execute("SELECT role FROM users WHERE email=?", (em,)).fetchone()
    if not cur:
        return 404, {"error": tr("srv.app.pouzivatel_neexistuje", lang)}
    if cur[0] == "admin" and c.execute("SELECT count(*) FROM users WHERE role='admin'").fetchone()[0] <= 1:
        return 400, {"error": tr("srv.app.posl_spravca_odstr", lang)}
    c.execute("DELETE FROM users WHERE email=?", (em,))
    c.commit()
    return 200, {"ok": True, "users": users_list(c), "sync": users_sync(cfg, c, lang)}


def room_target_post(cfg, body, who, lang="sk"):
    """POST /api/room_target {value} (iba správca)."""
    try:
        v = parse_room_target(body.get("value"), lang)
    except ValueError as e:
        return 400, {"error": str(e)}
    c = adb(cfg)
    try:
        res = set_room_target(cfg, c, v, who, lang)
    except RuntimeError as e:
        return 502, {"error": str(e)}
    finally:
        c.close()
    out = {"ok": True, "value": res["new"]}
    if res["dry"]:
        out["note"] = tr("srv.app.dry_hodnota", lang)
    return 200, out


def user_target_post(cfg, body, email, lang="sk"):
    """POST /api/user_target {value} (používateľ aj správca): zmena cieľa s mantinelmi chatu (20–24,5 po 0,5, ±1,0 naraz,
    raz za 30 min, nie počas A/B testu; zdieľa limit s chatom). Vracia {"ok","text","target"}."""
    c = adb(cfg)
    try:
        ok, text = _agent_mod.chat_room_target(cfg, c, body.get("value"), email or tr("srv.agent.user_fallback", lang), int(time.time()), lang)
    except Exception:
        ok, text = False, tr("srv.app.zmena_teploty_chyba", lang)
    finally:
        c.close()
    try:
        target = _agent_mod.room_target_current(cfg)
    except Exception:
        target = None
    return 200, {"ok": bool(ok), "text": str(text), "target": target}


TUV_CIEL_ENT = profil.device_entity("tuv_ciel") or "Cieľová teplota TÚV"
SERIES_KEYS = tuple(k for k in ROOMS if k in S) + (("bojler",) if "bojler" in S else ())
SERIES_TARGET = {REF_ROOM: S.get("ciel"), "bojler": TUV_CIEL_ENT}
SERIES_TARGET = {k: v for k, v in SERIES_TARGET.items() if v}


def _series_rows(cfg, entity, since, bin_s):
    """Priemery po košoch [(ts, hodnota)], zoradené, bez None a bez hodnôt 0xFFFF (abs > 6000); pri chybe DB prázdne."""
    try:
        rows = tcq(cfg, "SELECT (ts/?)*? AS b, avg(value) FROM samples WHERE entity=? AND ts>=? AND value IS NOT NULL GROUP BY b ORDER BY b",
                   (bin_s, bin_s, entity, since))
    except Exception:
        return []
    return [[int(b), round(float(v), 2)] for b, v in rows if v is not None and abs(v) <= 6000]


def series_get(cfg, p, lang="sk"):
    """GET /api/series?k=&days= -> graf izby/bojlera (priemery v koši, cieľ, štatistika)."""
    k = (p.get("k", [""])[0] or "").strip()
    if k not in SERIES_KEYS:
        return 400, {"error": tr("srv.app.neznamy_kluc_serie", lang)}
    try:
        days = int(float(p.get("days", ["3"])[0]))
    except (TypeError, ValueError):
        days = 3
    days = max(1, min(14, days))
    now = int(time.time())
    bin_s = max(300, (days * 86400 // 300) // 300 * 300)
    since = now - days * 86400
    points = _series_rows(cfg, S[k], since, bin_s)
    target = None
    if k in SERIES_TARGET:
        target = _series_rows(cfg, SERIES_TARGET[k], since, bin_s)
    vals = [v for _, v in points]
    stats_ = {"min": min(vals), "max": max(vals), "avg": round(sum(vals) / len(vals), 2), "now": vals[-1]} if vals else None
    return 200, {"k": k, "days": days, "from": since, "to": now, "bin_s": bin_s, "points": points, "target": target, "stats": stats_}


def tuv_boost_post(cfg, body, who, lang="sk"):
    """POST /api/tuv_boost {action:"start"|"stop", target} (správca aj používateľ). Vracia {"ok","text","tuv"}."""
    action = body.get("action")
    if action not in ("start", "stop"):
        return 400, {"error": tr("srv.app.neplatna_akcia", lang)}
    c = adb(cfg)
    try:
        try:
            ok, text = dhw.manual_start(cfg, c, body.get("target"), who, lang=lang) if action == "start" else dhw.manual_stop(cfg, c, who, lang)
        except Exception as e:
            ok, text = False, tr("srv.app.nastala_chyba", lang, err=e)
        return 200, {"ok": bool(ok), "text": str(text), "tuv": dhw.manual_status(c)}
    finally:
        c.close()


# --- Riadiaca izba: select „Riadiaca izba“ v ESP určuje čidlo, podľa ktorého beží izbová regulácia; profil (referenčná izba)
# sa prispôsobí. Možnosť selectu sa mapuje na izbu podľa názvu: „<názov izby>“ alebo „<názov izby> (<čidlo>)“.
RIADIACA_ENT = _agent_mod.RIADIACA_ENT
_riadiaca_cache = {"t": 0.0, "ttl": 0, "v": None}


def riadiaca_mapa(options, izby):
    """[{moznost, kluc, cidlo}] pre možnosti selectu, ktoré patria izbe z profilu (najdlhší zhodný názov vyhráva)."""
    out = []
    for o in options:
        best = None
        for r in izby:
            n = r.get("nazov") or ""
            if n and (o == n or o.startswith(n + " (")) and (best is None or len(n) > len(best["nazov"])):
                best = r
        if best:
            out.append({"moznost": o, "kluc": best["kluc"], "cidlo": o[len(best["nazov"]):].strip().strip("()").strip()})
    return out


def riadiaca_info(cfg, force=False):
    """Stav selectu „Riadiaca izba“ z ESP ({stav, moznosti}) alebo None (ESP ho nemá / neodpovedá). Výsledok sa drží 30 s (chyba 5 min)."""
    now = time.time()
    if not force and now - _riadiaca_cache["t"] < _riadiaca_cache["ttl"]:
        return _riadiaca_cache["v"]
    v = None
    try:
        url = "%s/select/%s?detail=all" % (cfg["ESP_URL"], urllib.parse.quote(RIADIACA_ENT, safe=""))
        with urllib.request.urlopen(url, timeout=3) as r:
            d = json.loads(r.read().decode("utf-8"))
        opts = [x for x in (d.get("option") or []) if isinstance(x, str)]
        if opts:
            stav = d.get("value") if isinstance(d.get("value"), str) else d.get("state")
            v = {"stav": stav if isinstance(stav, str) else None, "moznosti": riadiaca_mapa(opts, profil.load()["izby"])}
    except Exception:
        v = None
    _riadiaca_cache.update(t=now, ttl=30 if v else 300, v=v)
    return v


def riadiaca_post(cfg, body, who, lang="sk"):
    """POST /api/riadiaca {moznost} (iba správca): prepne čidlo izbovej regulácie v ESP; ak patrí inej izbe, zmení referenčnú
    izbu v profile a reštartuje služby. Vracia {ok, moznost, kluc, restart[, note]}."""
    opt = str(body.get("moznost") or "").strip()
    info = riadiaca_info(cfg, force=True)
    if not info:
        return 502, {"error": tr("srv.app.riadiaca_bez_esp", lang)}
    m = next((x for x in info["moznosti"] if x["moznost"] == opt), None)
    if not m:
        return 400, {"error": tr("srv.app.riadiaca_neplatna", lang)}
    c = adb(cfg)
    try:
        if abtest.active(c):
            return 409, {"error": tr("srv.app.riadiaca_abtest", lang)}
        p = profil.load()
        old_ref = profil.reference_room(p)
        names = {r["kluc"]: r["nazov"] for r in p["izby"]}
        if is_dry(cfg):
            return 200, {"ok": True, "moznost": opt, "kluc": m["kluc"], "restart": False, "note": tr("srv.app.dry_hodnota", lang)}
        try:
            _agent_mod.esp_set_select(cfg, RIADIACA_ENT, opt)
            time.sleep(2)
            back = riadiaca_info(cfg, force=True)
        except Exception as e:
            return 502, {"error": tr("srv.app.riadiaca_zapis", lang, err=e)}
        if not back or back.get("stav") != opt:
            return 502, {"error": tr("srv.app.riadiaca_nepotvrdene", lang)}
        restart = m["kluc"] != old_ref
        if restart:
            for r in p["izby"]:
                if r["kluc"] == m["kluc"]:
                    r["referencna"], r["min"] = True, None
                elif r.get("referencna"):
                    r["referencna"] = False
                    if r.get("min") is None:
                        r["min"] = 21.0
            try:
                errs = profil.save(p, lang=lang)
            except OSError as e:
                errs = [str(e)]
            if errs:
                return 500, {"error": tr("srv.app.riadiaca_profil", lang, err="; ".join(errs))}
        hl = i18n.home_lang(cfg)
        c.execute("INSERT INTO runs(ts,mode,status,stav,summary,actions) VALUES(?,?,?,?,?,?)",
                  (int(time.time()), get_state(c, "mode", "advise"), "info", None,
                   tr("srv.app.riadiaca_log", hl, who=who, old=names.get(old_ref, old_ref), new=opt), "[]"))
        c.commit()
    finally:
        c.close()
    out = {"ok": True, "moznost": opt, "kluc": m["kluc"], "restart": restart}
    if restart:
        rs = restart_services(cfg, lang)
        if not rs.get("ok"):
            out["restart"] = False
            out["note"] = tr("srv.app.riadiaca_restart_rucne", lang, dovod=rs.get("dovod") or "")
    return 200, out


def tuv_target_post(cfg, body, who, lang="sk"):
    """POST /api/tuv_target {value} (iba správca): trvalý cieľ TÚV 40-52."""
    if "value" not in body:
        return 400, {"error": tr("srv.app.chyba_hodnota", lang)}
    c = adb(cfg)
    try:
        try:
            ok, text = dhw.set_target_manual(cfg, c, body.get("value"), who, lang)
        except Exception as e:
            ok, text = False, tr("srv.app.nastala_chyba", lang, err=e)
        return 200, {"ok": bool(ok), "text": str(text), "tuv": dhw.manual_status(c)}
    finally:
        c.close()


def tunnel_save(cfg, body, lang="sk"):
    """POST /api/tunnel {token} – uloží token a spustí tunel; prázdny token tunel vypne a zmaže."""
    tk = str(body.get("token") or "").strip()
    if is_dry(cfg):
        return 400, {"error": tr("srv.app.dry_tunel", lang)}
    if not tk:
        _systemctl("disable", "--now", CF_UNIT)
        try:
            os.remove(CF_TOKEN_FILE)
        except FileNotFoundError:
            pass
        return 200, {"ok": True, "tunnel": tunnel_status(cfg)}
    if tk.lower().startswith("cloudflared") or " " in tk:
        tk = tk.split()[-1]   # používateľ vložil celý príkaz „cloudflared service install <token>“
    if not re.fullmatch(r"[A-Za-z0-9+/=_\-\.]{40,2048}", tk):
        return 400, {"error": tr("srv.app.token_tunela", lang)}
    if not tunnel_status()["installed"]:
        return 500, {"error": tr("srv.app.cloudflared_chyba", lang)}
    try:
        same = open(CF_TOKEN_FILE, encoding="utf-8").read().strip() == tk
    except OSError:
        same = False
    write_secret(CF_TOKEN_FILE, tk)
    _systemctl("enable", CF_UNIT)
    if not (same and _systemctl("is-active", CF_UNIT)[1] == "active"):
        # reštart tunela až po odoslaní odpovede: ak používateľ nastavuje cez samotný tunel, pripojenie by sa inak prerušilo (502)
        threading.Timer(4.0, lambda: _systemctl("restart", CF_UNIT)).start()
    return 200, {"ok": True, "tunnel": tunnel_status(cfg)}


def day(cfg, dstr, lang="sk"):
    a, b = day_bounds(dstr)
    hours = hour_stats(cfg, a, b)
    ev = tcq(cfg, "SELECT ts, kind, entity, old, new, text FROM events WHERE ts>=? AND ts<? ORDER BY ts LIMIT 5000", (a, b))
    notes = tcq(cfg, "SELECT ts, author, title, body FROM notes WHERE ts>=? AND ts<? ORDER BY ts", (a, b))
    c = adb(cfg)
    runs = [run_row(r) for r in c.execute("SELECT %s FROM runs WHERE ts>=? AND ts<? ORDER BY ts" % RUN_COLS, (a, b))]
    enrich_actions(c, runs)
    les = c.execute("SELECT summary FROM lessons WHERE status='ok' AND date=? ORDER BY ts DESC LIMIT 1",
                    (time.strftime("%Y-%m-%d", time.localtime(a)),)).fetchone()
    ivs = interventions.list_items(c, 200, a, b)
    okna = []
    try:
        day_temps = [dict(x, group=contacts.group_label(x["group"], lang)) for x in temps.day_temps(c, a, b)]
    except Exception:
        day_temps = []
    try:
        for h, x in zip(hours, contacts.hourly_open(c, a, b)):
            h["okna_min"], h["okna_izby"] = x["total"], x["by"]
        okna = [dict(x, group=contacts.group_label(x["group"], lang)) for x in contacts.window_log(c, a, b)]
        okna_day = contacts.window_minutes(c, a, b)
    except Exception:
        okna_day = None
    c.close()
    summ = day_summary(cfg, a, b, hours)
    summ["okna_min"] = None if okna_day is None else round(okna_day)
    summ.update(chyby=len([e for e in ev if e[1] == "chyba"]),
                zmeny_agenta=sum(1 for r in runs for x in r["actions"] if x.get("vykonane")))
    return {"date": time.strftime("%Y-%m-%d", time.localtime(a)), "a": a, "b": b, "hours": hours, "summary": summ,
            "day_review": les[0] if les else None, "messages": messages(cfg, a=a, b=b), "okna": okna, "temps": day_temps,
            "events": [dict(zip(("ts", "kind", "entity", "old", "new", "text"), (e[0], e[1], i18n.event_entity(e[2], lang), e[3], e[4], i18n.event_text(e[5], lang) if e[5] else e[5]))) for e in ev],
            "notes": [dict(zip(("ts", "author", "title", "body"), n)) for n in notes], "runs": runs, "interventions": ivs}


def history(cfg):
    """Denné súhrny (posledných 60 dní) + aktuálne poučenia agenta."""
    c = adb(cfg)
    try:
        update_days(cfg, c)
    except Exception:
        pass
    days = [json.loads(r[0]) for r in c.execute("SELECT data FROM days ORDER BY date DESC LIMIT 60")]
    a, b = day_bounds()
    t = day_summary(cfg, a, b)
    if t["hodin_s_datami"]:
        t["dnes"] = True
        days.insert(0, t)
    r = c.execute("SELECT ts, date, lessons, summary FROM lessons WHERE status='ok' ORDER BY ts DESC LIMIT 1").fetchone()
    err = c.execute("SELECT ts, error FROM lessons WHERE status!='ok' ORDER BY ts DESC LIMIT 1").fetchone()
    n = c.execute("SELECT count(*) FROM lessons WHERE status='ok'").fetchone()[0]
    c.close()
    les = None
    if r:
        try:
            les = {"ts": r[0], "date": r[1], "items": json.loads(r[2] or "[]"), "summary": r[3], "count": n}
        except ValueError:
            les = None
    return {"days": days, "lessons": les, "learn_error": {"ts": err[0], "error": err[1]} if err and (not r or err[0] > r[0]) else None}


_models = {"ts": 0, "data": []}


def models():
    """Lacné textové modely z OpenRouter (verejný zoznam, bez kľúča), cache 1 h."""
    if time.time() - _models["ts"] < 3600 and _models["data"]:
        return _models["data"]
    with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=20) as r:
        d = json.loads(r.read().decode("utf-8"))["data"]
    out = []
    for m in d:
        try:
            pin, pout = float(m["pricing"]["prompt"]) * 1e6, float(m["pricing"]["completion"]) * 1e6
        except (KeyError, TypeError, ValueError):
            continue
        mid = m.get("id", "")
        if pin <= 0 or pin > 1.5 or ":" in mid or "image" in mid or "audio" in mid or (m.get("context_length") or 0) < 32000:
            continue
        out.append({"id": mid, "name": m.get("name") or mid, "in": round(pin, 3), "out": round(pout, 3)})
    out.sort(key=lambda x: x["in"] + x["out"])
    _models.update(ts=time.time(), data=out)
    return out


MODEL_RE = r"[A-Za-z0-9_\-\.:/]{3,120}"


def save_settings(cfg, body, lang="sk"):
    upd, state_upd = {}, {}
    key = body.get("api_key")
    if key is not None:
        key = str(key).strip()
        if key and (len(key) < 20 or len(key) > 300 or not re.fullmatch(r"[A-Za-z0-9_\-\.]+", key)):
            return 400, {"error": tr("srv.app.kluc_openrouter", lang)}
        upd["OPENROUTER_API_KEY"] = key
    model = body.get("model")
    if model is not None:
        model = str(model).strip()
        if not re.fullmatch(MODEL_RE, model) or "/" not in model:
            return 400, {"error": tr("srv.app.nazov_modelu", lang)}
        upd["OPENROUTER_MODEL"] = model
    if body.get("fallback_model") is not None:
        fb = str(body["fallback_model"]).strip()
        if fb and (not re.fullmatch(MODEL_RE, fb) or "/" not in fb):
            return 400, {"error": tr("srv.app.nazov_zalozneho", lang)}
        upd["OPENROUTER_FALLBACK_MODEL"] = fb
    iv = body.get("interval")
    if iv is not None:
        try:
            iv = int(iv)
        except (TypeError, ValueError):
            iv = 0
        if iv not in (15, 20, 30, 60, 120, 180):
            return 400, {"error": tr("srv.app.neplatny_interval", lang)}
        upd["INTERVAL_MIN"] = str(iv)
    if body.get("llm_max_gap_h") is not None:
        try:
            g = int(body["llm_max_gap_h"])
        except (TypeError, ValueError):
            g = 0
        if not 1 <= g <= 12:
            return 400, {"error": tr("srv.app.pauza_llm", lang)}
        upd["LLM_MAX_GAP_H"] = str(g)
    if body.get("notify_url") is not None:
        u = str(body["notify_url"]).strip()
        if u and (len(u) > 500 or not re.fullmatch(r"https?://[^\s]+", u)):
            return 400, {"error": tr("srv.app.adresa_upozorneni", lang)}
        upd["NOTIFY_URL"] = u
    if body.get("ha_url") is not None:
        u = str(body["ha_url"]).strip().rstrip("/")
        if u and (len(u) > 300 or not re.fullmatch(r"https?://[^\s]+", u)):
            return 400, {"error": tr("srv.app.adresa_ha", lang)}
        upd["HA_URL"] = u
    if body.get("ha_token") is not None:
        tk = str(body["ha_token"]).strip()
        if tk and (len(tk) < 20 or len(tk) > 2048 or not re.fullmatch(r"[A-Za-z0-9_\-\.=]+", tk)):
            return 400, {"error": tr("srv.app.token_ha", lang)}
        upd["HA_TOKEN"] = tk
    if body.get("ui_lang") is not None:
        ul = str(body["ui_lang"]).strip().lower()
        if ul not in i18n.known_langs():
            return 400, {"error": tr("srv.app.neplatny_jazyk", lang)}
        upd["UI_LANG"] = ul
    if body.get("dhw_plan") is not None:
        v = body["dhw_plan"]
        if isinstance(v, str):
            v = v.strip().lower() in ("1", "true", "yes", "on")
        state_upd["dhw_plan"] = "1" if v else "0"
    if not upd and not state_upd:
        return 400, {"error": tr("srv.app.nic_na_ulozenie", lang)}
    if upd:
        save_cfg(upd)
    c = adb(cfg)
    for k, v in state_upd.items():
        set_state(c, k, v)   # vypnutie plánovača počas okna: agent (strážca) vráti pôvodný cieľ TÚV do ~1 min
    if "INTERVAL_MIN" in upd:
        set_state(c, "next_run", "0")   # agent si pri najbližšej slučke prepočíta čas ďalšieho behu
    hl = i18n.home_lang(cfg)
    what = ", ".join(x for x in (
        tr("srv.app.nast_kluc", hl) if "OPENROUTER_API_KEY" in upd else "",
        tr("srv.app.nast_model", hl, v=upd["OPENROUTER_MODEL"]) if "OPENROUTER_MODEL" in upd else "",
        tr("srv.app.nast_zalozny", hl, v=upd["OPENROUTER_FALLBACK_MODEL"] or tr("srv.app.nast_vypnuty", hl)) if "OPENROUTER_FALLBACK_MODEL" in upd else "",
        tr("srv.app.nast_interval", hl, v=upd["INTERVAL_MIN"]) if "INTERVAL_MIN" in upd else "",
        tr("srv.app.nast_pauza", hl, v=upd["LLM_MAX_GAP_H"]) if "LLM_MAX_GAP_H" in upd else "",
        tr("srv.app.nast_adresa_upoz", hl, v=tr("srv.app.nast_nastavena" if upd["NOTIFY_URL"] else "srv.app.nast_zmazana", hl)) if "NOTIFY_URL" in upd else "",
        tr("srv.app.nast_adresa_ha", hl, v=tr("srv.app.nast_nastavena" if upd["HA_URL"] else "srv.app.nast_zmazana", hl)) if "HA_URL" in upd else "",
        tr("srv.app.nast_token_ha", hl, v=tr("srv.app.nast_nastaveny" if upd["HA_TOKEN"] else "srv.app.nast_zmazany", hl)) if "HA_TOKEN" in upd else "",
        tr("srv.app.nast_plan", hl, v=tr("srv.app.nast_zapnuty" if state_upd["dhw_plan"] == "1" else "srv.app.nast_vypnuty", hl)) if "dhw_plan" in state_upd else "",
        tr("srv.app.nast_jazyk", hl, v=upd["UI_LANG"]) if "UI_LANG" in upd else "") if x)
    c.execute("INSERT INTO runs(ts,mode,status,stav,summary,actions) VALUES(?,?,?,?,?,?)",
              (int(time.time()), get_state(c, "mode", "advise"), "info", None, tr("srv.app.nast_log", hl, v=what), "[]"))
    c.commit(); c.close()
    return 200, {"ok": True}


MQTT_HOST_RE = r"[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?"
MQTT_NAME_RE = r"[A-Za-z0-9._@\-]+"


def mqtt_validate(body, cfg, lang="sk"):
    """Spoločná kontrola polí MQTT. -> (chyba, hodnoty). Prázdne heslo = uložené heslo z configu (hodnota sa nikdy nevracia klientovi)."""
    host = str(body.get("host") or "").strip()
    if not host or len(host) > 253 or not re.fullmatch(MQTT_HOST_RE, host):
        return tr("srv.app.mqtt_adresa", lang), None
    raw = body.get("port")
    try:
        port = int(str(raw).strip()) if raw not in (None, "") else 1883
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        return tr("srv.app.mqtt_port", lang), None
    user = str(body.get("user") or "").strip()
    if user and (len(user) > 64 or not re.fullmatch(MQTT_NAME_RE, user)):
        return tr("srv.app.mqtt_user", lang), None
    base = str(body.get("base") or "").strip() or "zigbee2mqtt"
    if len(base) > 100 or base.startswith("/") or base.endswith("/") or not re.fullmatch(MQTT_NAME_RE + r"(/" + MQTT_NAME_RE + ")*", base):
        return tr("srv.app.mqtt_base", lang), None
    pw = body.get("pass")
    if pw is None or pw == "":
        pw = cfg.get("MQTT_PASS", "")
        new_pw = False
    else:
        pw = str(pw)
        new_pw = True
        if len(pw) > 128 or any(ord(ch) < 32 or ord(ch) == 127 for ch in pw) or pw != pw.strip() or pw[0] in "\"'" or pw[-1] in "\"'":
            return tr("srv.app.mqtt_heslo", lang), None
    return None, {"host": host, "port": port, "user": user, "base": base, "pass": pw, "new_pass": new_pw}


def mqtt_get(cfg, c):
    def num(k):
        try:
            return int(float(get_state(c, k, "") or 0)) or None
        except ValueError:
            return None
    try:
        ids = json.loads(get_state(c, "mqtt_matched", "[]") or "[]")
    except ValueError:
        ids = []
    names = {eid: (alias or ha or eid) for eid, alias, ha in
             c.execute("SELECT entity_id, alias, ha_name FROM contacts WHERE entity_id LIKE 'binary_sensor.%\\_contact' ESCAPE '\\'")}
    matched = [{"entity_id": e, "label": names[e]} for e in ids if isinstance(e, str) and e in names]
    try:
        port = int(cfg.get("MQTT_PORT") or 1883)
    except ValueError:
        port = 1883
    return {"host": cfg.get("MQTT_HOST", ""), "port": port, "user": cfg.get("MQTT_USER", ""), "pass_set": bool(cfg.get("MQTT_PASS")),
            "base": cfg.get("MQTT_BASE") or "zigbee2mqtt",
            "status": {"connected": get_state(c, "mqtt_connected", "0") == "1", "last_msg": num("mqtt_last_msg"),
                       "last_contact": num("mqtt_last_contact"), "error": get_state(c, "mqtt_error", "") or "", "matched": matched}}


def mqtt_post(cfg, c, body, lang="sk"):
    err, v = mqtt_validate(body, cfg, lang)
    if err:
        return 400, {"error": err}
    upd = {"MQTT_HOST": v["host"], "MQTT_PORT": str(v["port"]), "MQTT_USER": v["user"], "MQTT_BASE": v["base"]}
    if v["new_pass"]:
        upd["MQTT_PASS"] = v["pass"]
    save_cfg(upd)
    set_state(c, "mqtt_reload", repr(time.time()))   # agent (iný proces) sa do ~5 s odpojí a pripojí s novou konfiguráciou
    return 200, {"ok": True}


def ims_import(cfg, body, token):
    """Jednorazový príjem hodinovej spotreby z portálu SSD IMS (prehliadač -> server). Povolené len s dočasným tokenom
    z state `ims_import_token` (nastavuje a maže správca cez SSH). body = {"hours": {"<unix začiatok hodiny>": kWh, ...}}.
    Ukladá sa do ims_hourly.json vedľa aplikácie (zlúčenie s predošlými dávkami)."""
    c = adb(cfg)
    try:
        want = get_state(c, "ims_import_token") or ""
    finally:
        c.close()
    if len(want) < 20 or not hmac.compare_digest(want, str(token)):
        return 403, {"error": "forbidden"}
    hours = body.get("hours")
    if not isinstance(hours, dict) or not 0 < len(hours) <= 2000:
        return 400, {"error": "hours"}
    clean = {}
    for k, v in hours.items():
        try:
            ts = int(k)
            v = float(v)
        except (TypeError, ValueError):
            return 400, {"error": "hodnota"}
        if ts % 3600 or not 1.4e9 < ts < 2.2e9 or not 0 <= v <= 60:
            return 400, {"error": "rozsah"}
        clean[str(ts)] = round(v, 4)
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ims_hourly.json")
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data.update(clean)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, p)
    return 200, {"ok": True, "spolu_hodin": len(data)}


def mqtt_test(cfg, body, lang="sk"):
    """Skúšobné pripojenie priamo z webu: CONNECT, SUBSCRIBE `base/+`, 3 s počúva. Konfiguráciu nemení, heslo sa do odpovede nedostane."""
    err, v = mqtt_validate(body, cfg, lang)
    if err:
        return {"ok": False, "text": err}
    sock = None
    try:
        sock = socket.create_connection((v["host"], v["port"]), timeout=5)
        sock.settimeout(5)
        sock.sendall(contacts_mqtt.build_connect("kurenie-test-%d" % (int(time.time()) % 100000), v["user"], v["pass"]))
        t, _, rb = contacts_mqtt.read_packet(sock)
        if t != 2 or len(rb) < 2:
            return {"ok": False, "text": tr("srv.app.mqtt_nie_broker", lang)}
        if rb[1] != 0:
            why = {1: tr("srv.app.mqtt_verzia", lang), 2: tr("srv.app.mqtt_klient_id", lang), 3: tr("srv.app.mqtt_nedostupny_docasne", lang),
                   4: tr("srv.app.mqtt_heslo_zle", lang), 5: tr("srv.app.mqtt_zamietnute", lang)}
            return {"ok": False, "text": why.get(rb[1], tr("srv.app.mqtt_odmietol", lang, code=rb[1]))}
        sock.sendall(contacts_mqtt.build_subscribe(1, v["base"] + "/+"))
        names, end = set(), time.time() + 3
        while True:
            left = end - time.time()
            if left <= 0:
                break
            sock.settimeout(left)
            try:
                t, flags, pb = contacts_mqtt.read_packet(sock)
            except socket.timeout:
                break
            if t == 3:
                topic = contacts_mqtt.parse_publish(flags, pb)[0]
                if topic.startswith(v["base"] + "/"):
                    names.add(topic[len(v["base"]) + 1:])
        if names:
            n = len(names)
            return {"ok": True, "text": tn("srv.app.mqtt_zachytene", n, lang)}
        return {"ok": True, "text": tr("srv.app.mqtt_ziadna_sprava", lang)}
    except socket.gaierror:
        return {"ok": False, "text": tr("srv.app.mqtt_adresa_nenajdena", lang)}
    except ConnectionRefusedError:
        return {"ok": False, "text": tr("srv.app.mqtt_odmieta", lang)}
    except (socket.timeout, TimeoutError):
        return {"ok": False, "text": tr("srv.app.mqtt_timeout", lang)}
    except ConnectionError:
        return {"ok": False, "text": tr("srv.app.mqtt_zatvoril", lang)}
    except (OSError, ValueError):
        return {"ok": False, "text": tr("srv.app.mqtt_nedostupny", lang)}
    finally:
        if sock:
            sock.close()


def contacts_get(cfg, c, lang="sk"):
    now = int(time.time())
    gs, ungrouped = contacts.tree(c, now)
    gs = [dict(g, name=contacts.group_label(g["name"], lang)) for g in gs]    # predvolené skupiny „Ostatné“ / „Chodba“ podľa jazyka
    return {"season": contacts.season(c), "notify_min": contacts.notify_min(c), "repeat_min": contacts.repeat_min(c),
            "ha": contacts.ha_status(cfg, c, lang), "other_group_id": contacts.other_id(c), "groups": gs, "ungrouped": ungrouped}


def contacts_post(cfg, c, what, body, lang="sk"):
    """POST /api/contacts/{settings|sensor|group|group_delete}. Neplatný vstup = 400 s textom po slovensky."""
    try:
        if what == "settings":
            old = contacts.season(c)
            txt = contacts.save_settings(c, body, lang)
            if contacts.season(c) != old:
                c.execute("INSERT INTO runs(ts,mode,status,stav,summary,actions) VALUES(?,?,?,?,?,?)",
                          (int(time.time()), get_state(c, "mode", "advise"), "info", None, tr("srv.app.sezona_log", i18n.home_lang(cfg), old=old, new=contacts.season(c)), "[]"))
                c.commit()
            return 200, {"ok": True}
        if what == "sensor":
            eid = body.get("entity_id")
            if not isinstance(eid, str) or not eid:
                return 400, {"error": tr("srv.app.chyba_senzor", lang)}
            kw = {k: body[k] for k in ("alias", "group_id", "enabled", "note") if k in body}
            contacts.update_contact(c, eid, lang=lang, **kw)
            return 200, {"ok": True}
        if what == "group":
            gid = body.get("id")
            if gid is not None and (isinstance(gid, bool) or not isinstance(gid, int)):
                return 400, {"error": tr("srv.app.neplatne_id_skupiny", lang)}
            nid = contacts.group_save(c, body.get("name"), body.get("room"), body.get("notify_min"), gid, lang)
            return 200, {"ok": True, "id": nid}
        if what == "group_delete":
            gid = body.get("id")
            if isinstance(gid, bool) or not isinstance(gid, int):
                return 400, {"error": tr("srv.app.neplatne_id_skupiny", lang)}
            contacts.group_delete(c, gid, lang)
            temps.move_group(c, gid, contacts.other_id(c))   # aj teplotné čidlá z tejto skupiny idú do „Ostatné“
            return 200, {"ok": True}
    except ValueError as e:
        return 400, {"error": str(e)}
    return 404, {"error": "not found"}


def temps_get(cfg, c, lang="sk"):
    return temps.api_state(cfg, c, lang=lang)


def temps_post(cfg, c, what, body, lang="sk"):
    """POST /api/temps/{sensor|settings}. Neplatný vstup = 400 s textom po slovensky."""
    try:
        if what == "settings":
            temps.save_settings(c, body, lang)
            return 200, {"ok": True}
        if what == "sensor":
            eid = body.get("entity_id")
            if not isinstance(eid, str) or not eid:
                return 400, {"error": tr("srv.app.chyba_cidlo", lang)}
            kw = {k: body[k] for k in ("alias", "group_id", "enabled", "kind", "note", "alert_min", "alert_max") if k in body}
            temps.update_sensor(c, eid, lang=lang, **kw)
            return 200, {"ok": True}
    except ValueError as e:
        return 400, {"error": str(e)}
    return 404, {"error": "not found"}


def targets_get(cfg):
    c = adb(cfg)
    t = get_targets(c)
    c.close()
    return {"rooms": [{"key": k, "name": ROOM_NAMES[k], "min": t[k]["min"]} for k in TARGET_ROOMS]}


def targets_set(cfg, body, lang="sk"):
    inp = body.get("targets")
    if not isinstance(inp, dict) or not inp:
        return 400, {"error": tr("srv.app.zoznam_teplot", lang)}
    c = adb(cfg)
    try:
        cur = get_targets(c)
        for k, v in inp.items():
            if k not in TARGET_ROOMS:
                return 400, {"error": tr("srv.app.neznama_izba", lang, k=k)}
            try:
                x = float((v or {}).get("min") if isinstance(v, dict) else v)
            except (TypeError, ValueError):
                return 400, {"error": tr("srv.app.neplatna_teplota", lang, room=ROOM_NAMES[k])}
            if not (15 <= x <= 26) or abs(x * 2 - round(x * 2)) > 1e-9:
                return 400, {"error": tr("srv.app.teplota_rozsah", lang, room=ROOM_NAMES[k])}
            cur[k]["min"] = x
        set_state(c, "targets", json.dumps(cur))
        c.execute("INSERT INTO runs(ts,mode,status,stav,summary,actions) VALUES(?,?,?,?,?,?)",
                  (int(time.time()), get_state(c, "mode", "advise"), "info", None, tr("srv.app.teploty_log", i18n.home_lang(cfg)), "[]"))
        c.commit()
    finally:
        c.close()
    return 200, {"ok": True}


def parse_ts(v):
    """Čas zásahu z tela požiadavky: unix sekundy (aj milisekundy) alebo 'YYYY-MM-DD HH:MM' (miestny čas). None = neplatné/neuvedené."""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = float(v)
        return int(v / 1000) if v > 1e11 else int(v)
    s = str(v).strip()
    if re.fullmatch(r"\d{9,13}", s):
        return parse_ts(int(s))
    for f in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(time.mktime(time.strptime(s[:19] if "S" in f else s[:16], f)))
        except ValueError:
            continue
    raise ValueError("neplatný čas")


def test_key(cfg, lang="sk"):
    if not cfg["OPENROUTER_API_KEY"]:
        return {"ok": False, "error": tr("srv.app.kluc_nenastaveny", lang)}
    try:
        txt, tin, tout, cost = call_llm(cfg, tr("srv.prompt.test_key", i18n.home_lang(cfg)), "test", 2500)
        return {"ok": True, "model": cfg["OPENROUTER_MODEL"], "tokens": (tin or 0) + (tout or 0), "cost": cost, "reply": txt[:80]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:400]}


def run_detail(cfg, rid):
    c = adb(cfg)
    r = c.execute("SELECT input, output FROM runs WHERE id=?", (rid,)).fetchone()
    c.close()
    return {"input": r[0], "output": r[1]} if r else {}


# ---------------------------------------------------------------- prihlásenie (SPEC bod 11)

_fails = {}               # ip -> {"t": [časy zlyhaní], "block": do_kedy}
_fails_lock = threading.Lock()


def hash_password(pw, salt=None, iters=PBKDF2_ITER):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iters)
    return "pbkdf2_sha256$%d$%s$%s" % (iters, salt.hex(), h.hex())


def verify_password(pw, stored):
    try:
        algo, it, salt, h = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(calc.hex(), h)
    except (ValueError, AttributeError):
        return False


def tok_hash(tok):
    return hashlib.sha256(tok.encode("utf-8")).hexdigest()


def rate_blocked(ip):
    """Zostávajúce sekundy blokovania pre IP (0 = voľno)."""
    with _fails_lock:
        f = _fails.get(ip)
        return max(0, int(f["block"] - time.time()) + 1) if f and f.get("block", 0) > time.time() else 0


def rate_fail(ip):
    now = time.time()
    with _fails_lock:
        f = _fails.setdefault(ip, {"t": [], "block": 0})
        f["t"] = [t for t in f["t"] if now - t < 600] + [now]
        if len(f["t"]) >= 5:
            f["block"] = now + 60
            f["t"] = []


def rate_ok(ip):
    with _fails_lock:
        _fails.pop(ip, None)


def create_session(c, role="admin", email=None):
    tok = secrets.token_hex(32)
    now = int(time.time())
    c.execute("DELETE FROM sessions WHERE expires<?", (now,))
    c.execute("INSERT INTO sessions(token_hash, created, expires, role, email) VALUES(?,?,?,?,?)",
              (tok_hash(tok), now, now + SESSION_TTL, role, email))
    c.commit()
    return tok


def session_identity(c, tok):
    """(role, email) platnej relácie alebo None. Staré relácie bez roly = admin."""
    if not tok or not re.fullmatch(r"[0-9a-f]{64}", tok):
        return None
    r = c.execute("SELECT expires, role, email FROM sessions WHERE token_hash=?", (tok_hash(tok),)).fetchone()
    if not (r and r[0] > time.time()):
        return None
    return (r[1] if r[1] in ("admin", "user") else ("admin" if r[1] is None else "user")), r[2]


def session_valid(c, tok):
    return session_identity(c, tok) is not None


LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
USER_GET = ("/api/status", "/api/messages", "/api/push/key", "/api/push/status", "/api/series", "/api/spotreba", "/api/vonku", "/api/akokuri")    # čo smie rola `user` (okrem /api/auth a statických súborov)
USER_POST = ("/api/message", "/api/user_target", "/api/tuv_boost", "/api/logout", "/api/push/subscribe", "/api/push/unsubscribe", "/api/push/test", "/api/push/remove")
NO_PERM = {"error": "Na túto akciu nemáš oprávnenie."}


COOKIE_ATTRS = "HttpOnly; SameSite=Strict; Path=/; Max-Age=%d"


# ---------------------------------------------------------------- štatistiky (SPEC_STATS §7)

# len tieto dva súbory, bez prihlásenia; názov sa nikdy neberie z požiadavky (žiadny path traversal)
STATS_STATIC = {
    "/assets/stats_ui.js": ("stats_ui.js", "application/javascript; charset=utf-8"),
    "/assets/stats_ui.css": ("stats_ui.css", "text/css; charset=utf-8"),
}
# ikony aplikácie (PWA): cesta -> súbor v ROOT/assets; názov sa nikdy neberie z požiadavky
STATIC = {
    "/apple-touch-icon.png": "apple-touch-icon.png",
    "/favicon.png": "favicon.png",
    "/icon-192.png": "icon-192.png",
    "/icon-512.png": "icon-512.png",
    "/icon-maskable-512.png": "icon-maskable-512.png",
}
MANIFEST = {"name": "Samsung Heat Pump AI Control", "short_name": "Heat Pump AI", "start_url": "/", "scope": "/", "display": "standalone",
            "background_color": "#0e1115", "theme_color": "#0e1115", "lang": "sk",
            "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                      {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                      {"src": "/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]}
LOCALE_RE = re.compile(r"^/locales/[a-z]{2}\.json$")   # jazykové súbory webu (SPEC_i18n)
STATS_GROUPS = ("day", "week", "month")
STATS_NA = "Modul štatistík nie je nainštalovaný."
STATS_MAX_SPAN = 3660   # dní; najdlhšie obdobie v jednom dotaze (10 rokov)


def today_s():
    return datetime.date.today().isoformat()


def shift_days(s, n):
    return (datetime.date.fromisoformat(s) + datetime.timedelta(days=n)).isoformat()


def stats_date(v, name, lang="sk"):
    """Dátum RRRR-MM-DD (lokálny deň). Neplatný = ValueError po slovensky."""
    s = str(v or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            return datetime.date.fromisoformat(s).isoformat()
        except ValueError:
            pass
    raise ValueError(tr("srv.app.stats_datum", lang, name=name))


def stats_qdate(p, name, default, lang="sk"):
    v = p.get(name, [""])[0]
    return stats_date(v, name, lang) if v else default


def stats_period(p, lang="sk"):
    """Parametre from / to (predvolene posledných 30 dní do dnes). Obdobie musí byť v poriadku."""
    to = stats_qdate(p, "to", today_s(), lang)
    frm = stats_qdate(p, "from", shift_days(to, -30), lang)
    if frm > to:
        raise ValueError(tr("srv.app.stats_od_do", lang))
    if (datetime.date.fromisoformat(to) - datetime.date.fromisoformat(frm)).days > STATS_MAX_SPAN:
        raise ValueError(tr("srv.app.stats_dlhe", lang))
    return frm, to


def stats_range_args(p, lang="sk"):
    frm, to = stats_period(p, lang)
    g = p.get("group", [""])[0] or "day"
    if g not in STATS_GROUPS:
        raise ValueError(tr("srv.app.stats_skupina", lang))
    return frm, to, g


def stats_compare_dates(p, lang="sk"):
    parts = [x.strip() for x in p.get("dates", [""])[0].split(",") if x.strip()]
    if not 1 <= len(parts) <= 6:
        raise ValueError(tr("srv.app.stats_porovnanie", lang))
    return [stats_date(x, "dates", lang) for x in parts]


def stats_ts(v, lang="sk"):
    """Čas poruchy: unix sekundy (číslo alebo reťazec cifier) alebo RRRR-MM-DD HH:MM (miestny čas). None = neuvedené."""
    if v is None or v == "":
        return None
    if not isinstance(v, bool) and (isinstance(v, (int, float)) or (isinstance(v, str) and re.fullmatch(r"\s*\d{1,11}\s*", v))):
        try:
            ts = int(float(v))
        except (ValueError, OverflowError):
            raise ValueError(tr("srv.app.stats_cas_poruchy", lang))
        if not 946684800 <= ts <= 4102444800:
            raise ValueError(tr("srv.app.stats_cas_rozsah", lang))
        return ts
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})", str(v).strip()) if isinstance(v, str) else None
    if m:
        try:
            return int(time.mktime(time.strptime("%s %s:%s" % m.groups(), "%Y-%m-%d %H:%M")))
        except ValueError:
            pass
    raise ValueError(tr("srv.app.stats_cas_format", lang))


def stats_text(body, key, maxlen, lang="sk"):
    v = body.get(key)
    if v is None:
        return ""
    if not isinstance(v, str) or len(v) > maxlen:
        raise ValueError(tr("srv.app.stats_text", lang, key=key, max=maxlen))
    return v.strip()


def stats_code(v, lang="sk"):
    if not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,30}", v.strip()):
        raise ValueError(tr("srv.app.stats_kod", lang))
    return v.strip().upper()


def hdd_base(cfg):
    return cfg_int(cfg, "STATS_HDD_BASE", 18, 10, 25)


def stats_brief(cfg, lang="sk"):
    """Blok `stats` v /api/status: počet hodín s dátami a posledný deň."""
    if stats is None:
        return {"hours": None, "last_day": None, "error": tr("srv.app.stats_na", lang)}
    try:
        cov = stats.coverage(cfg)
        return {"hours": cov.get("hours"), "last_day": cov.get("last_day")}
    except Exception as e:
        return {"hours": None, "last_day": None, "error": str(e)[:200]}


# --- profil inštalácie (v2): informácie pre UI, sprievodca nastavením zariadenia a domu
_spotreba_cache = {"t": 0.0, "v": False}


def _spotreba_domu(cfg):
    """Existujú dáta o spotrebe celého domu (stats.db kwh_dom alebo ims_hourly.json)? Výsledok sa 60 s pamätá."""
    now = time.time()
    if now - _spotreba_cache["t"] < 60:
        return _spotreba_cache["v"]
    ok = False
    try:
        path = ((cfg.get("STATS_DB") or "").strip() or os.path.join(ROOT, "stats.db"))
        if os.path.isfile(path):
            con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=5)
            try:
                ok = con.execute("SELECT 1 FROM hourly WHERE kwh_dom IS NOT NULL LIMIT 1").fetchone() is not None
            finally:
                con.close()
    except Exception:
        ok = False
    if not ok:
        try:
            import ha_cost
            ok = bool(ha_cost.load_ims(ha_cost.IMS_FILE))
        except Exception:
            ok = False
    _spotreba_cache.update(t=now, v=bool(ok))
    return bool(ok)


def profil_info(cfg):
    """Blok `profil_info` v /api/status (aj pre rolu user, bez citlivých údajov): zariadenie, izby a dostupné funkcie."""
    p = profil.load()
    ref = profil.reference_room(p)
    s_map = profil.esp_s(p)
    tuv_ent = profil.device_entity("tuv", p)
    tuv_on = bool(p["zariadenie"].get("tuv") and tuv_ent)
    try:
        m = profil.model(p)
    except Exception:
        m = None
    mdl = m is not None
    izby = [{"kluc": r["kluc"], "nazov": r["nazov"], "referencna": r["kluc"] == ref, "esp": s_map.get(r["kluc"]) or r["esp"] or ""}
            for r in p["izby"]]
    izby.sort(key=lambda r: not r["referencna"])
    lo, hi = profil.room_target_range(p)
    out = {"zariadenie": p["zariadenie"].get("nazov") or "", "kurenie": p["zariadenie"].get("kurenie"), "izby": izby,
           "ciel_rozsah": [lo, hi],
           "funkcie": {"ha": bool((cfg.get("HA_TOKEN") or "").strip()), "agent_ai": bool((cfg.get("OPENROUTER_API_KEY") or "").strip()),
                       "tuv": tuv_on, "spotreba_domu": _spotreba_domu(cfg), "model_cop": mdl}}
    z = p["zariadenie"]
    out["zariadenie_info"] = {"model": z.get("model") or "", "typ": z.get("typ"), "vykon_kw": z.get("vykon_kw"),
                              "fazy": z.get("fazy"), "chladivo": z.get("chladivo") or "", "kurenie": z.get("kurenie"),
                              "tuv": bool(z.get("tuv")), "model_v_kniznici": mdl, "model_overeny": bool(m and m.get("overene"))}
    if tuv_on:
        out["tuv_entity"] = {"teplota": tuv_ent, "ciel": profil.device_entity("tuv_ciel", p) or TUV_CIEL_ENT}
    return out


def profil_get(lang="sk"):
    """GET /api/profil (admin)."""
    return {"profil": profil.load(), "chyby": profil.errors(lang=lang), "ulozeny": os.path.exists(profil.PROFILE_FILE), "modely": profil.list_models(),
            "typy_kurenia": list(profil.TYPY_KURENIA), "typy_jednotky": list(profil.TYPY_JEDNOTKY),
            "abs_limity": dict({k: list(v) for k, v in profil.ABS_LIMITS.items()}, ciel_izby=list(profil.ABS_ROOM_TARGET)),
            "predvolby": {t: {k: list(v) for k, v in d.items()} for t, d in profil.PRESETS.items()},
            "kluce_zariadenia": list(profil.DEVICE_KEYS), "kluce_nastaveni": list(profil.SETTING_KEYS)}


def profil_post(body, lang="sk"):
    """POST /api/profil {profil}: overí a uloží. Chyby -> 400 {chyby:[…]}."""
    p = body.get("profil")
    if not isinstance(p, dict):
        return 400, {"error": tr("srv.app.profil_chyba", lang), "chyby": [tr("srv.app.profil_telo", lang)]}
    try:
        errs = profil.save(p, lang=lang)
    except OSError as e:
        return 500, {"error": tr("srv.app.profil_zapis", lang, err=e), "chyby": []}
    if errs:
        return 400, {"error": tr("srv.app.profil_chyby", lang, n=len(errs)), "chyby": errs}
    return 200, {"ok": True, "restart_potrebny": True}


def profil_default(lang="sk"):
    """POST /api/profil/predvoleny: zmaže profil.json (návrat na pôvodnú inštaláciu)."""
    try:
        if os.path.exists(profil.PROFILE_FILE):
            os.remove(profil.PROFILE_FILE)
        profil._cache.pop(profil.PROFILE_FILE, None)
    except OSError as e:
        return 500, {"error": tr("srv.app.profil_zmazanie", lang, err=e)}
    return 200, {"ok": True, "restart_potrebny": True}


def _plain(s):
    """Malé písmená bez diakritiky (na porovnávanie názvov entít)."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", str(s or "")) if not unicodedata.combining(ch)).lower()


def slug_key(name):
    """Kľúč izby z názvu: malé písmená bez diakritiky, a-z0-9_, max 21 znakov."""
    k = re.sub(r"[^a-z0-9]+", "_", _plain(name)).strip("_")[:21].strip("_")
    return k if k and k[0].isalpha() else ("i_" + k)[:21] if k else ""


def _http_get_text(url, timeout=4):
    req = urllib.request.Request(url, headers={"User-Agent": "kurenie-web"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(2_000_000).decode("utf-8", "replace")


ESP_ROW_RE = re.compile(r'<tr[^>]*\bid="[^"]*"[^>]*>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>', re.S | re.I)


def esp_entities(cfg):
    """Entity z ESP: [{nazov, hodnota, zdroj}] – z web_server stránky ESP (ak odpovie) a z posledných vzoriek v tc.db (10 min)."""
    out, seen = [], set()
    url = (os.environ.get("TC_ESP") or cfg.get("TC_ESP") or cfg.get("ESP_URL") or "").strip().rstrip("/")
    if url:
        try:
            page = _http_get_text(url + "/")
            for nm, val in ESP_ROW_RE.findall(page):
                nm = _html.unescape(re.sub(r"<[^>]+>", "", nm)).strip()
                val = _html.unescape(re.sub(r"<[^>]+>", "", val)).strip()
                if nm and nm not in seen:
                    seen.add(nm)
                    out.append({"nazov": nm, "hodnota": val, "zdroj": "esp"})
        except Exception:
            pass
    try:
        rows = tcq(cfg, "SELECT entity, value, max(ts) FROM samples WHERE ts>=? GROUP BY entity", (int(time.time()) - 600,))
        for ent, val, _ts in rows:
            if ent and ent not in seen:
                seen.add(ent)
                out.append({"nazov": ent, "hodnota": None if val is None else round(float(val), 2), "zdroj": "tc.db"})
    except Exception:
        pass
    return out


# (kľúč, výraz nad názvom bez diakritiky); prvá zhoda s ešte nepridelenou entitou vyhráva, nastavenia FSV sa hľadajú samostatne
_NAV_DEVICE = (
    ("tuv_ciel", lambda n: "tuv" in n and "cielov" in n),
    ("tuv", lambda n: "tuv" in n or "bojler" in n),
    ("ciel_izby", lambda n: "cielova izbova" in n or ("cielova teplota izby" in n and "aktualna" not in n)),
    ("ciel_akt", lambda n: "aktualna cielova" in n),
    ("sledovana", lambda n: "sledovana" in n),
    ("posun", lambda n: "posun" in n and "ekviterm" in n),
    ("wl", lambda n: ("ekviterm" in n and "voda" in n) or "(wl)" in n),
    ("tw2", lambda n: "tw2" in n or "vystup" in n),
    ("tw1", lambda n: "tw1" in n or "spiatoc" in n),
    ("dt", lambda n: "delta t" in n or n.startswith("dt ")),
    ("freq", lambda n: "frekvenc" in n),
    ("prikon", lambda n: "prikon" in n),
    ("pwm", lambda n: "pwm" in n or "otacky" in n or "obehov" in n),
    ("chyba", lambda n: "chyb" in n),
    ("kwh_den", lambda n: "denna spotreba" in n or ("denn" in n and "spotreb" in n)),
    ("flow", lambda n: "prietok" in n),
    ("ventil", lambda n: "ventil" in n and ("cest" in n or "smer" in n)),
    ("odmrazovanie", lambda n: "odmraz" in n),
    ("termostat", lambda n: "termostat" in n),
    ("vonku", lambda n: "vonkajsia" in n or "vonku" in n),
)


def esp_navrh(names):
    """Automatické priradenie kanonických kľúčov (profil.DEVICE_KEYS, SETTING_KEYS) k názvom entít podľa zhody názvu."""
    entity, nast, used = {}, {}, set()
    plain = [(n, _plain(n)) for n in names]
    for n, pn in plain:   # nastavenia: „FSV 2031 …“ -> fsv2031
        m = re.match(r"\s*fsv\s*(\d{4})", pn)
        if m and ("fsv" + m.group(1)) in profil.SETTING_KEYS and ("fsv" + m.group(1)) not in nast:
            nast["fsv" + m.group(1)] = n
            used.add(n)
    for key, test in (("max_posun_zaporny", lambda p: "maximalny posun" in p),
                      ("max_posun", lambda p: "maximalny kladny posun" in p),
                      ("zosilnenie", lambda p: "zosilnenie" in p)):
        for n, pn in plain:
            if n not in used and test(pn) and not pn.startswith("fsv"):
                nast[key] = n
                used.add(n)
                break
    for key, test in _NAV_DEVICE:
        for n, pn in plain:
            if n not in used and not pn.startswith("fsv") and test(pn):
                entity[key] = n
                used.add(n)
                break
    izby, ks = [], set()
    for n, pn in plain:
        if n not in used and pn.startswith("izba ") and not pn.startswith("izba cielov"):
            nm = re.sub(r"\s*\(.*?\)\s*$", "", n[5:]).strip() or n
            k = slug_key(nm)
            if k and k not in ks:
                ks.add(k)
                izby.append({"kluc": k, "nazov": nm, "esp": n})
    return {"entity": entity, "nastavenia": nast, "izby": izby}


def profil_esp_entity(cfg):
    items = esp_entities(cfg)
    return {"entity": items, "navrh": esp_navrh([i["nazov"] for i in items]),
            "povinne": ["vonku", "tw2", "tw1", "freq", "wl", "posun", "fsv2031", "fsv2032"]}


HA_TEMP_TEMPLATE = ("{% set ns = namespace(o=[]) %}{% for s in states.sensor if s.attributes.device_class == 'temperature' %}"
                    "{% set ns.o = ns.o + [{'entity_id': s.entity_id, 'meno': s.name, 'stav': s.state}] %}{% endfor %}{{ ns.o | tojson }}")


def _ha_temp_sensors(cfg, lang="sk"):
    url, token = contacts.ha_conf(cfg)
    ha_auth.check(url, token, lang=lang)
    req = urllib.request.Request(url + "/api/template", data=json.dumps({"template": HA_TEMP_TEMPLATE}).encode("utf-8"), method="POST",
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read(4_000_000).decode("utf-8", "replace").strip())
    return data if isinstance(data, list) else []


def profil_ha_entity(cfg, lang="sk"):
    """Teplotné senzory z HA pre výber izieb; bez HA_TOKEN prázdny zoznam s dôvodom."""
    url, token = contacts.ha_conf(cfg)
    if not token or not url:
        return {"ok": False, "entity": [], "dovod": tr("srv.app.ha_nenastaveny", lang)}
    try:
        rows = _ha_temp_sensors(cfg, lang)
    except Exception as e:
        return {"ok": False, "entity": [], "dovod": tr("srv.app.ha_neodpoveda", lang, err=type(e).__name__)}
    out = [{"entity_id": r["entity_id"], "meno": str(r.get("meno") or r["entity_id"]), "stav": r.get("stav")}
           for r in rows if isinstance(r, dict) and isinstance(r.get("entity_id"), str) and r["entity_id"].startswith("sensor.")]
    out.sort(key=lambda r: r["meno"].lower())
    return {"ok": True, "entity": out}


RESTART_DELAY = 1.0
RESTART_UNITS = ("kurenie-agent", "kurenie-collector")


def _restart_worker():
    _systemctl("restart", *RESTART_UNITS)
    time.sleep(RESTART_DELAY)
    _systemctl("restart", "kurenie-web")


def restart_services(cfg, lang="sk"):
    """POST /api/restart: asynchrónny reštart agenta a zberača, potom webu (sám seba). V DRY_RUN alebo bez systemctl len vráti dôvod."""
    if is_dry(cfg):
        return {"ok": False, "dovod": tr("srv.app.dry_restart", lang)}
    if not shutil.which("systemctl"):
        return {"ok": False, "dovod": tr("srv.app.systemctl", lang)}
    threading.Thread(target=_restart_worker, name="restart-sluzieb", daemon=True).start()
    return {"ok": True}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8", headers=None, cache="no-store"):
        b = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(b)))
        for k, v in (headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def token(self):
        try:
            ck = SimpleCookie(self.headers.get("Cookie") or "")
            return ck[COOKIE].value if COOKIE in ck else ""
        except Exception:
            return ""

    def access_identity(self, cfg):
        """{"email", "role"} z podpísaného JWT Cloudflare Access (iba z loopbacku a keď je nastavené cf_team_domain + cf_aud),
        ak je e-mail v tabuľke `users`; inak None. Holé hlavičky s e-mailom sa NEPOUŽÍVAJÚ. Chyby sa logujú stručne, bez tokenu."""
        if self.client_address[0] not in LOOPBACK:
            return None
        tok = (self.headers.get("Cf-Access-Jwt-Assertion") or "").strip()
        if not tok:
            try:
                ck = SimpleCookie(self.headers.get("Cookie") or "")
                tok = ck["CF_Authorization"].value.strip() if "CF_Authorization" in ck else ""
            except Exception:
                tok = ""
        if not tok:
            return None
        c = adb(cfg)
        try:
            td, aud = access_settings(c)
            if not (td and aud):
                return None
            try:
                claims = cfjwt.verify(tok, td, aud)
            except cfjwt.JwtError as e:
                print("access: JWT odmietnutý: %s" % e, file=sys.stderr, flush=True)
                return None
            except Exception as e:
                print("access: chyba overenia JWT (%s)" % type(e).__name__, file=sys.stderr, flush=True)
                return None
            r = c.execute("SELECT role FROM users WHERE email=?", (claims["email"],)).fetchone()
            if not r or r[0] not in ("admin", "user"):
                return None
            return {"email": claims["email"], "role": r[0]}
        finally:
            c.close()

    def ha_api_ok(self, cfg):
        c = adb(cfg)
        try:
            return ha_api.check(c, self.headers.get("Authorization"))
        finally:
            c.close()

    def identity(self, cfg):
        """(role, email) alebo None – iba z platnej relácie (cookie). Relácia s e-mailom (Cloudflare Access) platí len kým je e-mail
        v tabuľke `users` a rola správcu len kým ju tabuľka potvrdzuje. Hlavička Cf-Access-Authenticated-User-Email sa nepoužíva."""
        c = adb(cfg)
        try:
            ident = session_identity(c, self.token())
            if ident and ident[1]:
                r = c.execute("SELECT role FROM users WHERE email=?", (ident[1],)).fetchone()
                if not r:
                    return None
                return ("admin" if ident[0] == "admin" and r[0] == "admin" else "user"), ident[1]
            return ident
        finally:
            c.close()

    def logged_in(self, cfg):
        return self.identity(cfg) is not None

    def set_cookie(self, tok):
        sec = "; Secure" if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" else ""   # cez HTTPS tunel
        return [("Set-Cookie", "%s=%s; %s%s" % (COOKIE, tok, COOKIE_ATTRS % (SESSION_TTL if tok else 0), sec))]

    def do_GET(self):
        u = urlparse(self.path)
        p = parse_qs(u.query)
        cfg = load_cfg()
        lang = i18n.req_lang(self.headers)
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(ROOT, "ui.html"), "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/auth":
                ident = self.identity(cfg)
                c = adb(cfg)
                try:
                    acc = None
                    ai = self.access_identity(cfg)
                    if ai:
                        acc = {"email": ai["email"], "role": ai["role"], "choices": ["user", "admin"] if ai["role"] == "admin" else ["user"]}
                    return self.send(200, {"setup_needed": not get_state(c, "auth_hash"), "logged_in": ident is not None,
                                           "role": ident[0] if ident else None, "email": ident[1] if ident else None, "access": acc})
                finally:
                    c.close()
            if u.path in STATS_STATIC:   # statické súbory štatistík: bez prihlásenia, ako ui.html
                return self.stats_static(u.path)
            if LOCALE_RE.match(u.path):   # prekladové súbory webu (locales/xx.json): bez prihlásenia, ako ui.html
                return self.static_locale(u.path)
            if u.path in STATIC or u.path == "/manifest.webmanifest":   # ikony a manifest PWA: bez prihlásenia
                return self.static_icon(u.path)
            if u.path == "/sw.js":   # service worker pre Web Push: bez prihlásenia, scope celý web
                return self.static_sw()
            if u.path == mcp_api.PATH:   # MCP: len POST (JSON odpovede, bez SSE streamu)
                return self.send(405, {"error": "method not allowed"}, headers=[("Allow", "POST")])
            if not u.path.startswith("/api/"):
                return self.send(404, {"error": "not found"})
            if u.path == ha_api.PATH_STATE:   # integrácia Home Assistant: vlastný kľúč (Authorization: Bearer)
                if not self.ha_api_ok(cfg):
                    return self.send(401, {"error": "unauthorized"})
                st = status(cfg, "admin", None, i18n.home_lang(cfg))
                return self.send(200, ha_api.build_state(st, int(time.time()), names=profil.load()["esp"]["entity"]))
            ident = self.identity(cfg)
            if not ident:
                return self.send(401, {"error": "auth"})
            role, email = ident
            if role != "admin" and u.path not in USER_GET:   # centrálna kontrola oprávnení pre rolu `user`
                return self.send(403, {"error": tr("srv.app.no_perm", lang)})
            if u.path == "/api/status":
                return self.send(200, status(cfg, role, email, lang))
            if u.path == "/api/profil":
                return self.send(200, profil_get(lang))
            if u.path == "/api/profil/esp_entity":
                return self.send(200, profil_esp_entity(cfg))
            if u.path == "/api/profil/ha_entity":
                return self.send(200, profil_ha_entity(cfg, lang))
            if u.path == "/api/push/key":
                try:
                    return self.send(200, {"key": notify.vapid_keys(lang)[1]})
                except Exception as e:
                    print("push: kľúč nie je dostupný (%s)" % type(e).__name__, file=sys.stderr, flush=True)
                    return self.send(503, {"error": tr("srv.app.push_nedostupne", lang)})
            if u.path == "/api/series":
                code, out = series_get(cfg, p, lang)
                return self.send(code, out)
            if u.path == "/api/spotreba":   # denná spotreba domu a odhad ceny (spotreba.py); neplatný vstup = 400 s textom
                try:
                    return self.send(200, spotreba.report(cfg, p.get("od", [""])[0] or None, p.get("do", [""])[0] or None, lang=lang))
                except ValueError as e:
                    return self.send(400, {"error": str(e)})
            if u.path == "/api/vonku":   # teploty podľa vonkajšej teploty (vonku.py); neplatný vstup = 400 s textom
                try:
                    return self.send(200, vonku.report(cfg, p.get("od", [""])[0] or None, p.get("do", [""])[0] or None,
                                                       p.get("krok", ["2"])[0] or 2, lang=lang))
                except ValueError as e:
                    return self.send(400, {"error": str(e)})
            if u.path == "/api/akokuri":   # teplota vody podľa vonkajšej teploty z minútových vzoriek (vonku.kurenie); neplatný vstup = 400
                try:
                    return self.send(200, vonku.kurenie(cfg, p.get("od", [""])[0] or None, p.get("do", [""])[0] or None,
                                                        p.get("krok", ["1"])[0] or 1, lang=lang))
                except ValueError as e:
                    return self.send(400, {"error": str(e)})
            if u.path == "/api/push/status":
                c = adb(cfg)
                try:
                    return self.send(200, notify.push_status(cfg, c, role, email, lang))
                finally:
                    c.close()
            if u.path == "/api/users":
                c = adb(cfg)
                try:
                    return self.send(200, users_get(cfg, c, lang))
                finally:
                    c.close()
            if u.path == "/api/day":
                return self.send(200, day(cfg, p.get("d", [""])[0], lang))
            if u.path == "/api/messages":
                return self.send(200, {"messages": messages(cfg)})
            if u.path == "/api/models":
                return self.send(200, {"models": models()})
            if u.path == "/api/history":
                return self.send(200, history(cfg))
            if u.path == "/api/run":
                return self.send(200, run_detail(cfg, int(p.get("id", ["0"])[0])))
            if u.path == "/api/targets":
                return self.send(200, targets_get(cfg))
            if u.path == "/api/mqtt":
                c = adb(cfg)
                try:
                    return self.send(200, mqtt_get(cfg, c))
                finally:
                    c.close()
            if u.path == "/api/contacts":
                c = adb(cfg)
                try:
                    return self.send(200, contacts_get(cfg, c, lang))
                finally:
                    c.close()
            if u.path == "/api/temps":
                c = adb(cfg)
                try:
                    return self.send(200, temps_get(cfg, c, lang))
                finally:
                    c.close()
            if u.path == "/api/abtest":
                c = adb(cfg)
                try:
                    return self.send(200, abtest.api_state(cfg, c, lang=lang))
                finally:
                    c.close()
            if u.path == "/api/interventions":
                try:
                    lim = max(1, min(200, int(p.get("limit", ["20"])[0])))
                except ValueError:
                    lim = 20
                c = adb(cfg)
                try:
                    return self.send(200, {"items": interventions.list_items(c, lim)})
                finally:
                    c.close()
            if u.path == "/api/reports":
                try:
                    lim = max(1, min(100, int(p.get("limit", ["8"])[0])))
                except ValueError:
                    lim = 8
                c = adb(cfg)
                try:
                    rows = c.execute("SELECT id, ts, week_from, week_to, text, cost FROM reports ORDER BY ts DESC LIMIT ?", (lim,)).fetchall()
                finally:
                    c.close()
                return self.send(200, {"items": [dict(zip(("id", "ts", "week_from", "week_to", "text", "cost"), r)) for r in rows]})
            if u.path.startswith("/api/stats/"):
                return self.stats_get(cfg, u.path[len("/api/stats/"):], p)
            self.send(404, {"error": "not found"})
        except Exception as e:
            self.send(500, {"error": "%s: %s" % (type(e).__name__, e)})

    def do_POST(self):
        u = urlparse(self.path)
        cfg = load_cfg()
        lang = i18n.req_lang(self.headers)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
            except ValueError:
                return self.send(400, {"error": tr("srv.app.neplatne_telo", lang)})
            if not isinstance(body, dict):
                return self.send(400, {"error": tr("srv.app.neplatne_telo", lang)})
            if u.path in ha_api.ACTIONS:   # integrácia Home Assistant: vlastný kľúč namiesto cookie / X-Kurenie
                if not self.ha_api_ok(cfg):
                    return self.send(401, {"error": "unauthorized"})
                return self.send(*ha_api.dispatch(cfg, u.path, body, sys.modules[__name__]))
            if u.path == mcp_api.PATH:   # MCP server pre Clauda: vlastný kľúč (Authorization: Bearer), bez cookie / X-Kurenie
                c = adb(cfg)
                try:
                    ok = mcp_api.check(c, self.headers.get("Authorization"))
                finally:
                    c.close()
                if not ok:
                    return self.send(401, {"error": "unauthorized"}, headers=[("WWW-Authenticate", "Bearer")])
                code, out = mcp_api.handle(cfg, body, sys.modules[__name__])
                return self.send(code, b"" if out is None else out)
            # jednoduchá ochrana pred cudzou stránkou: požiadavka musí prísť z tejto stránky
            if self.headers.get("X-Kurenie") != "1":
                return self.send(403, {"error": "forbidden"})
            ip = self.client_address[0]
            if ip in ("127.0.0.1", "::1"):   # požiadavka cez Cloudflare tunel (cloudflared beží lokálne): skutočná adresa klienta
                ip = (self.headers.get("CF-Connecting-IP") or ip).strip()[:64]
            if u.path == "/api/ims_import":
                return self.send(*ims_import(cfg, body, self.headers.get("X-Import-Token") or ""))
            if u.path == "/api/setup":
                return self.do_setup(cfg, body)
            if u.path == "/api/login":
                return self.do_login(cfg, body, ip)
            if u.path == "/api/access_login":
                return self.do_access_login(cfg, body, ip)
            ident = self.identity(cfg)
            if not ident:
                return self.send(401, {"error": "auth"})
            role, email = ident
            if role != "admin" and u.path not in USER_POST:   # centrálna kontrola oprávnení pre rolu `user`
                return self.send(403, {"error": tr("srv.app.no_perm", lang)})
            if u.path in ("/api/mcp_key", "/api/mcp_key_revoke"):   # kľúč MCP servera (len správca)
                c = adb(cfg)
                try:
                    if u.path == "/api/mcp_key":
                        return self.send(200, {"ok": True, "key": mcp_api.new_key(c), "info": mcp_api.key_info(c)})
                    mcp_api.revoke(c)
                    return self.send(200, {"ok": True, "info": mcp_api.key_info(c)})
                finally:
                    c.close()
            if u.path in ("/api/ha_key", "/api/ha_key_revoke"):   # kľúč integrácie Home Assistant (len správca)
                c = adb(cfg)
                try:
                    if u.path == "/api/ha_key":
                        return self.send(200, {"ok": True, "key": ha_api.new_key(c), "info": ha_api.key_info(c)})
                    ha_api.revoke(c)
                    return self.send(200, {"ok": True, "info": ha_api.key_info(c)})
                finally:
                    c.close()
            if u.path == "/api/logout":
                c = adb(cfg)
                try:
                    c.execute("DELETE FROM sessions WHERE token_hash=?", (tok_hash(self.token()),))
                    c.commit()
                finally:
                    c.close()
                return self.send(200, {"ok": True}, headers=self.set_cookie(""))
            if u.path.startswith("/api/push/"):
                c = adb(cfg)
                try:
                    what = u.path[len("/api/push/"):]
                    if what == "subscribe":
                        code, out = notify.push_subscribe(cfg, c, email, role, body.get("subscription"), body.get("ua"), lang)
                    elif what == "unsubscribe":
                        code, out = notify.push_unsubscribe(c, email, body.get("endpoint"), lang)
                    elif what == "test":
                        code, out = notify.push_test(cfg, c, email, lang)
                    elif what == "remove":
                        code, out = notify.push_remove(c, role, email, body.get("id"), lang)
                    else:
                        code, out = 404, {"error": "not found"}
                finally:
                    c.close()
                return self.send(code, out)
            if u.path == "/api/password":
                return self.do_password(cfg, body, ip)
            if u.path.startswith("/api/stats/"):
                return self.stats_post(cfg, u.path[len("/api/stats/"):], body)
            if u.path == "/api/settings":
                code, out = save_settings(cfg, body, lang)
                return self.send(code, out)
            if u.path == "/api/profil":
                code, out = profil_post(body, lang)
                return self.send(code, out)
            if u.path == "/api/profil/predvoleny":
                code, out = profil_default(lang)
                return self.send(code, out)
            if u.path == "/api/restart":
                return self.send(200, restart_services(cfg, lang))
            if u.path == "/api/message":
                text = str(body.get("text") or "").strip()
                if not text or len(text) > 2000:
                    return self.send(400, {"error": tr("srv.app.sprava_prazdna", lang)})
                c = adb(cfg)
                info = {}
                try:
                    if role != "admin" and c.execute("SELECT count(*) FROM messages WHERE author=? AND ts>?",
                                                     (email, int(time.time()) - 3600)).fetchone()[0] >= 30:
                        return self.send(429, {"error": tr("srv.app.sprav_limit", lang)})
                    reply, cost, err = chat_reply(cfg, c, text, info=info, role=role, email=email, lang=lang)
                finally:
                    c.close()
                return self.send(200, {"reply": reply, "cost": cost, "error": err, "messages": messages(cfg),
                                       "intervention": info.get("intervention")})
            if u.path == "/api/tunnel_setup":
                code, out = tunnel_auto(cfg, body, lang)
                return self.send(code, out)
            if u.path == "/api/access_config":
                code, out = access_config(cfg, body, lang)
                return self.send(code, out)
            if u.path == "/api/tunnel":
                code, out = tunnel_save(cfg, body, lang)
                return self.send(code, out)
            if u.path == "/api/tunnel_api":
                code, out = tunnel_api(cfg, body, lang)
                return self.send(code, out)
            if u.path in ("/api/users", "/api/users/delete"):
                c = adb(cfg)
                try:
                    code, out = users_set(cfg, c, body, lang) if u.path == "/api/users" else users_delete(cfg, c, body, lang)
                finally:
                    c.close()
                return self.send(code, out)
            if u.path == "/api/user_target":
                code, out = user_target_post(cfg, body, email, lang)
                return self.send(code, out)
            if u.path == "/api/room_target":
                code, out = room_target_post(cfg, body, email or tr("srv.notify.spravca", lang), lang)
                return self.send(code, out)
            if u.path == "/api/riadiaca":
                code, out = riadiaca_post(cfg, body, email or tr("srv.notify.spravca", lang), lang)
                return self.send(code, out)
            if u.path == "/api/tuv_boost":
                code, out = tuv_boost_post(cfg, body, email or tr("srv.notify.spravca", lang), lang)
                return self.send(code, out)
            if u.path == "/api/tuv_target":
                code, out = tuv_target_post(cfg, body, email or tr("srv.notify.spravca", lang), lang)
                return self.send(code, out)
            if u.path == "/api/test_key":
                return self.send(200, test_key(cfg, lang))
            if u.path == "/api/ha_test":
                tu, tt = str(body.get("ha_url") or "").strip().rstrip("/"), str(body.get("ha_token") or "").strip()
                if tu and not re.fullmatch(r"https?://[^\s]{1,300}", tu):
                    return self.send(400, {"error": tr("srv.app.adresa_ha", lang)})
                if tt and (len(tt) > 2048 or not re.fullmatch(r"[A-Za-z0-9_\-\.=]+", tt)):
                    return self.send(400, {"error": tr("srv.app.token_ha", lang)})
                return self.send(200, contacts.test(cfg, tu or None, tt or None, lang))
            if u.path == "/api/mqtt":
                c = adb(cfg)
                try:
                    code, out = mqtt_post(cfg, c, body, lang)
                finally:
                    c.close()
                return self.send(code, out)
            if u.path == "/api/mqtt_test":
                return self.send(200, mqtt_test(cfg, body, lang))
            if u.path.startswith("/api/contacts/"):
                c = adb(cfg)
                try:
                    code, out = contacts_post(cfg, c, u.path[len("/api/contacts/"):], body, lang)
                finally:
                    c.close()
                return self.send(code, out)
            if u.path.startswith("/api/temps/"):
                c = adb(cfg)
                try:
                    code, out = temps_post(cfg, c, u.path[len("/api/temps/"):], body, lang)
                finally:
                    c.close()
                return self.send(code, out)
            if u.path == "/api/notify_test":
                ok, err = notify.send(cfg, tr("srv.app.notify_test_titul", lang), tr("srv.app.notify_test_text", lang), "test", 0)
                return self.send(200, {"ok": bool(ok), "error": notify.err_text(err, lang)})
            if u.path == "/api/targets":
                code, out = targets_set(cfg, body, lang)
                return self.send(code, out)
            if u.path == "/api/intervention":
                text = str(body.get("text") or "").strip()
                if not text or len(text) > 500:
                    return self.send(400, {"error": tr("srv.app.zasah_popis", lang)})
                kind = body.get("kind") or "ine"
                if kind not in interventions.KINDS:
                    return self.send(400, {"error": tr("srv.app.zasah_typ", lang)})
                room = body.get("room") or None
                if room is not None and room not in ROOMS:
                    return self.send(400, {"error": tr("srv.app.neplatna_izba", lang)})
                try:
                    ts = parse_ts(body.get("ts"))
                except ValueError:
                    return self.send(400, {"error": tr("srv.app.zasah_cas", lang)})
                c = adb(cfg)
                try:
                    iid = interventions.add(c, "user_manual", kind, room, text, ts=ts)
                finally:
                    c.close()
                return self.send(200, {"ok": True, "id": iid})
            if u.path == "/api/intervention_cancel":
                c = adb(cfg)
                try:
                    ok = interventions.cancel(c, body.get("id"))
                except (TypeError, ValueError):
                    ok = False
                finally:
                    c.close()
                if not ok:
                    return self.send(400, {"error": tr("srv.app.zasah_zrusit", lang)})
                return self.send(200, {"ok": True})
            if u.path == "/api/abtest_start":
                c = adb(cfg)
                try:
                    code, out = abtest.start(cfg, c, body.get("days"), lang=lang)
                finally:
                    c.close()
                return self.send(code, out)
            if u.path == "/api/abtest_stop":
                c = adb(cfg)
                try:
                    ok = abtest.stop(cfg, c, tr("srv.app.abtest_stop_dovod", i18n.home_lang(cfg)))
                finally:
                    c.close()
                if not ok:
                    return self.send(400, {"error": tr("srv.app.ziadny_test", lang)})
                return self.send(200, {"ok": True})
            if u.path == "/api/undo":
                c = adb(cfg)
                try:
                    ok, msg = undo(cfg, c, body.get("action_id"), lang)
                finally:
                    c.close()
                return self.send(200 if ok else 400, {"ok": True, "message": msg} if ok else {"error": msg})
            c = adb(cfg)
            try:
                if u.path == "/api/mode":
                    m = body.get("mode")
                    if m not in MODES:
                        return self.send(400, {"error": tr("srv.app.neplatny_rezim", lang)})
                    old = get_state(c, "mode", "advise")
                    set_state(c, "mode", m)
                    c.execute("INSERT INTO runs(ts,mode,status,stav,summary,actions) VALUES(?,?,?,?,?,?)",
                              (int(time.time()), m, "info", None, tr("srv.app.rezim_log", i18n.home_lang(cfg), old=old, new=m), "[]"))
                    c.commit()
                    return self.send(200, {"mode": m})
                if u.path == "/api/run_now":
                    set_state(c, "run_now", "1")
                    return self.send(200, {"ok": True})
            finally:
                c.close()
            self.send(404, {"error": "not found"})
        except Exception as e:
            self.send(500, {"error": "%s: %s" % (type(e).__name__, e)})

    # --- štatistiky (SPEC_STATS §7); prihlásenie a X-Kurenie rieši do_GET / do_POST
    def send_csv(self, text, filename):
        self.send(200, text, "text/csv; charset=utf-8", headers=[("Content-Disposition", 'attachment; filename="%s"' % filename)])

    def static_icon(self, path):
        if path == "/manifest.webmanifest":
            m = dict(MANIFEST)
            try:
                hl = i18n.home_lang(load_cfg())
            except Exception:
                hl = "sk"
            if hl != "sk":   # názov aplikácie a jazyk PWA podľa jazyka domácnosti (UI_LANG)
                m.update(name=tr("srv.app.manifest_name", hl), short_name=tr("srv.app.manifest_short", hl), lang=hl)
            return self.send(200, json.dumps(m, ensure_ascii=False), "application/manifest+json; charset=utf-8", cache="public, max-age=86400")
        fp = os.path.join(ROOT, "assets", STATIC[path])
        if not os.path.isfile(fp):
            return self.send(404, {"error": "not found"})
        with open(fp, "rb") as f:
            return self.send(200, f.read(), "image/png", cache="public, max-age=86400")

    def static_sw(self):
        fp = os.path.join(ROOT, "sw.js")
        if not os.path.isfile(fp):
            return self.send(404, {"error": "not found"})
        with open(fp, "rb") as f:
            return self.send(200, f.read(), "application/javascript; charset=utf-8", headers=[("Service-Worker-Allowed", "/")], cache="no-cache")

    def static_locale(self, path):
        # názov súboru je overený regulárnym výrazom ^/locales/[a-z]{2}\.json$ (žiadny path traversal)
        fp = os.path.join(ROOT, "locales", path.rsplit("/", 1)[1])
        if not os.path.isfile(fp):
            return self.send(404, {"error": "not found"})
        with open(fp, "rb") as f:
            return self.send(200, f.read(), "application/json; charset=utf-8", cache="no-cache")

    def stats_static(self, path):
        name, ctype = STATS_STATIC[path]
        fp = os.path.join(ROOT, name)
        if not os.path.isfile(fp):
            return self.send(404, {"error": "not found"})
        with open(fp, "rb") as f:
            return self.send(200, f.read(), ctype)

    def stats_get(self, cfg, what, p):
        lang = i18n.req_lang(self.headers)
        if (p.get("lang") or [""])[0] in i18n.known_langs():   # export CSV je odkaz (bez hlavičiek): jazyk ide v parametri `lang`
            lang = p["lang"][0]
        if stats is None:
            return self.send(503, {"error": tr("srv.app.stats_na", lang)})
        try:
            if what == "meta":
                return self.send(200, {"metrics": stats.metrics(lang), "coverage": stats.coverage(cfg, lang), "hdd_base": hdd_base(cfg),
                                       "hdd_note": tr("srv.app.stats_hdd_note", lang)})
            if what == "day":
                return self.send(200, stats.get_day(cfg, stats_qdate(p, "d", today_s(), lang), lang))
            if what == "range":
                frm, to, g = stats_range_args(p, lang)
                return self.send(200, stats.get_range(cfg, frm, to, g, lang))
            if what == "compare":
                return self.send(200, stats.compare(cfg, stats_compare_dates(p, lang), lang))
            if what == "quick":
                return self.send(200, {"items": stats.quick_dates(cfg, stats_qdate(p, "base", today_s(), lang), lang)})
            if what == "faults":
                frm, to = stats_period(p, lang)
                only = p.get("open", [""])[0] in ("1", "true", "yes")
                return self.send(200, {"items": stats.faults_list(cfg, frm, to, only_open=only, lang=lang)})
            if what == "codes":
                return self.send(200, {"items": stats.fault_codes_list(cfg, lang)})
            if what == "export":
                kind = p.get("kind", [""])[0]
                if kind == "day":
                    d = stats_qdate(p, "d", today_s(), lang)
                    text, tag = stats.export_csv(cfg, "day", date=d, lang=lang), d
                elif kind == "range":
                    frm, to, g = stats_range_args(p, lang)
                    text, tag = stats.export_csv(cfg, "range", date_from=frm, date_to=to, group=g, lang=lang), "%s_%s" % (frm, to)
                elif kind == "compare":
                    dates = stats_compare_dates(p, lang)
                    text, tag = stats.export_csv(cfg, "compare", dates=dates, lang=lang), dates[0]
                else:
                    raise ValueError(tr("srv.app.stats_export", lang))
                return self.send_csv(text, "statistiky-%s-%s.csv" % (kind, tag))
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        return self.send(404, {"error": "not found"})

    def stats_post(self, cfg, what, body):
        lang = i18n.req_lang(self.headers)
        if stats is None:
            return self.send(503, {"error": tr("srv.app.stats_na", lang)})
        try:
            if what == "fault":
                ts_start = stats_ts(body.get("ts_start"), lang)
                if ts_start is None:
                    raise ValueError(tr("srv.app.stats_chyba_zaciatok", lang))
                ts_end = stats_ts(body.get("ts_end"), lang)
                if ts_end is not None and ts_end < ts_start:
                    raise ValueError(tr("srv.app.stats_koniec", lang))
                fid = stats.fault_add(cfg, ts_start, ts_end, stats_code(body.get("code"), lang),
                                      stats_text(body, "title", 200, lang), stats_text(body, "text", 2000, lang), lang)
                return self.send(200, {"ok": True, "id": fid})
            if what == "fault_delete":
                fid = body.get("id")
                if isinstance(fid, bool) or not isinstance(fid, int):
                    raise ValueError(tr("srv.app.stats_id_poruchy", lang))
                stats.fault_delete(cfg, fid, lang)
                return self.send(200, {"ok": True})
            if what == "fault_code":
                stats.fault_code_set(cfg, stats_code(body.get("code"), lang), stats_text(body, "title", 200, lang),
                                     stats_text(body, "description", 2000, lang), stats_text(body, "fix", 2000, lang), lang)
                return self.send(200, {"ok": True})
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        return self.send(404, {"error": "not found"})

    # --- prihlásenie
    def do_setup(self, cfg, body):
        lang = i18n.req_lang(self.headers)
        pw = body.get("password")
        c = adb(cfg)
        try:
            if get_state(c, "auth_hash"):
                return self.send(400, {"error": tr("srv.app.heslo_nastavene", lang)})
            if not isinstance(pw, str) or len(pw) < 8 or len(pw) > 200:
                return self.send(400, {"error": tr("srv.app.heslo_8", lang)})
            set_state(c, "auth_hash", hash_password(pw))
            tok = create_session(c)
        finally:
            c.close()
        return self.send(200, {"ok": True}, headers=self.set_cookie(tok))

    def do_login(self, cfg, body, ip):
        lang = i18n.req_lang(self.headers)
        wait = rate_blocked(ip)
        if wait:
            return self.send(429, {"error": tr("srv.app.privela_pokusov", lang, n=wait)})
        pw = body.get("password")
        c = adb(cfg)
        try:
            h = get_state(c, "auth_hash")
            if not h:
                return self.send(400, {"error": tr("srv.app.heslo_nenastavene", lang)})
            if not isinstance(pw, str) or not verify_password(pw, h):
                rate_fail(ip)   # po 5. neúspešnom pokuse sa ďalšie pokusy 60 s odmietajú (429)
                return self.send(401, {"error": tr("srv.app.nespravne_heslo", lang)})
            rate_ok(ip)
            tok = create_session(c)
        finally:
            c.close()
        return self.send(200, {"ok": True}, headers=self.set_cookie(tok))

    def do_access_login(self, cfg, body, ip):
        """POST /api/access_login {as: user|admin, password?}: relácia až po explicitnej voľbe roly, len s overenou Access identitou."""
        lang = i18n.req_lang(self.headers)
        ai = self.access_identity(cfg)
        if not ai:
            return self.send(403, {"error": tr("srv.app.cf_login", lang)})
        want = body.get("as")
        if want not in ("user", "admin"):
            return self.send(400, {"error": tr("srv.app.vyber_rolu", lang)})
        c = adb(cfg)
        try:
            if want == "admin":
                if ai["role"] != "admin":
                    return self.send(403, {"error": tr("srv.app.email_nie_spravca", lang)})
                pass   # správcu už overil Cloudflare Access (e-mail s rolou admin v zozname používateľov); heslo sa nevyžaduje
            tok = create_session(c, want, ai["email"])
        finally:
            c.close()
        return self.send(200, {"ok": True, "role": want, "email": ai["email"]}, headers=self.set_cookie(tok))

    def do_password(self, cfg, body, ip):
        lang = i18n.req_lang(self.headers)
        wait = rate_blocked(ip)
        if wait:
            return self.send(429, {"error": tr("srv.app.privela_pokusov", lang, n=wait)})
        old, new = body.get("old"), body.get("new")
        c = adb(cfg)
        try:
            if not isinstance(old, str) or not verify_password(old, get_state(c, "auth_hash", "") or ""):
                rate_fail(ip)
                return self.send(400, {"error": tr("srv.app.sucasne_heslo", lang)})
            if not isinstance(new, str) or len(new) < 8 or len(new) > 200:
                return self.send(400, {"error": tr("srv.app.nove_heslo_8", lang)})
            set_state(c, "auth_hash", hash_password(new))
            c.execute("DELETE FROM sessions WHERE token_hash!=?", (tok_hash(self.token()),))   # ostatné session sa zrušia
            c.commit()
        finally:
            c.close()
        rate_ok(ip)
        return self.send(200, {"ok": True})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(load_cfg()["WEB_PORT"])), H).serve_forever()
