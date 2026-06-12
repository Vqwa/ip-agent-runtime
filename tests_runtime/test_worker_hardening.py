"""Worker-cluster hardening tests (H22/H23/H24/H25/H26) for turn_worker.py.

Stdlib-only; never imports run_agent — turn_worker defers that import until
after the guards under test, so every test exercises pure helpers or the
pre-import refusal paths of run_turn()/main().
"""

import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

# Import-order contract: HERMES_HOME + the platform key must exist BEFORE the
# module snapshot (_PARENT_ENV_SECRETS) is taken at turn_worker import time.
_TEST_HOME = tempfile.mkdtemp(prefix="turn-test-")
os.environ["HERMES_HOME"] = _TEST_HOME
E2B_KEY = "e2b_platform_key_abc123456"
os.environ["E2B_API_KEY"] = E2B_KEY

import turn_worker  # noqa: E402

ALLOWED_ENDPOINT = "https://main.insightfulmcp.com/"


class WorkerEnvIsolation(unittest.TestCase):
    """Base: deterministic env + injected-state isolation per test."""

    def setUp(self):
        # Purge credential-shaped vars the dev shell may carry (the containment
        # assert scans os.environ); restore everything in tearDown.
        self._purged = {}
        for k in list(os.environ):
            if turn_worker._SECRET_ENV_NAME_RE.search(k):
                self._purged[k] = os.environ.pop(k)
        self._saved = {
            k: os.environ.get(k) for k in ("TERMINAL_ENV", "RUNTIME_MCP_ENDPOINT_ALLOWLIST")
        }
        os.environ["TERMINAL_ENV"] = "e2b"
        os.environ.pop("RUNTIME_MCP_ENDPOINT_ALLOWLIST", None)
        self._injected_orig = dict(turn_worker._INJECTED_ENV)
        turn_worker._INJECTED_ENV.clear()
        self._parent_orig = turn_worker._PARENT_ENV_SECRETS
        turn_worker._PARENT_ENV_SECRETS = frozenset({E2B_KEY})

    def tearDown(self):
        turn_worker._clear_injected_env()
        turn_worker._INJECTED_ENV.clear()
        turn_worker._INJECTED_ENV.update(self._injected_orig)
        turn_worker._PARENT_ENV_SECRETS = self._parent_orig
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for k, v in self._purged.items():
            os.environ[k] = v


class CollectSecretValuesTests(WorkerEnvIsolation):
    def test_pulls_every_nested_key_shape(self):
        req = {
            "turn_id": "t-1",
            "message": {"text": "hello world, not a secret"},
            "config": {
                "model": "gpt-x",
                "llm": {"provider": "openai", "api_key": "sk-llm-key-111111"},
                "mcp": {"endpoint": ALLOWED_ENDPOINT, "token": "ip_sk_aaaa1111"},
                "web": {"backend": "oxylabs", "api_key": "oxy-key-222222"},
                "browser": {
                    "provider": "browserbase",
                    "api_key": json.dumps({"api_key": "bb-key-333333", "project_id": "bb-proj-444444"}),
                },
                "image_gen": {"provider": "fal", "api_key": "fal-key-555555"},
                "sandbox": {"provider": "modal", "token_id": "mod-id-666666", "token_secret": "mod-sec-777777"},
                "vision_model": {"model": "vis", "api_key": "vis-key-888888"},
                "extras": [{"deep": {"signing_secret": "deep-secret-999999"}}],
                "auth": {"key": "bare-key-000000", "password": "pw-value-121212", "bearer": "bear-value-131313"},
            },
        }
        got = turn_worker._collect_secret_values(req)
        for s in (
            "sk-llm-key-111111",
            "ip_sk_aaaa1111",
            "oxy-key-222222",
            "bb-key-333333",
            "bb-proj-444444",  # inner value of the JSON-packed browserbase blob
            "fal-key-555555",
            "mod-id-666666",
            "mod-sec-777777",
            "vis-key-888888",
            "deep-secret-999999",
            "bare-key-000000",
            "pw-value-121212",
            "bear-value-131313",
        ):
            self.assertIn(s, got, f"missing secret value: {s}")
        # Non-secret values are never collected.
        self.assertNotIn("hello world, not a secret", got)
        self.assertNotIn("openai", got)
        self.assertNotIn("browserbase", got)

    def test_scrub_set_unions_request_and_parent_env(self):
        req = {"config": {"llm": {"api_key": "sk-req-key-424242"}}}
        msg = f"boom: {E2B_KEY} and sk-req-key-424242 leaked"
        scrubbed = turn_worker._scrub_by_value(msg, turn_worker._scrub_set(req))
        self.assertNotIn(E2B_KEY, scrubbed)
        self.assertNotIn("sk-req-key-424242", scrubbed)
        self.assertIn("[REDACTED]", scrubbed)


class McpEndpointPinTests(WorkerEnvIsolation):
    def test_default_allows_main_with_and_without_slash(self):
        turn_worker._check_mcp_endpoint({"endpoint": "https://main.insightfulmcp.com/"})
        turn_worker._check_mcp_endpoint({"endpoint": "https://main.insightfulmcp.com"})

    def test_missing_mcp_or_endpoint_is_allowed(self):
        turn_worker._check_mcp_endpoint(None)
        turn_worker._check_mcp_endpoint({})
        turn_worker._check_mcp_endpoint({"endpoint": ""})

    def test_off_allowlist_endpoint_refused(self):
        with self.assertRaises(turn_worker.TurnRefused) as cm:
            turn_worker._check_mcp_endpoint({"endpoint": "https://evil.example.com/"})
        self.assertEqual(cm.exception.error_type, "mcp_endpoint_refused")

    def test_prefix_tricks_refused_exact_match_only(self):
        for url in (
            "https://main.insightfulmcp.com.evil.com/",
            "https://main.insightfulmcp.com/extra/path",
            "http://main.insightfulmcp.com/",
        ):
            with self.assertRaises(turn_worker.TurnRefused, msg=url):
                turn_worker._check_mcp_endpoint({"endpoint": url})

    def test_env_override_comma_separated_and_normalized(self):
        os.environ["RUNTIME_MCP_ENDPOINT_ALLOWLIST"] = "https://a.example.com/, https://b.example.com"
        turn_worker._check_mcp_endpoint({"endpoint": "https://a.example.com"})
        turn_worker._check_mcp_endpoint({"endpoint": "https://b.example.com/"})
        with self.assertRaises(turn_worker.TurnRefused):
            turn_worker._check_mcp_endpoint({"endpoint": "https://main.insightfulmcp.com/"})

    def test_refusal_happens_before_config_yaml_is_written(self):
        config_path = os.path.join(turn_worker._HOME, "config.yaml")
        if os.path.exists(config_path):
            os.unlink(config_path)
        req = {"turn_id": "t-2", "config": {"mcp": {"endpoint": "https://evil.example.com/", "token": "tok-123456"}}}
        with self.assertRaises(turn_worker.TurnRefused):
            turn_worker.run_turn(req)
        self.assertFalse(os.path.exists(config_path), "config.yaml must not exist after a refused endpoint")


class ErrorPathFramingTests(WorkerEnvIsolation):
    def _run_main(self, req: dict) -> tuple[str, str]:
        out, err = io.StringIO(), io.StringIO()
        orig_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(req))
        try:
            with redirect_stdout(out), redirect_stderr(err):
                turn_worker.main()
        finally:
            sys.stdin = orig_stdin
        return out.getvalue(), err.getvalue()

    def test_error_path_emits_sentinel_then_one_scrubbed_json_line(self):
        token = "ip_sk_deadbeef123456"
        req = {
            "turn_id": "t-9",
            "config": {"mcp": {"endpoint": f"https://evil.example.com/?t={token}", "token": token}},
        }
        stdout, stderr = self._run_main(req)
        lines = stdout.splitlines()
        self.assertGreaterEqual(len(lines), 2)
        self.assertEqual(lines[-2], turn_worker.RESULT_SENTINEL, "sentinel must be the penultimate line")
        resp = json.loads(lines[-1])
        self.assertEqual(resp["status"], "error")
        self.assertEqual(resp["turn_id"], "t-9")
        self.assertEqual(resp["error"]["type"], "mcp_endpoint_refused")
        self.assertIn("wall_ms", resp["usage"])
        # H23: by-value scrub on error.message AND the worker's stderr line.
        self.assertNotIn(token, resp["error"]["message"])
        self.assertIn("[REDACTED]", resp["error"]["message"])
        self.assertNotIn(token, stderr)

    def test_unparseable_request_still_emits_framed_error(self):
        out, err = io.StringIO(), io.StringIO()
        orig_stdin = sys.stdin
        sys.stdin = io.StringIO("this is not json")
        try:
            with redirect_stdout(out), redirect_stderr(err):
                turn_worker.main()
        finally:
            sys.stdin = orig_stdin
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[-2], turn_worker.RESULT_SENTINEL)
        resp = json.loads(lines[-1])
        self.assertEqual(resp["status"], "error")
        self.assertIsNone(resp["turn_id"])


class ConfigFileModeTests(WorkerEnvIsolation):
    def test_config_yaml_written_owner_only_0600(self):
        req = {"config": {"mcp": {"endpoint": ALLOWED_ENDPOINT, "token": "tok-abcdef123"}}, "memory": {}}
        turn_worker._materialize_home(req)
        config_path = os.path.join(turn_worker._HOME, "config.yaml")
        mode = stat.S_IMODE(os.stat(config_path).st_mode)
        self.assertEqual(mode, 0o600, f"config.yaml mode is {oct(mode)}, expected 0o600")
        with open(config_path) as f:
            self.assertIn("tok-abcdef123", f.read())  # bearer present, but owner-only


class SandboxEnvContainmentTests(WorkerEnvIsolation):
    def test_platform_e2b_path_allows_platform_key_and_recorded_byok(self):
        os.environ["E2B_API_KEY"] = E2B_KEY
        turn_worker._assert_sandbox_env_contained()
        turn_worker._inject_env("OXYLABS_API_KEY", "oxy-key-123456")
        turn_worker._assert_sandbox_env_contained()

    def test_platform_e2b_path_refuses_unrecorded_api_key(self):
        os.environ["E2B_API_KEY"] = E2B_KEY
        os.environ["EVIL_API_KEY"] = "evil-key-123456"
        try:
            with self.assertRaises(turn_worker.TurnRefused) as cm:
                turn_worker._assert_sandbox_env_contained()
        finally:
            os.environ.pop("EVIL_API_KEY", None)
        self.assertEqual(cm.exception.error_type, "env_key_leak")
        self.assertIn("EVIL_API_KEY", str(cm.exception))
        self.assertNotIn("evil-key-123456", str(cm.exception))  # names only, never values

    def test_byok_path_requires_platform_key_scrubbed(self):
        os.environ["TERMINAL_ENV"] = "modal"
        turn_worker._inject_env("MODAL_TOKEN_ID", "mod-id-123456")
        turn_worker._inject_env("MODAL_TOKEN_SECRET", "mod-sec-123456")
        os.environ["E2B_API_KEY"] = E2B_KEY  # simulate a failed platform scrub
        with self.assertRaises(turn_worker.TurnRefused):
            turn_worker._assert_sandbox_env_contained()
        os.environ.pop("E2B_API_KEY")
        turn_worker._assert_sandbox_env_contained()

    def test_injected_env_cleared_but_values_stay_in_scrub_set(self):
        turn_worker._inject_env("OXYLABS_API_KEY", "oxy-key-654321")
        self.assertEqual(os.environ.get("OXYLABS_API_KEY"), "oxy-key-654321")
        turn_worker._clear_injected_env()
        self.assertNotIn("OXYLABS_API_KEY", os.environ)
        self.assertIn("oxy-key-654321", turn_worker._scrub_set(None))


if __name__ == "__main__":
    unittest.main()
