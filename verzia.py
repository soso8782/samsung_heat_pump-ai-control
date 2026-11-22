#!/usr/bin/env python3
"""Verzia aplikácie a kontrola novšej verzie (blok `verzia` v /api/status).

app_version()        obsah VERSION (X.Y.Z) alebo None
git_commit(root)    {commit, datum} z `git log -1` (len ak inštalácia má .git); inak None; cache 10 min
parse_semver(s)     „v2.14.0“ / „2.14.0“ -> (2, 14, 0); neplatné -> None
latest_tag(cfg)     najvyššia verzia vX.Y.Z z tagov GitHub repozitára; cache 6 h, pri chybe 1 h; UPDATE_CHECK=0 -> None
info(cfg, firmver)  blok `verzia` pre správcu (app, commit, datum, adresar, firmver, najnovsia, novsia, kontrola)
Nič tu nevyhodí výnimku okrem neočakávaných chýb v kóde; sieť a git sú len s krátkym timeoutom."""
import json, os, re, subprocess, time, urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
REPO_TAGS_URL = "https://api.github.com/repos/soso8782/samsung_heat_pump-ai-control/tags"
USER_AGENT = "samsung-heat-pump-ai-control"
GIT_TIMEOUT = 3          # s
API_TIMEOUT = 5          # s
GIT_TTL = 600            # s, commit sa mení len pri aktualizácii
LATEST_OK_TTL = 6 * 3600
LATEST_FAIL_TTL = 3600
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_cache = {"git": {"loaded": False, "t": 0.0, "v": None}, "latest": {"loaded": False, "t": 0.0, "ttl": 0, "v": None}}


def app_version(root=None):
    root = ROOT if root is None else root
    try:
        with open(os.path.join(root, "VERSION"), encoding="utf-8") as f:
            v = f.read().strip()
    except OSError:
        return None
    return v or None


def parse_semver(s):
    m = _SEMVER.match(str(s or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def git_commit(root=None, now=None):
    """Krátky hash a dátum posledného commitu inštalácie. Bez .git v adresári (alebo bez gitu) = None."""
    root = ROOT if root is None else root
    now = time.time() if now is None else now
    g = _cache["git"]
    if g["loaded"] and now - g["t"] < GIT_TTL:
        return g["v"]
    v = None
    if os.path.exists(os.path.join(root, ".git")):
        try:
            r = subprocess.run(["git", "-C", root, "log", "-1", "--format=%h|%cI"], capture_output=True, text=True,
                               timeout=GIT_TIMEOUT)
            h, _, d = (r.stdout or "").strip().partition("|")
            if r.returncode == 0 and h and d:
                v = {"commit": h, "datum": d}
        except (OSError, subprocess.SubprocessError):
            v = None
    g.update(loaded=True, t=now, v=v)
    return v


def update_check_on(cfg):
    return str((cfg or {}).get("UPDATE_CHECK", "1")).strip().lower() not in ("0", "false", "no", "off")


def latest_tag(cfg, now=None):
    """Najvyšší tag vX.Y.Z repozitára na GitHube, alebo None (vypnuté / chyba / žiadny semver tag)."""
    if not update_check_on(cfg):
        return None
    now = time.time() if now is None else now
    c = _cache["latest"]
    if c["loaded"] and now - c["t"] < c["ttl"]:
        return c["v"]
    v, ok = None, False
    try:
        req = urllib.request.Request(REPO_TAGS_URL, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as r:
            tags = json.loads(r.read().decode("utf-8"))
        vers = [p for p in (parse_semver(x.get("name")) for x in tags if isinstance(x, dict)) if p]
        v = "v%d.%d.%d" % max(vers) if vers else None
        ok = True
    except Exception:
        v, ok = None, False
    c.update(loaded=True, t=now, ttl=LATEST_OK_TTL if ok else LATEST_FAIL_TTL, v=v)
    return v


def info(cfg, firmver=None, root=None, now=None):
    """Blok `verzia` pre správcu. `firmver` = výstup app.firmver_info ({verzia, pozadovana, stary, nedostupne, hint})."""
    root = ROOT if root is None else root
    app = app_version(root)
    g = git_commit(root, now)
    latest = latest_tag(cfg, now)
    ap, lp = parse_semver(app), parse_semver(latest)
    return {"app": app, "commit": g["commit"] if g else None, "datum": g["datum"] if g else None, "adresar": root,
            "firmver": firmver, "najnovsia": latest, "novsia": bool(ap and lp and lp > ap), "kontrola": update_check_on(cfg)}
