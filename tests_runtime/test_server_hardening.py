"""Cluster S hardening tests for server.py — H12 (iat/age), H20 (jti dedup),
H21b (mcp_token_sha256 verify-if-present), H24 parent framing, H23 value scrub.

Parent-process logic only: no subprocess, no Hermes imports. Run from the repo
root with `python -m unittest discover -s tests_runtime`.
"""

import hashlib
import io
import json
import os
import sys
import time
import unittest
import uuid
from contextlib import redirect_stderr
from unittest import mock

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_PEM = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
_PUBLIC_PEM = _KEY.public_key().public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()

# server.py reads the public key from env at import time.
os.environ["RUNTIME_JWT_PUBLIC_KEY"] = _PUBLIC_PEM

import server  # noqa: E402


def _claims(**overrides) -> dict:
    now = int(time.time())
    claims = {
        "iss": server.JWT_ISS,
        "aud": server.JWT_AUD,
        "iat": now,
        "exp": now + 30,
        "jti": uuid.uuid4().hex,
        "agent_id": "agent-1",
        "workspace_id": "ws-1",
        "turn_id": "turn-1",
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}  # None = drop the claim


def _token(**overrides) -> str:
    return "Bearer " + jwt.encode(_claims(**overrides), _PRIVATE_PEM, algorithm="RS256")


def _body(**extra) -> dict:
    body = {"agent_id": "agent-1", "workspace_id": "ws-1", "turn_id": "turn-1", "config": {}}
    body.update(extra)
    return body


def _verify(authorization: str, body: dict) -> str:
    """Run _verify_jwt with stderr captured; returns what it logged."""
    buf = io.StringIO()
    with redirect_stderr(buf):
        server._verify_jwt(authorization, body)
    return buf.getvalue()


class JwtAgeTests(unittest.TestCase):
    """H12: iat required + exp - iat bounded at 35s."""

    def setUp(self):
        server._seen_jtis.clear()

    def test_iat_is_required(self):
        with self.assertRaises(HTTPException) as ctx:
            _verify(_token(iat=None), _body())
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("iat", ctx.exception.detail)

    def test_over_age_token_rejected(self):
        now = int(time.time())
        with self.assertRaises(HTTPException) as ctx:
            _verify(_token(iat=now, exp=now + 60), _body())
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, "token age exceeds bound")

    def test_age_at_bound_accepted(self):
        now = int(time.time())
        _verify(_token(iat=now, exp=now + 35), _body())  # exactly 35s: allowed


class JtiReplayTests(unittest.TestCase):
    """H20: in-process jti dedup with per-call pruning."""

    def setUp(self):
        server._seen_jtis.clear()

    def test_replayed_jti_rejected(self):
        tok = _token()
        _verify(tok, _body())
        with self.assertRaises(HTTPException) as ctx:
            _verify(tok, _body())
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, "jti already used")

    def test_expired_jti_entry_is_pruned(self):
        server._seen_jtis["stale-jti"] = time.time() - 10
        _verify(_token(jti="stale-jti"), _body())  # stale entry pruned, so accepted
        self.assertGreater(server._seen_jtis["stale-jti"], time.time())

    def test_unexpired_entries_survive_pruning(self):
        live_exp = time.time() + 300
        server._seen_jtis["live-jti"] = live_exp
        _verify(_token(), _body())
        self.assertEqual(server._seen_jtis["live-jti"], live_exp)

    def test_rejected_request_does_not_burn_jti(self):
        now = int(time.time())
        tok = _token(jti="kept-jti", iat=now, exp=now + 60)  # fails the age bound
        with self.assertRaises(HTTPException):
            _verify(tok, _body())
        self.assertNotIn("kept-jti", server._seen_jtis)


class McpTokenBindingTests(unittest.TestCase):
    """H21b: verify mcp_token_sha256 when present; allow + log when absent."""

    def setUp(self):
        server._seen_jtis.clear()

    def test_hash_mismatch_rejected(self):
        claim = hashlib.sha256(b"ip_sk_the_signed_one").hexdigest()
        body = _body(config={"mcp": {"endpoint": "https://mcp.example", "token": "ip_sk_a_swapped_one"}})
        with self.assertRaises(HTTPException) as ctx:
            _verify(_token(mcp_token_sha256=claim), body)
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, "mcp_token_sha256 mismatch")

    def test_hash_match_accepted(self):
        token_val = "ip_sk_" + "a" * 32
        claim = hashlib.sha256(token_val.encode()).hexdigest()
        _verify(_token(mcp_token_sha256=claim), _body(config={"mcp": {"endpoint": "https://mcp.example", "token": token_val}}))

    def test_absent_claim_allowed_and_logged(self):
        log = _verify(_token(), _body())
        self.assertIn("lacks mcp_token_sha256", log)

    def test_claim_present_with_no_body_token_compares_against_empty(self):
        _verify(_token(mcp_token_sha256=hashlib.sha256(b"").hexdigest()), _body(config={}))


class FramedResultTests(unittest.TestCase):
    """H24 parent half: result = the line after the LAST sentinel line."""

    def test_framed_result_parsed_despite_noisy_stdout(self):
        out = (
            b"some library printed this\n"
            b'{"status": "decoy", "turn_id": "spoof"}\n'
            b"===TURN_RESULT_v1===\n"
            b'{"status": "ok", "turn_id": "turn-1"}\n'
        )
        self.assertEqual(server._extract_framed_result(out)["status"], "ok")

    def test_last_sentinel_wins(self):
        out = (
            b"===TURN_RESULT_v1===\n"
            b'{"status": "first"}\n'
            b"===TURN_RESULT_v1===\n"
            b'{"status": "second"}\n'
        )
        self.assertEqual(server._extract_framed_result(out)["status"], "second")

    def test_unframed_output_is_502(self):
        with self.assertRaises(HTTPException) as ctx:
            server._extract_framed_result(b'{"status": "ok", "turn_id": "turn-1"}\n')
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(ctx.exception.detail, "child produced no framed result")

    def test_sentinel_with_no_result_line_is_502(self):
        with self.assertRaises(HTTPException) as ctx:
            server._extract_framed_result(b"===TURN_RESULT_v1===\n")
        self.assertEqual(ctx.exception.status_code, 502)

    def test_result_roundtrips_json(self):
        payload = {"status": "ok", "turn_id": "turn-1", "reply_text": "hi"}
        out = f"noise\n{server.RESULT_SENTINEL}\n{json.dumps(payload)}\n".encode()
        self.assertEqual(server._extract_framed_result(out), payload)


class ValueScrubTests(unittest.TestCase):
    """H23 parent half: scrub stderr by VALUE (request secrets + parent-env E2B key)."""

    def test_request_borne_key_the_regex_misses_is_redacted(self):
        oxylabs = "OXY_0123456789abcdef"  # no sk-/ip_sk_/Bearer shape
        body = _body(config={"web": {"backend": "oxylabs", "api_key": oxylabs}})
        scrubbed = server._scrub(f"oxylabs 401 for key {oxylabs}".encode(), server._collect_secret_values(body))
        self.assertNotIn(oxylabs, scrubbed)
        self.assertIn("[REDACTED]", scrubbed)

    def test_parent_env_e2b_key_redacted(self):
        e2b = "e2b_0123456789abcdef"
        with mock.patch.dict(os.environ, {"E2B_API_KEY": e2b}):
            secrets = server._collect_secret_values(_body())
        scrubbed = server._scrub(f"E2B sandbox boot failed for {e2b}".encode(), secrets)
        self.assertNotIn(e2b, scrubbed)

    def test_nested_sandbox_and_vision_secrets_collected(self):
        body = _body(
            config={
                "sandbox": {"provider": "modal", "token_id": "ak-0123456789", "token_secret": "as-0123456789"},
                "vision_model": {"model": "m", "api_key": "vk_0123456789"},
            }
        )
        secrets = server._collect_secret_values(body)
        self.assertIn("ak-0123456789", secrets)
        self.assertIn("as-0123456789", secrets)
        self.assertIn("vk_0123456789", secrets)

    def test_llm_and_mcp_secrets_collected(self):
        body = _body(
            config={
                "llm": {"provider": "openai", "api_key": "plainkey-0123456789"},
                "mcp": {"endpoint": "https://mcp.example", "token": "ip_sk_aaaabbbbcccc"},
            }
        )
        secrets = server._collect_secret_values(body)
        self.assertIn("plainkey-0123456789", secrets)
        self.assertIn("ip_sk_aaaabbbbcccc", secrets)

    def test_short_values_are_not_collected(self):
        secrets = server._collect_secret_values(_body(config={"llm": {"api_key": "short"}}))
        self.assertNotIn("short", secrets)

    def test_regex_layer_still_applies_as_second_pass(self):
        scrubbed = server._scrub(b"leaked sk-abcdefghij1234567890", set())
        self.assertNotIn("sk-abcdefghij1234567890", scrubbed)


if __name__ == "__main__":
    unittest.main()


class ChildEnvPassthroughTests(unittest.TestCase):
    """Review blocker: policy config must cross the parent->worker process boundary."""

    def test_mcp_allowlist_is_forwarded_to_the_worker(self):
        with mock.patch.dict(os.environ, {"RUNTIME_MCP_ENDPOINT_ALLOWLIST": "https://x.example/"}):
            env = server._child_env("/tmp/h")
        self.assertEqual(env.get("RUNTIME_MCP_ENDPOINT_ALLOWLIST"), "https://x.example/")

    def test_reaper_ignores_malformed_ids_and_caps_the_count(self):
        import tempfile

        home = tempfile.mkdtemp(prefix="reap-")
        with open(os.path.join(home, ".sandbox_id"), "w") as fh:
            fh.write("../../etc/passwd\nok-id-123456\n" + "\n".join(f"id-{i:060d}x" for i in range(10)) + "\n")
        killed = []
        fake_mod = mock.Mock()
        fake_mod.kill_sandbox.side_effect = lambda sid: killed.append(sid)
        with mock.patch.dict(sys.modules, {"tools.environments.e2b": fake_mod, "tools.environments": mock.Mock(e2b=fake_mod), "tools": mock.Mock()}):
            server._reap_orphaned_sandboxes(home)
        self.assertIn("ok-id-123456", killed)
        self.assertNotIn("../../etc/passwd", killed)
        self.assertLessEqual(len(killed), 4)
