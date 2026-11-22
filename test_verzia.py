#!/usr/bin/env python3
"""Testy verzie (verzia.py + blok `verzia` v /api/status): parse/porovnanie, git (bez gitu, cache, chyba), kontrola GitHubu
(cache 6 h, chyba 1 h, UPDATE_CHECK=0), API pole pre správcu a rolu user. subprocess aj urlopen sú mockované, nič sa nepripája."""
import json, os, shutil, subprocess, sys, tempfile, unittest, urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verzia

NOW = 1_800_000_000.0
GIT_OUT = "abc1234|2026-11-22T10:00:00+01:00\n"


def _resp(payload):
    cm = mock.MagicMock()
    cm.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
    return cm


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        with open(os.path.join(self.d, "VERSION"), "w", encoding="utf-8") as f:
            f.write("2.14.0\n")
        self._reset()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    @staticmethod
    def _reset():
        verzia._cache["git"].update(loaded=False, t=0.0, v=None)
        verzia._cache["latest"].update(loaded=False, t=0.0, ttl=0, v=None)

    def make_repo(self):
        open(os.path.join(self.d, ".git"), "w").close()     # inštalácia ako git repozitár (worktree má .git súbor)


class Parse(unittest.TestCase):
    def test_parse_semver(self):
        self.assertEqual(verzia.parse_semver("v2.14.0"), (2, 14, 0))
        self.assertEqual(verzia.parse_semver(" 2.14.0 \n"), (2, 14, 0))
        for bad in ("2.14", "v2.14.0-rc1", "release-2", "", None):
            self.assertIsNone(verzia.parse_semver(bad), bad)

    def test_compare_is_semantic(self):
        self.assertGreater(verzia.parse_semver("v2.10.0"), verzia.parse_semver("v2.9.0"))
        self.assertGreater(verzia.parse_semver("v3.0.0"), verzia.parse_semver("v2.99.99"))
        self.assertFalse(verzia.parse_semver("v2.14.0") > verzia.parse_semver("2.14.0"))


class AppVersion(Base):
    def test_version_file(self):
        self.assertEqual(verzia.app_version(self.d), "2.14.0")

    def test_missing_version_file(self):
        self.assertIsNone(verzia.app_version(os.path.join(self.d, "nie-je")))


class Git(Base):
    def test_no_git_dir_no_subprocess(self):
        with mock.patch.object(verzia.subprocess, "run") as run:
            self.assertIsNone(verzia.git_commit(self.d, NOW))
        run.assert_not_called()

    def test_commit_and_date(self):
        self.make_repo()
        cp = subprocess.CompletedProcess([], 0, stdout=GIT_OUT, stderr="")
        with mock.patch.object(verzia.subprocess, "run", return_value=cp) as run:
            g = verzia.git_commit(self.d, NOW)
        self.assertEqual(g, {"commit": "abc1234", "datum": "2026-11-22T10:00:00+01:00"})
        args, kw = run.call_args
        self.assertEqual(args[0], ["git", "-C", self.d, "log", "-1", "--format=%h|%cI"])
        self.assertEqual(kw["timeout"], verzia.GIT_TIMEOUT)

    def test_cache(self):
        self.make_repo()
        cp = subprocess.CompletedProcess([], 0, stdout=GIT_OUT, stderr="")
        with mock.patch.object(verzia.subprocess, "run", return_value=cp) as run:
            verzia.git_commit(self.d, NOW)
            verzia.git_commit(self.d, NOW + 60)
            self.assertEqual(run.call_count, 1)                       # v rámci TTL z cache
            verzia.git_commit(self.d, NOW + verzia.GIT_TTL + 1)
            self.assertEqual(run.call_count, 2)                       # po TTL znovu

    def test_no_git_binary_or_error(self):
        """git chýba, timeout, alebo git skončí chybou (napr. poškodený .git) = bez údaja, bez výnimky."""
        self.make_repo()
        cases = [dict(side_effect=FileNotFoundError("git")), dict(side_effect=subprocess.TimeoutExpired("git", 3)),
                 dict(return_value=subprocess.CompletedProcess([], 128, stdout="", stderr="fatal"))]
        for kw in cases:
            self._reset()
            with mock.patch.object(verzia.subprocess, "run", **kw):
                self.assertIsNone(verzia.git_commit(self.d, NOW))

    def test_timeout_is_short(self):
        self.assertLessEqual(verzia.GIT_TIMEOUT, 5)


class Latest(Base):
    CFG = {"UPDATE_CHECK": "1"}
    TAGS = [{"name": "v2.9.0"}, {"name": "v2.14.0"}, {"name": "v2.10.0"}, {"name": "nightly"}, {"name": "v2.14.0-rc1"}]

    def test_highest_semver_tag_and_request(self):
        with mock.patch.object(verzia.urllib.request, "urlopen", return_value=_resp(self.TAGS)) as uo:
            self.assertEqual(verzia.latest_tag(self.CFG, NOW), "v2.14.0")
        req, = uo.call_args[0]
        self.assertEqual(req.full_url, "https://api.github.com/repos/soso8782/samsung_heat_pump-ai-control/tags")
        self.assertEqual(req.get_header("User-agent"), "samsung-heat-pump-ai-control")
        self.assertEqual(uo.call_args[1]["timeout"], 5)

    def test_no_semver_tags(self):
        with mock.patch.object(verzia.urllib.request, "urlopen", return_value=_resp([{"name": "latest"}])):
            self.assertIsNone(verzia.latest_tag(self.CFG, NOW))

    def test_cache_6h(self):
        with mock.patch.object(verzia.urllib.request, "urlopen", return_value=_resp(self.TAGS)) as uo:
            verzia.latest_tag(self.CFG, NOW)
            verzia.latest_tag(self.CFG, NOW + 6 * 3600 - 1)
            self.assertEqual(uo.call_count, 1)
            verzia.latest_tag(self.CFG, NOW + 6 * 3600 + 1)
            self.assertEqual(uo.call_count, 2)

    def test_failure_cached_1h(self):
        err = urllib.error.URLError("offline")
        with mock.patch.object(verzia.urllib.request, "urlopen", side_effect=err) as uo:
            self.assertIsNone(verzia.latest_tag(self.CFG, NOW))
            self.assertIsNone(verzia.latest_tag(self.CFG, NOW + 1800))
            self.assertEqual(uo.call_count, 1)                        # chyba sa drží 1 h, nie 6 h
            self.assertIsNone(verzia.latest_tag(self.CFG, NOW + 3600 + 1))
            self.assertEqual(uo.call_count, 2)

    def test_bad_json_is_failure(self):
        cm = mock.MagicMock()
        cm.__enter__.return_value.read.return_value = b"<html>rate limit</html>"
        with mock.patch.object(verzia.urllib.request, "urlopen", return_value=cm):
            self.assertIsNone(verzia.latest_tag(self.CFG, NOW))
        self.assertEqual(verzia._cache["latest"]["ttl"], verzia.LATEST_FAIL_TTL)

    def test_update_check_off(self):
        for val in ("0", "false", "off"):
            self._reset()
            with mock.patch.object(verzia.urllib.request, "urlopen") as uo:
                self.assertIsNone(verzia.latest_tag({"UPDATE_CHECK": val}, NOW))
            uo.assert_not_called()
        self.assertFalse(verzia.update_check_on({"UPDATE_CHECK": "0"}))
        self.assertTrue(verzia.update_check_on({}))                   # predvolene zapnuté


class Info(Base):
    def info(self, latest, cfg=None, firmver=None):
        self._reset()
        verzia._cache["latest"].update(loaded=True, t=NOW, ttl=10 ** 9, v=latest)
        return verzia.info(cfg if cfg is not None else {"UPDATE_CHECK": "1"}, firmver, root=self.d, now=NOW)

    def test_newer_available(self):
        i = self.info("v2.15.0")
        self.assertTrue(i["novsia"]); self.assertEqual(i["najnovsia"], "v2.15.0"); self.assertEqual(i["app"], "2.14.0")

    def test_up_to_date_and_older_remote(self):
        for latest in ("v2.14.0", "v2.13.9"):
            i = self.info(latest)
            self.assertFalse(i["novsia"], latest); self.assertEqual(i["najnovsia"], latest)

    def test_unknown_latest(self):
        i = self.info(None)
        self.assertFalse(i["novsia"]); self.assertIsNone(i["najnovsia"]); self.assertTrue(i["kontrola"])

    def test_disabled_flag(self):
        self.assertFalse(self.info(None, cfg={"UPDATE_CHECK": "0"})["kontrola"])

    def test_firmver_and_no_git(self):
        fw = {"verzia": "2.12.0", "pozadovana": "2.12", "stary": False, "nedostupne": False, "hint": False}
        i = self.info("v2.14.0", firmver=fw)
        self.assertEqual(i["firmver"], fw)
        self.assertIsNone(i["commit"]); self.assertIsNone(i["datum"]); self.assertEqual(i["adresar"], self.d)

    def test_commit_in_info(self):
        self.make_repo()
        cp = subprocess.CompletedProcess([], 0, stdout=GIT_OUT, stderr="")
        with mock.patch.object(verzia.subprocess, "run", return_value=cp):
            i = self.info("v2.14.0")
        self.assertEqual((i["commit"], i["datum"]), ("abc1234", "2026-11-22T10:00:00+01:00"))


class ApiStatus(Base):
    """`app.status` (admin) a `app.status_user` (rola user): pole `verzia` v /api/status."""

    @classmethod
    def setUpClass(cls):
        import app
        cls.app = app

    def setUp(self):
        super().setUp()
        from common import DEFAULTS
        self.cfg = dict(DEFAULTS, AGENT_DB=os.path.join(self.d, "a.db"), TC_DB=os.path.join(self.d, "tc.db"),
                        STATS_DB=os.path.join(self.d, "stats.db"), HA_TOKEN="", OPENROUTER_API_KEY="",
                        HA_URL="http://ha.invalid:8123", ESP_URL="http://esp.invalid", UPDATE_CHECK="1")
        self.fw = {"verzia": "2.11.0", "pozadovana": "2.12", "stary": True, "nedostupne": False, "hint": True}
        self.patches = [mock.patch.object(verzia, "ROOT", self.d),
                        mock.patch.object(self.app, "firmver_info", return_value=self.fw),
                        mock.patch.object(self.app, "riadiaca_info", return_value=None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def test_admin_block(self):
        self._reset()
        verzia._cache["latest"].update(loaded=True, t=NOW, ttl=10 ** 9, v="v2.15.0")
        with mock.patch.object(verzia.time, "time", return_value=NOW), \
                mock.patch.object(verzia.urllib.request, "urlopen") as uo, \
                mock.patch.object(verzia.subprocess, "run") as run:
            st = self.app.status(self.cfg, "admin", None, "sk")
        uo.assert_not_called(); run.assert_not_called()               # cache aj bez .git: žiadne spojenie
        v = st["verzia"]
        self.assertEqual(set(v), {"app", "commit", "datum", "adresar", "firmver", "najnovsia", "novsia", "kontrola"})
        self.assertEqual(v["app"], "2.14.0"); self.assertIsNone(v["commit"])
        self.assertEqual(v["najnovsia"], "v2.15.0"); self.assertTrue(v["novsia"]); self.assertTrue(v["kontrola"])
        self.assertEqual(v["firmver"]["verzia"], "2.11.0"); self.assertTrue(v["firmver"]["stary"])
        self.assertEqual(v["firmver"]["pozadovana"], "2.12")

    def test_admin_block_with_git_and_update_off(self):
        self._reset()
        self.make_repo()
        self.cfg["UPDATE_CHECK"] = "0"
        cp = subprocess.CompletedProcess([], 0, stdout=GIT_OUT, stderr="")
        with mock.patch.object(verzia.subprocess, "run", return_value=cp), \
                mock.patch.object(verzia.urllib.request, "urlopen") as uo:
            v = self.app.status(self.cfg, "admin", None, "sk")["verzia"]
        uo.assert_not_called()
        self.assertEqual(v["commit"], "abc1234"); self.assertEqual(v["datum"], "2026-11-22T10:00:00+01:00")
        self.assertIsNone(v["najnovsia"]); self.assertFalse(v["novsia"]); self.assertFalse(v["kontrola"])

    def test_user_gets_only_app_version(self):
        with mock.patch.object(verzia.urllib.request, "urlopen") as uo:
            st = self.app.status(self.cfg, "user", "u@x", "sk")
        uo.assert_not_called()
        self.assertEqual(st["verzia"], {"app": "2.14.0"})
        self.assertNotIn("firmver", st)


if __name__ == "__main__":
    unittest.main()
