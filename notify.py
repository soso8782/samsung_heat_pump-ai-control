#!/usr/bin/env python3
"""Upozornenia agenta kúrenia: webhook (Home Assistant) a Web Push (iOS PWA, Android, desktop).

send() nikdy nevyhodí výnimku - vracia (ok, chyba). V skúšobnom režime (DRY_RUN) sa nič neodošle, iba zaloguje.
Webhook aj push sú na sebe nezávislé kanály; ok = aspoň jeden uspel. Tajomstvá (VAPID súkromný kľúč, endpointy a kľúče
predplatiteľov) sa nikdy nelogujú ani nevracajú v odpovediach.
"""
import base64, hashlib, ipaddress, json, os, re, threading, time, urllib.request
from urllib.parse import urlparse
from common import REF_ROOM, ROOT, adb, is_dry, tcq, S
import i18n
from i18n import tr
try:   # Web Push potrebuje python3-cryptography; ak chýba, webhook funguje ďalej
    import webpush
except Exception:
    webpush = None

# kind -> predvolený cooldown v sekundách (0 = bez obmedzenia)
COOLDOWNS = {"agent_zmena": 0, "agent_problem": 3 * 3600, "agent_chyba": 6 * 3600, "zberac": 3 * 3600, "zberac_ok": 0, "cidlo": 0, "cidlo_ok": 0,
             "esp": 3 * 3600, "chyba_tc": 3600, "dohrev": 6 * 3600, "kredit": 24 * 3600, "vyhodnotenie": 0,
             "tyzden": 0, "suhrn": 0, "tuv": 6 * 3600, "test": 0, "abtest": 0,
             "okno": 0,                # riadi si to contacts.check_notifications (pamäť v state, pripomienky)
             "teplota": 0,             # riadi si to temps.check_notifications (pamäť v state, opakovanie najviac raz za 3 h)
             "cielova_zmena": 0,       # zmena cieľovej izbovej teploty (web aj chat); posiela sa všetkým zariadeniam
             "ha": 6 * 3600}        # Home Assistant neodpovedá

# druhy upozornení, ktoré dostanú aj zariadenia role `user` (správca dostane všetky)
PUSH_USER_KINDS = frozenset(("okno", "teplota", "cielova_zmena", "test"))
PUSH_TOTAL_TIMEOUT = 10.0      # najdlhšie čakanie volajúceho na celé odoslanie pushov [s]
PUSH_ONE_TIMEOUT = 6           # timeout jedného HTTP volania [s]
PUSH_MAX_DEVICES_PER_OWNER = 10
PUSH_MAX_TARGETS = 40
VAPID_FILE = os.path.join(ROOT, "vapid.json")
DEFAULT_SUBJECT = "mailto:kurenie@localhost.invalid"
_vapid_lock = threading.Lock()


# ---------------------------------------------------------------- VAPID kľúče
def vapid_keys(lang="sk"):
    """(súkromný, verejný) VAPID kľúč z ROOT/vapid.json; pri prvom použití sa vygeneruje (práva 600).
    Existujúci poškodený súbor sa NEprepíše (zmena kľúča by zneplatnila všetky predplatné) - vyhodí RuntimeError."""
    if webpush is None:
        raise RuntimeError(tr("srv.notify.vapid_lib", lang))
    with _vapid_lock:
        if os.path.exists(VAPID_FILE):
            try:
                with open(VAPID_FILE, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d.get("private"), str) and isinstance(d.get("public"), str) and d["private"] and d["public"]:
                    return d["private"], d["public"]
            except (OSError, ValueError, AttributeError):
                pass
            raise RuntimeError(tr("srv.notify.vapid_bad", lang))
        priv, pub = webpush.generate_vapid()
        tmp = VAPID_FILE + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"private": priv, "public": pub}, f)
        os.replace(tmp, VAPID_FILE)
        try:
            os.chmod(VAPID_FILE, 0o600)
        except OSError:
            pass
        return priv, pub


def vapid_subject(cfg):
    s = str(cfg.get("VAPID_SUBJECT") or "").strip()
    if s.lower().startswith("mailto:") and len(s) > 8:
        return s
    if s.lower().startswith("https://"):
        return s
    if re.fullmatch(r"[^@\s]+@[^@\s]+", s):
        return "mailto:" + s
    return DEFAULT_SUBJECT


# ---------------------------------------------------------------- predplatné
def _b64u_dec(s):
    if not isinstance(s, str) or not re.fullmatch(r"[A-Za-z0-9_\-]+=*", s.strip()):
        raise ValueError("neplatný kľúč")
    s = s.strip().rstrip("=")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64u_enc(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def push_id(endpoint):
    """Neutajené id zariadenia (skrátený hash endpointu); endpoint sa nikdy nevracia."""
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:16]


def validate_subscription(sub, lang="sk"):
    """-> (endpoint, p256dh, auth) alebo ValueError s textom po slovensky."""
    if not isinstance(sub, dict):
        raise ValueError(tr("srv.notify.chyba_predplatne", lang))
    ep = sub.get("endpoint")
    keys = sub.get("keys")
    if not isinstance(ep, str) or not isinstance(keys, dict):
        raise ValueError(tr("srv.notify.neplatne_predplatne", lang))
    ep = ep.strip()
    if not (20 <= len(ep) <= 1000) or not re.fullmatch(r"[\x21-\x7e]+", ep):
        raise ValueError(tr("srv.notify.neplatna_adresa", lang))
    u = urlparse(ep)
    host = (u.hostname or "").lower()
    try:
        port = u.port
    except ValueError:
        port = -1
    if u.scheme != "https" or not host or "." not in host or u.username or u.password or port not in (None, 443):
        raise ValueError(tr("srv.notify.adresa_https", lang))
    is_ip = True
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        is_ip = False
    if is_ip:
        raise ValueError(tr("srv.notify.adresa_ip", lang))
    if host.endswith((".local", ".localhost", ".internal", ".lan", ".home", ".test", ".invalid")) or host == "localhost":
        raise ValueError(tr("srv.notify.adresa_verejna", lang))
    try:
        p = _b64u_dec(keys.get("p256dh"))
        a = _b64u_dec(keys.get("auth"))
    except (ValueError, TypeError):
        raise ValueError(tr("srv.notify.kluce", lang))
    if len(p) != 65 or p[0] != 4 or len(a) != 16:
        raise ValueError(tr("srv.notify.kluce", lang))
    return ep, _b64u_enc(p), _b64u_enc(a)


def _clean_err(text, row=None):
    t = str(text or "")[:200]
    if row:
        for k in ("endpoint", "p256dh", "auth"):
            if row.get(k):
                t = t.replace(row[k], "…")
    return t


def push_subscribe(cfg, c, email, role, sub, ua, lang="sk"):
    """POST /api/push/subscribe: uloží (alebo obnoví) zariadenie pre prihláseného používateľa. -> (code, out)."""
    if webpush is None:
        return 503, {"error": tr("srv.notify.push_nedostupny", lang)}
    try:
        ep, p256, auth = validate_subscription(sub, lang)
    except ValueError as e:
        return 400, {"error": str(e)}
    ua = ua if isinstance(ua, str) else ""
    ua = re.sub(r"[\x00-\x1f\x7f]", " ", ua).strip()[:300]
    now = int(time.time())
    own = c.execute("SELECT endpoint FROM push_subs WHERE email IS ?", (email,)).fetchall()
    if ep not in [r[0] for r in own] and len(own) >= PUSH_MAX_DEVICES_PER_OWNER:
        return 400, {"error": tr("srv.notify.max_zariadeni", lang, n=PUSH_MAX_DEVICES_PER_OWNER)}
    cur = c.execute("SELECT 1 FROM push_subs WHERE endpoint=?", (ep,)).fetchone()
    if cur:
        c.execute("UPDATE push_subs SET email=?, role=?, p256dh=?, auth=?, ua=?, last_err=NULL WHERE endpoint=?",
                  (email, role, p256, auth, ua, ep))
    else:
        c.execute("INSERT INTO push_subs(endpoint, email, role, p256dh, auth, ua, created, last_ok, last_err) VALUES(?,?,?,?,?,?,?,NULL,NULL)",
                  (ep, email, role, p256, auth, ua, now))
    c.commit()
    return 200, {"ok": True, "id": push_id(ep)}


def push_unsubscribe(c, email, endpoint, lang="sk"):
    """POST /api/push/unsubscribe: zmaže iba vlastné zariadenie (podľa e-mailu)."""
    if not isinstance(endpoint, str) or not endpoint.strip():
        return 400, {"error": tr("srv.notify.chyba_zariadenie", lang)}
    cur = c.execute("DELETE FROM push_subs WHERE endpoint=? AND email IS ?", (endpoint.strip(), email))
    c.commit()
    return 200, {"ok": True, "removed": cur.rowcount}


def _current_roles(c):
    return dict(c.execute("SELECT email, role FROM users").fetchall())


def push_status(cfg, c, role, email, lang="sk"):
    """GET /api/push/status: správca vidí všetky zariadenia, používateľ iba svoje. Bez endpointov a kľúčov."""
    roles = _current_roles(c)
    rows = c.execute("SELECT endpoint, email, role, ua, created, last_ok, last_err FROM push_subs ORDER BY created, endpoint").fetchall()
    devices = []
    for ep, em, rl, ua, created, last_ok, last_err in rows:
        mine = em == email
        if role != "admin" and not mine:
            continue
        d = {"id": push_id(ep), "ua": ua or "", "created": created, "last_ok": last_ok, "last_err": last_err, "mine": mine}
        if role == "admin":
            d["email"] = em
            d["role"] = roles.get(em, rl) if em else (rl or "admin")
        devices.append(d)
    return {"configured": webpush is not None, "dry_run": is_dry(cfg), "devices": devices}


def push_remove(c, role, email, did, lang="sk"):
    """POST /api/push/remove {id}: správca kohokoľvek, používateľ iba vlastné."""
    if not isinstance(did, str) or not re.fullmatch(r"[0-9a-f]{16}", did):
        return 400, {"error": tr("srv.notify.neplatne_zariadenie", lang)}
    for ep, em in c.execute("SELECT endpoint, email FROM push_subs").fetchall():
        if push_id(ep) == did:
            if role != "admin" and em != email:
                return 403, {"error": tr("srv.notify.no_perm", lang)}
            c.execute("DELETE FROM push_subs WHERE endpoint=?", (ep,))
            c.commit()
            return 200, {"ok": True}
    return 404, {"error": tr("srv.notify.nenaslo", lang)}


# ---------------------------------------------------------------- odoslanie pushu
def badge_value(cfg):
    """Zaokrúhlená aktuálna teplota referenčnej izby (celé číslo) alebo None, ak nie je čerstvá."""
    try:
        meta = dict(tcq(cfg, "SELECT k, v FROM meta"))
        ls = int(meta.get("last_sample") or 0)
        if not ls or time.time() - ls > 3600:
            return None
        rows = tcq(cfg, "SELECT value FROM samples WHERE ts=? AND entity=?", (ls, S.get(REF_ROOM, "")))
        if rows and rows[0][0] is not None and -30 < rows[0][0] < 60:
            return int(round(rows[0][0]))
    except Exception:
        pass
    return None


def push_payload(cfg, title, message, kind):
    d = {"title": str(title or "")[:120], "body": str(message or "")[:600], "tag": kind, "url": "/"}
    b = badge_value(cfg)
    if b is not None:
        d["badge"] = b
    return d


def push_targets(c, kind=None, email=False):
    """Zariadenia, ktoré majú dostať upozornenie druhu `kind` (kind=None = všetky; email=<str|None> = len zariadenia daného e-mailu,
    bez ohľadu na druh). Rola sa berie aktuálna z `users`; bez e-mailu (správca s heslom) alebo ak e-mail už nie je v `users`
    sa použije uložená."""
    roles = _current_roles(c)
    out = []
    for ep, em, rl, p, a in c.execute("SELECT endpoint, email, role, p256dh, auth FROM push_subs ORDER BY created, endpoint").fetchall():
        if email is not False:
            if em != email:
                continue
        else:
            if em and em not in roles:   # odstránený používateľ už upozornenia nedostáva
                continue
            role = roles.get(em) if em else None
            role = role or rl or "user"
            if kind is not None and role != "admin" and kind not in PUSH_USER_KINDS:
                continue
        out.append({"endpoint": ep, "email": em, "p256dh": p, "auth": a})
    return out[:PUSH_MAX_TARGETS]


_WP_ERRS = ((re.compile(r"^payload je prilis dlhy \(max (\d+) bajtov\)$"), "wp_payload", "n"), (re.compile(r"^neplatny endpoint$"), "wp_endpoint", None),
            (re.compile(r"^chyba push sluzby \(HTTP (\d+)\)$"), "wp_service", "code"), (re.compile(r"^chyba: (.*)$"), "wp_error", "e"))


def _wp_err(err, lang="sk"):
    """Chyba z webpush.send (ASCII slovenčina) -> text v jazyku `lang` (sk bez zmeny)."""
    if lang == "sk" or not isinstance(err, str):
        return err
    for rx, key, var in _WP_ERRS:
        m = rx.match(err)
        if m:
            return tr("srv.notify." + key, lang, **({var: m.group(1)} if var else {}))
    return err


def push_deliver(cfg, c, targets, payload, total_timeout=PUSH_TOTAL_TIMEOUT, lang="sk"):
    """Odošle payload na zariadenia (paralelne, celkovo najviac ~total_timeout s). 404/410 = predplatné sa zmaže, iné chyby sa
    uložia do last_err. Vracia (počet_ok, [chyby]). Nikdy nevyhodí výnimku."""
    if not targets:
        return 0, []
    try:
        priv, pub = vapid_keys(lang)
    except Exception as e:
        return 0, [_clean_err("Web Push: %s" % e)]
    subj = vapid_subject(cfg)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    results = {}

    def work(t):
        try:
            code, err = webpush.send({"endpoint": t["endpoint"], "keys": {"p256dh": t["p256dh"], "auth": t["auth"]}},
                                     body, priv, pub, subj, ttl=3600, timeout=PUSH_ONE_TIMEOUT)
        except Exception as e:
            code, err = 0, type(e).__name__
        results[t["endpoint"]] = (code, err)

    ths = [threading.Thread(target=work, args=(t,), daemon=True) for t in targets]
    for th in ths:
        th.start()
    end = time.time() + total_timeout
    for th in ths:
        th.join(max(0.0, end - time.time()))
    n_ok, errs, now = 0, [], int(time.time())
    for t in targets:
        r = results.get(t["endpoint"])
        if r is None:
            errs.append(tr("srv.notify.push_timeout", lang))
            continue
        code, err = r
        if code in (200, 201, 202):
            n_ok += 1
            c.execute("UPDATE push_subs SET last_ok=?, last_err=NULL WHERE endpoint=?", (now, t["endpoint"]))
        elif code in (404, 410):
            c.execute("DELETE FROM push_subs WHERE endpoint=?", (t["endpoint"],))
            errs.append(tr("srv.notify.push_gone", lang))
        else:
            txt = _clean_err("%s%s" % ("%s: " % code if code else "", _wp_err(err, lang) or tr("srv.notify.push_err", lang)), t)
            c.execute("UPDATE push_subs SET last_err=? WHERE endpoint=?", (txt, t["endpoint"]))
            errs.append("push: " + txt)
    c.commit()
    return n_ok, errs


def push_test(cfg, c, email, lang="sk"):
    """POST /api/push/test: skúšobná notifikácia na všetky zariadenia prihláseného používateľa (bez ohľadu na rolu a cooldown)."""
    if webpush is None:
        return 503, {"error": tr("srv.notify.push_nedostupny", lang)}
    targets = push_targets(c, email=email)
    if not targets:
        return 400, {"error": tr("srv.notify.ziadne_zariadenie", lang)}
    if is_dry(cfg):
        print("[DRY_RUN] skúšobný push na %d zariadení" % len(targets), flush=True)
        return 200, {"ok": True, "dry": True, "devices": len(targets), "sent": 0,
                     "note": tr("srv.notify.dry_nic", lang)}
    n, errs = push_deliver(cfg, c, targets, push_payload(cfg, tr("srv.notify.skuska_titul", lang), tr("srv.notify.skuska_text", lang), "test"), lang=lang)
    out = {"ok": n > 0, "devices": len(targets), "sent": n}
    if n == 0:
        out["error"] = errs[0] if errs else tr("srv.notify.odoslanie_zlyhalo", lang)
    return 200, out


# ---------------------------------------------------------------- hlavné rozhranie
def event(cfg, kind, title, message):
    """Pošle upozornenie s predvoleným cooldownom pre daný druh."""
    return send(cfg, title, message, kind, COOLDOWNS.get(kind, 0))


def target_changed(cfg, old, new, who):
    """Upozornenie na zmenu cieľovej izbovej teploty (všetky zariadenia aj webhook). Nič, ak sa hodnota nezmenila."""
    try:
        if new is None or (old is not None and abs(float(old) - float(new)) < 1e-9):
            return False, "bez zmeny"
        hl = i18n.home_lang(cfg)
        who = str(who or tr("srv.notify.spravca", hl))[:120]
        return event(cfg, "cielova_zmena", tr("srv.notify.cielova_titul", hl),
                     tr("srv.notify.cielova_text", hl, v=i18n.dec(("%.1f" % float(new)).replace(".", ","), hl), who=who))
    except Exception as e:
        return False, ("%s: %s" % (type(e).__name__, e))[:300]


def send(cfg, title, message, kind, cooldown_s=0):
    """Webhook (POST {"title","message","kind"} na NOTIFY_URL, ak je nastavená) a Web Push na zariadenia podľa druhu a roly.
    Kanály sú nezávislé. Cooldown platí pre celé upozornenie. Vracia (ok, chyba); ok = aspoň jeden kanál uspel."""
    url = (cfg.get("NOTIFY_URL") or "").strip()
    c = None
    try:
        c = adb(cfg)
        targets = push_targets(c, kind) if webpush is not None else []
        if not url and not targets:
            return False, "nenastavené"
        now = int(time.time())
        if cooldown_s and cooldown_s > 0:
            r = c.execute("SELECT max(ts) FROM notify_log WHERE kind=? AND ok=1", (kind,)).fetchone()
            if r and r[0] and now - r[0] < cooldown_s:
                return False, "cooldown"
            f = c.execute("SELECT max(ts) FROM notify_log WHERE kind=? AND ok=0", (kind,)).fetchone()
            if f and f[0] and now - f[0] < 300 and (not r or not r[0] or r[0] < f[0]):
                return False, "predchádzajúci pokus zlyhal, skúsim neskôr"   # nezahlcovať nefunkčný kanál
        title, message = str(title or "")[:200], str(message or "")[:1500]
        ok, err = True, None
        if is_dry(cfg):
            print("[DRY_RUN] upozornenie [%s] %s: %s (webhook: %s, push zariadení: %d)" % (kind, title, message, "áno" if url else "nie", len(targets)), flush=True)
            err = "DRY_RUN (neodoslané)"
        else:
            oks, errs = 0, []
            if url:
                try:
                    req = urllib.request.Request(url, data=json.dumps({"title": title, "message": message, "kind": kind},
                                                                       ensure_ascii=False).encode("utf-8"),
                                                 headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
                    with urllib.request.urlopen(req, timeout=10) as r:
                        r.read(2000)
                    oks += 1
                except Exception as e:
                    errs.append("webhook: %s: %s" % (type(e).__name__, e))
            if targets:
                n, perrs = push_deliver(cfg, c, targets, push_payload(cfg, title, message, kind), lang=i18n.home_lang(cfg))
                oks += 1 if n else 0
                errs += perrs
            ok = oks > 0
            err = ("; ".join(errs)[:300] or None) if (errs or not ok) else None
            if not ok and not err:
                err = "odoslanie zlyhalo"
        c.execute("INSERT INTO notify_log(ts, kind, title, message, ok, error) VALUES(?,?,?,?,?,?)",
                  (now, kind, title, message, 1 if ok else 0, err))
        c.commit()
        return ok, (None if ok else err)
    except Exception as e:
        return False, ("%s: %s" % (type(e).__name__, e))[:300]
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass


_ERR_KEYS = {"nenastavené": "nenastavene", "cooldown": "cooldown", "DRY_RUN (neodoslané)": "dry_run",
             "predchádzajúci pokus zlyhal, skúsim neskôr": "predchadzajuci", "odoslanie zlyhalo": "zlyhalo", "bez zmeny": "bez_zmeny"}

def err_text(err, lang="sk"):
    """Preklad chybového kódu z send() pre zobrazenie na webe (neznáme texty sa vrátia bez zmeny)."""
    if lang == "sk" or err not in _ERR_KEYS:
        return err
    return tr("srv.notify.err_" + _ERR_KEYS[err], lang)


def status(cfg, c):
    """Pre web: nastavené?, čas posledného úspešného odoslania, posledná chyba (ak posledný pokus zlyhal), počet push zariadení."""
    last_ok = c.execute("SELECT max(ts) FROM notify_log WHERE ok=1").fetchone()[0]
    r = c.execute("SELECT ok, error FROM notify_log ORDER BY ts DESC, id DESC LIMIT 1").fetchone()
    return {"configured": bool((cfg.get("NOTIFY_URL") or "").strip()), "last_ok": last_ok,
            "last_error": r[1] if r and not r[0] else None,
            "push_devices": c.execute("SELECT count(*) FROM push_subs").fetchone()[0]}
