"""Cluster E hardening tests — H27 E2B sandbox lifecycle + H41 oxylabs SSRF gate.

Runs without the e2b / oxylabs SDKs and without network: the SDK surfaces (and
tools.lazy_deps / tools.url_safety, for determinism) are stubbed into
sys.modules before the modules under test lazy-import them. Run from the repo
root with `python -m unittest discover -s tests_runtime`.
"""

import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# --- fake e2b SDK surface ----------------------------------------------------
class _HybridKill:
    """Mimic the e2b SDK's dual kill surface: Sandbox.kill(id) and sbx.kill()."""

    def __get__(self, obj, objtype=None):
        if obj is None:
            def _kill_by_id(sandbox_id, **kwargs):
                if objtype.kill_by_id_error is not None:
                    raise objtype.kill_by_id_error
                objtype.killed_by_id.append(sandbox_id)
                return True

            return _kill_by_id

        def _kill_instance(**kwargs):
            if type(obj).instance_kill_error is not None:
                raise type(obj).instance_kill_error
            obj.killed = True
            return True

        return _kill_instance


class FakeSandbox:
    created_kwargs = None
    killed_by_id = []
    kill_by_id_error = None
    instance_kill_error = None

    kill = _HybridKill()

    def __init__(self):
        self.sandbox_id = "sbx-fake-1"
        self.killed = False
        self.commands = types.SimpleNamespace(
            run=lambda cmd, timeout=None: types.SimpleNamespace(stdout="", stderr="", exit_code=0)
        )

    @classmethod
    def create(cls, **kwargs):
        cls.created_kwargs = kwargs
        return cls()

    @classmethod
    def reset(cls):
        cls.created_kwargs = None
        cls.killed_by_id = []
        cls.kill_by_id_error = None
        cls.instance_kill_error = None


_fake_e2b = types.ModuleType("e2b")
_fake_e2b.Sandbox = FakeSandbox
sys.modules["e2b"] = _fake_e2b

# lazy_deps would otherwise probe importlib.metadata for the (absent) e2b dist.
_fake_lazy = types.ModuleType("tools.lazy_deps")
_fake_lazy.ensure = lambda feature, prompt=True: None
sys.modules["tools.lazy_deps"] = _fake_lazy

from tools.environments import e2b as e2b_env  # noqa: E402


class E2BReaperTests(unittest.TestCase):
    """H27: sandbox-id breadcrumb, lifetime clamp, and the kill_sandbox reaper."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="test-home-")
        self._old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = self.home
        FakeSandbox.reset()

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._old_home

    def _id_file(self):
        return os.path.join(self.home, e2b_env.SANDBOX_ID_FILENAME)

    def _recorded_ids(self):
        try:
            with open(self._id_file()) as f:
                return [ln for ln in f.read().splitlines() if ln]
        except OSError:
            return []

    def test_sandbox_id_file_written_on_create(self):
        env = e2b_env.E2BEnvironment()
        self.assertEqual(self._recorded_ids(), ["sbx-fake-1"])
        env.cleanup()

    def test_lifetime_clamped_to_wall_budget(self):
        self.assertLessEqual(e2b_env.MAX_SANDBOX_LIFETIME, 300)
        env = e2b_env.E2BEnvironment(sandbox_lifetime=9999)
        self.assertLessEqual(FakeSandbox.created_kwargs["timeout"], 300)
        # The no-egress guardrail must survive untouched alongside the clamp.
        self.assertFalse(FakeSandbox.created_kwargs["allow_internet_access"])
        env.cleanup()

    def test_cleanup_unrecords_sandbox_id(self):
        env = e2b_env.E2BEnvironment()
        env.cleanup()
        self.assertEqual(self._recorded_ids(), [])
        self.assertFalse(os.path.exists(self._id_file()))

    def test_failed_cleanup_keeps_id_for_parent_reaper(self):
        env = e2b_env.E2BEnvironment()
        FakeSandbox.instance_kill_error = RuntimeError("e2b api down")
        env.cleanup()
        self.assertEqual(self._recorded_ids(), ["sbx-fake-1"])

    def test_kill_sandbox_reaps_by_id(self):
        self.assertTrue(e2b_env.kill_sandbox("sbx-orphan-7"))
        self.assertEqual(FakeSandbox.killed_by_id, ["sbx-orphan-7"])

    def test_kill_sandbox_failure_returns_false(self):
        FakeSandbox.kill_by_id_error = RuntimeError("already gone")
        self.assertFalse(e2b_env.kill_sandbox("sbx-orphan-8"))

    def test_kill_sandbox_empty_id_is_noop(self):
        self.assertFalse(e2b_env.kill_sandbox(""))
        self.assertEqual(FakeSandbox.killed_by_id, [])

    def test_no_home_env_degrades_without_error(self):
        os.environ.pop("HERMES_HOME", None)
        env = e2b_env.E2BEnvironment()  # record is a no-op, never a crash
        env.cleanup()


class OxylabsSsrfGateTests(unittest.TestCase):
    """H41: the fork-owned oxylabs extract loop re-checks url_safety per URL."""

    BLOCKED = "http://169.254.169.254/latest/meta-data/"
    ALLOWED = "https://example.com/page"

    def setUp(self):
        self._saved = {
            name: sys.modules.get(name)
            for name in ("tools.url_safety", "oxylabs_ai_studio", "oxylabs_ai_studio.apps",
                         "oxylabs_ai_studio.apps.ai_scraper")
        }
        fake_safety = types.ModuleType("tools.url_safety")
        fake_safety.is_safe_url = lambda url: "169.254." not in url
        sys.modules["tools.url_safety"] = fake_safety

        tests = self

        class FakeAiScraper:
            def __init__(self, api_key):
                tests.scraper_key = api_key

            def scrape(self, url, output_format=None, render_javascript=None):
                tests.scraped.append(url)
                return {"content": "ok-content", "title": "ok-title"}

        self.scraped = []
        self.scraper_key = None
        pkg = types.ModuleType("oxylabs_ai_studio")
        apps = types.ModuleType("oxylabs_ai_studio.apps")
        scraper_mod = types.ModuleType("oxylabs_ai_studio.apps.ai_scraper")
        scraper_mod.AiScraper = FakeAiScraper
        sys.modules["oxylabs_ai_studio"] = pkg
        sys.modules["oxylabs_ai_studio.apps"] = apps
        sys.modules["oxylabs_ai_studio.apps.ai_scraper"] = scraper_mod

        self._old_key = os.environ.get("OXYLABS_API_KEY")
        os.environ["OXYLABS_API_KEY"] = "test-oxy-key"

    def tearDown(self):
        for name, mod in self._saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        if self._old_key is None:
            os.environ.pop("OXYLABS_API_KEY", None)
        else:
            os.environ["OXYLABS_API_KEY"] = self._old_key

    def test_blocked_url_never_reaches_the_scraper(self):
        from plugins.web.oxylabs.provider import OxylabsWebSearchProvider

        rows = OxylabsWebSearchProvider().extract([self.BLOCKED, self.ALLOWED])
        self.assertEqual(len(rows), 2)
        self.assertIn("Blocked", rows[0]["error"])
        self.assertEqual(rows[1]["content"], "ok-content")
        self.assertEqual(self.scraped, [self.ALLOWED])


if __name__ == "__main__":
    unittest.main()
