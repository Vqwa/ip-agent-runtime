"""H30 tests for server.py — kid-keyed verifier set with kid-less back-compat.

Parent-process logic only: no subprocess, no Hermes imports. Run from the repo
root with `python -m unittest discover -s tests_runtime`.
"""

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


def _keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


_ENV_PRIVATE, _ENV_PUBLIC = _keypair()  # the kid-less RUNTIME_JWT_PUBLIC_KEY signer
_KMS_PRIVATE, _KMS_PUBLIC = _keypair()  # a rotated key the verifier knows by kid
_KID = "projects/p/locations/l/keyRings/agent-jwt/cryptoKeys/turn-signer/cryptoKeyVersions/1"

# server.py reads the single public key from env at import time; the kid set is
# patched onto the module per test (import-order-safe vs test_server_hardening).
os.environ.setdefault("RUNTIME_JWT_PUBLIC_KEY", _ENV_PUBLIC)

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
    return claims


def _token(private_pem: str, kid: str | None = None, **overrides) -> str:
    headers = {"kid": kid} if kid is not None else None
    return "Bearer " + jwt.encode(_claims(**overrides), private_pem, algorithm="RS256", headers=headers)


def _body() -> dict:
    return {"agent_id": "agent-1", "workspace_id": "ws-1", "turn_id": "turn-1", "config": {}}


def _verify(authorization: str) -> None:
    with redirect_stderr(io.StringIO()):  # absent-mcp-claim log is expected noise here
        server._verify_jwt(authorization, _body())


class KidResolutionTests(unittest.TestCase):
    """H30: kid in set -> that PEM; no kid -> the single env key; unknown kid -> 401."""

    def setUp(self):
        server._seen_jtis.clear()
        for name, value in (("JWT_PUBLIC_KEY", _ENV_PUBLIC), ("JWT_PUBLIC_KEYS", {_KID: _KMS_PUBLIC})):
            patcher = mock.patch.object(server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_kid_in_set_verifies(self):
        _verify(_token(_KMS_PRIVATE, kid=_KID))

    def test_unknown_kid_is_401(self):
        with self.assertRaises(HTTPException) as ctx:
            _verify(_token(_KMS_PRIVATE, kid="not-a-known-kid"))
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, "unknown kid")

    def test_kid_with_empty_set_is_401(self):
        with mock.patch.object(server, "JWT_PUBLIC_KEYS", {}):
            with self.assertRaises(HTTPException) as ctx:
                _verify(_token(_KMS_PRIVATE, kid=_KID))
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, "unknown kid")

    def test_kidless_token_falls_back_to_the_single_env_key(self):
        _verify(_token(_ENV_PRIVATE))

    def test_kidless_token_signed_with_the_set_key_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            _verify(_token(_KMS_PRIVATE))  # no kid -> verified against the env key
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("invalid token", ctx.exception.detail)

    def test_known_kid_with_wrong_signing_key_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            _verify(_token(_ENV_PRIVATE, kid=_KID))  # kid pins the KMS PEM
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("invalid token", ctx.exception.detail)

    def test_garbage_bearer_token_is_401_not_500(self):
        with self.assertRaises(HTTPException) as ctx:
            _verify("Bearer not-a-jwt")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_rotation_overlap_both_signers_verify(self):
        _verify(_token(_ENV_PRIVATE))  # old signer still live
        _verify(_token(_KMS_PRIVATE, kid=_KID))  # new signer already trusted


class PublicKeysJsonParserTests(unittest.TestCase):
    """The env is parsed ONCE at import — bad JSON must raise loudly, not at turn time."""

    def test_empty_env_is_an_empty_set(self):
        self.assertEqual(server._parse_public_keys_json(""), {})

    def test_valid_object_round_trips(self):
        raw = json.dumps({_KID: _KMS_PUBLIC})
        self.assertEqual(server._parse_public_keys_json(raw), {_KID: _KMS_PUBLIC})

    def test_bad_json_raises(self):
        with self.assertRaises(ValueError):
            server._parse_public_keys_json("{not json")

    def test_non_object_json_raises(self):
        with self.assertRaises(ValueError):
            server._parse_public_keys_json('["just", "a", "list"]')

    def test_non_string_pem_value_raises(self):
        with self.assertRaises(ValueError):
            server._parse_public_keys_json('{"kid-1": 5}')

    def test_empty_kid_or_pem_raises(self):
        with self.assertRaises(ValueError):
            server._parse_public_keys_json('{"": "pem"}')
        with self.assertRaises(ValueError):
            server._parse_public_keys_json('{"kid-1": ""}')

    def test_module_wires_the_parser_at_import(self):
        # RUNTIME_JWT_PUBLIC_KEYS_JSON is unset in tests -> the parsed set is a dict.
        self.assertIsInstance(server.JWT_PUBLIC_KEYS, dict)


if __name__ == "__main__":
    unittest.main()
