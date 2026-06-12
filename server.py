"""FastAPI parent for the InsightfulPipe Hosted Agents runtime.

Verifies the per-turn RS256 JWT (baked-in PUBLIC key only), then runs the turn
in a FRESH SUBPROCESS per request with:
  * a unique ephemeral HERMES_HOME set BEFORE the child interpreter starts,
  * a MINIMAL env (NO tenant secrets — those go to the child via stdin only),
  * a hard wall-clock deadline enforced by a process-group SIGTERM -> SIGKILL,
  * stdout parsed via the ===TURN_RESULT_v1=== frame + schema-validated as the
    result; stderr captured, value-scrubbed (request secrets + E2B key), capped.

Contract: docs/hosted_agents/TURN_CONTRACT.md. Auth/§4.1 + child-boundary/§5/§2.2 of PLAN.
GCP IAM (Cloud Run invoker = the Django SA only) is the second, independent control.
"""

import hashlib
import hmac
import json
import os
import signal
import subprocess
import sys
import tempfile
import time

import jwt
from fastapi import FastAPI, Header, HTTPException, Request

app = FastAPI()

# --- config (baked into the image / runtime env; NOT per-tenant) -------------
WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "turn_worker.py")
JWT_PUBLIC_KEY = os.environ["RUNTIME_JWT_PUBLIC_KEY"]  # PEM; baked in, never a network JWKS
JWT_ISS = os.environ.get("RUNTIME_JWT_ISS", "insightfulpipe")
JWT_AUD = os.environ.get("RUNTIME_JWT_AUD", "ip-agent-runtime")
MAX_WALL_SECONDS = int(os.environ.get("RUNTIME_MAX_WALL_SECONDS", "300"))
MAX_STDOUT_BYTES = int(os.environ.get("RUNTIME_MAX_STDOUT_BYTES", str(8 * 1024 * 1024)))
# Local/dev escape hatch ONLY — never set in prod. Gates the no-IAM single-gate
# fallback and the off-registry base_url passthrough (both unsafe under real IAM).
LOCAL_NO_IAM = os.environ.get("RUNTIME_ALLOW_LOCAL_NO_IAM") == "1"
# E2B_API_KEY + E2B_TEMPLATE are read from this process's env and passed through.

# H24 framing contract: the worker's result is the line AFTER the last sentinel line.
RESULT_SENTINEL = "===TURN_RESULT_v1==="
# H20 jti replay guard: {jti: exp_epoch}, pruned per call. containerConcurrency=1 makes a
# plain dict safe (no lock); residual: cross-instance replay stays IAM-bounded (H40).
_seen_jtis: dict[str, float] = {}


def _verify_jwt(authorization: str, body: dict) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization[len("Bearer "):]
    try:
        claims = jwt.decode(
            token,
            JWT_PUBLIC_KEY,
            algorithms=["RS256"],            # reject alg=none / HMAC
            audience=JWT_AUD,
            issuer=JWT_ISS,
            leeway=5,                        # small clock skew
            options={"require": ["exp", "iss", "aud", "jti", "iat"]},
        )
    except jwt.InvalidTokenError as e:
        raise HTTPException(401, f"invalid token: {e}")
    # Claims must match the body — a forged/mismatched payload can't drive a
    # different tenant's turn.
    for k in ("agent_id", "workspace_id", "turn_id"):
        if claims.get(k) != body.get(k):
            raise HTTPException(401, f"claim/body mismatch on {k}")
    # H12: bound token age — the Django signer mints iat with ttl=30s.
    if claims["exp"] - claims["iat"] > 35:
        raise HTTPException(401, "token age exceeds bound")
    # H21b verify-if-present: bind the body's MCP bearer to the signed JWT.
    # TODO(H21b-flip): make the claim REQUIRED once the Django half (PR #46) is live.
    mcp_hash = claims.get("mcp_token_sha256")
    if mcp_hash is None:
        sys.stderr.write("turn JWT lacks mcp_token_sha256 — Django half not yet deployed\n")
    else:
        mcp_token = ((body.get("config") or {}).get("mcp") or {}).get("token") or ""
        expected = hashlib.sha256(mcp_token.encode()).hexdigest()
        if not (isinstance(mcp_hash, str) and hmac.compare_digest(mcp_hash, expected)):
            raise HTTPException(401, "mcp_token_sha256 mismatch")
    # H20: prune expired entries, then refuse a re-seen unexpired jti.
    now = time.time()
    for j in [j for j, exp in _seen_jtis.items() if exp <= now]:
        del _seen_jtis[j]
    if claims["jti"] in _seen_jtis:
        raise HTTPException(401, "jti already used")
    _seen_jtis[claims["jti"]] = float(claims["exp"])


def _child_env(home: str) -> dict:
    """Minimal allowlisted env — NO tenant secrets (those go via stdin)."""
    env = {
        "HERMES_HOME": home,
        "TERMINAL_ENV": "e2b",
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PYTHONUNBUFFERED": "1",
    }
    # Policy config (not a secret): without this the worker's H25 allowlist silently
    # falls back to its baked default and the Cloud Run env knob is a no-op.
    passthrough = ["E2B_API_KEY", "E2B_TEMPLATE", "LANG", "LC_ALL", "RUNTIME_MCP_ENDPOINT_ALLOWLIST"]
    if LOCAL_NO_IAM:
        passthrough.append("RUNTIME_EXTRA_BASE_URLS")  # off-registry base_url: dev only
    for key in passthrough:
        if key in os.environ:
            env[key] = os.environ[key]
    return env


@app.post("/v1/turn")
async def turn(request: Request, authorization: str = Header(default=""),
               x_runtime_authorization: str = Header(default="")):
    raw = await request.body()
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(422, "body is not valid JSON")
    # Under Cloud Run IAM, Authorization carries the Google identity token, so the
    # RS256 turn JWT travels in X-Runtime-Authorization. The Authorization fallback
    # exists ONLY for local/no-IAM runs (gated) — in prod we require the dedicated
    # header so a misconfigured/absent IAM gate can never silently become single-gate.
    token_header = x_runtime_authorization or (authorization if LOCAL_NO_IAM else "")
    _verify_jwt(token_header, body)

    home = tempfile.mkdtemp(prefix="turn-")
    t0 = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, WORKER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_child_env(home),
        cwd=os.path.dirname(WORKER),
        start_new_session=True,          # own process group -> killable as a unit
    )
    try:
        out, err = proc.communicate(input=raw, timeout=MAX_WALL_SECONDS)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        raise HTTPException(408, "turn exceeded wall-clock budget")
    finally:
        _reap_orphaned_sandboxes(home)
        _rmtree(home)

    if err:
        # stderr is diagnostics only — value-scrubbed (H23) + shape-scrubbed + capped.
        sys.stderr.write(f"[turn {body.get('turn_id')}] child stderr: {_scrub(err, _collect_secret_values(body))[:4000]}\n")
    if len(out) > MAX_STDOUT_BYTES:
        raise HTTPException(502, "child stdout exceeded cap")
    resp = _extract_framed_result(out)
    if not isinstance(resp, dict) or "status" not in resp:
        raise HTTPException(502, "child result failed schema check")
    # A worker that errored before parsing the request can't echo turn_id; pass its
    # structured error through (stamped with the known turn_id) instead of masking it
    # as a generic 502 — preserves the real cause for diagnosis.
    if resp.get("turn_id") != body.get("turn_id"):
        if resp.get("status") == "error":
            resp["turn_id"] = body.get("turn_id")
        else:
            raise HTTPException(502, "child result turn_id mismatch")
    resp.setdefault("usage", {})["parent_wall_ms"] = int((time.monotonic() - t0) * 1000)
    return resp


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _rmtree(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


# Anchored e2b id shape + a hard cap: the breadcrumb lives in the CHILD-writable home,
# so a hostile child must not be able to make the parent spray kills or stall the loop.
_SANDBOX_ID_RE = None
_MAX_REAPED_IDS = 4


def _reap_orphaned_sandboxes(home: str) -> None:
    """H27: a SIGKILLed worker can't run its own sandbox cleanup — kill any sandbox id
    it left in the breadcrumb file (normal turns unrecord theirs, so this is orphans only)."""
    global _SANDBOX_ID_RE
    if _SANDBOX_ID_RE is None:
        import re

        _SANDBOX_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
    try:
        with open(os.path.join(home, ".sandbox_id")) as fh:
            ids = [line.strip() for line in fh if line.strip()]
    except OSError:
        return
    for sandbox_id in ids[:_MAX_REAPED_IDS]:
        if not _SANDBOX_ID_RE.fullmatch(sandbox_id):
            continue
        try:
            from tools.environments.e2b import kill_sandbox

            kill_sandbox(sandbox_id)
        except Exception:  # noqa: BLE001 — best-effort; the 300s lifetime clamp is the backstop
            pass


def _extract_framed_result(out: bytes) -> dict:
    """H24: the result is the ONE line after the LAST sentinel line (rest is noise)."""
    lines = out.decode("utf-8", "replace").splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i] == RESULT_SENTINEL:
            try:
                return json.loads(lines[i + 1])
            except (ValueError, IndexError):
                raise HTTPException(502, "child produced no valid result")
    raise HTTPException(502, "child produced no framed result")


# Kept in parity with turn_worker._SECRET_KEY_HINTS / _MIN_SECRET_LEN — the parent scrubs
# ALL child stderr (incl. success-path SDK noise the worker never touches).
_SECRET_KEY_HINTS = ("api_key", "apikey", "token", "secret", "password", "bearer", "authorization")
_MIN_SECRET_LEN = 6


def _collect_secret_values(node, _under_secret: bool = False) -> set[str]:
    """H23: every secret STRING in the request, any depth; JSON packed inside a secret
    string (browserbase's api_key blob) is parsed and walked too. Plus the E2B key."""
    values: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            kl = str(k).lower()
            hit = _under_secret or kl == "key" or any(h in kl for h in _SECRET_KEY_HINTS)
            values |= _collect_secret_values(v, hit)
    elif isinstance(node, (list, tuple)):
        for item in node:
            values |= _collect_secret_values(item, _under_secret)
    elif _under_secret and isinstance(node, str) and len(node) >= _MIN_SECRET_LEN:
        values.add(node)
        try:
            inner = json.loads(node)
        except ValueError:
            inner = None
        if isinstance(inner, (dict, list)):
            values |= _collect_secret_values(inner, True)
    if not _under_secret and isinstance(node, dict):
        e2b = os.environ.get("E2B_API_KEY", "")
        if len(e2b) >= _MIN_SECRET_LEN:
            values.add(e2b)
    return values


def _scrub(b: bytes, secret_values=()) -> str:
    """Layer 1 (H23): replace known secret values; layer 2: token-shape regex."""
    import re

    s = b.decode("utf-8", "replace")
    for v in sorted(secret_values, key=len, reverse=True):  # longest first: composites
        s = s.replace(v, "[REDACTED]")
    return re.sub(r"(ip_sk_[0-9a-f]+|sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+)", "[REDACTED]", s)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# /healthz appears to be intercepted by the Cloud Run frontend on this org; expose
# an app-owned readiness path too.
@app.get("/readyz")
async def readyz():
    return {"ok": True}
