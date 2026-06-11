"""FastAPI parent for the InsightfulPipe Hosted Agents runtime.

Verifies the per-turn RS256 JWT (baked-in PUBLIC key only), then runs the turn
in a FRESH SUBPROCESS per request with:
  * a unique ephemeral HERMES_HOME set BEFORE the child interpreter starts,
  * a MINIMAL env (NO tenant secrets — those go to the child via stdin only),
  * a hard wall-clock deadline enforced by a process-group SIGTERM -> SIGKILL,
  * stdout read + schema-validated as the result; stderr captured/capped/scrubbed.

Contract: docs/hosted_agents/TURN_CONTRACT.md. Auth/§4.1 + child-boundary/§5/§2.2 of PLAN.
GCP IAM (Cloud Run invoker = the Django SA only) is the second, independent control.
"""

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
MAX_WALL_SECONDS = int(os.environ.get("RUNTIME_MAX_WALL_SECONDS", "150"))
MAX_STDOUT_BYTES = int(os.environ.get("RUNTIME_MAX_STDOUT_BYTES", str(8 * 1024 * 1024)))
# E2B_API_KEY + E2B_TEMPLATE are read from this process's env and passed through.


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
            options={"require": ["exp", "iss", "aud", "jti"]},
        )
    except jwt.InvalidTokenError as e:
        raise HTTPException(401, f"invalid token: {e}")
    # Claims must match the body — a forged/mismatched payload can't drive a
    # different tenant's turn.
    for k in ("agent_id", "workspace_id", "turn_id"):
        if claims.get(k) != body.get(k):
            raise HTTPException(401, f"claim/body mismatch on {k}")


def _child_env(home: str) -> dict:
    """Minimal allowlisted env — NO tenant secrets (those go via stdin)."""
    env = {
        "HERMES_HOME": home,
        "TERMINAL_ENV": "e2b",
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PYTHONUNBUFFERED": "1",
    }
    for passthrough in ("E2B_API_KEY", "E2B_TEMPLATE", "LANG", "LC_ALL", "RUNTIME_EXTRA_BASE_URLS"):
        if passthrough in os.environ:
            env[passthrough] = os.environ[passthrough]
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
    # RS256 turn JWT travels in X-Runtime-Authorization. Fall back to Authorization
    # for local / no-IAM runs.
    _verify_jwt(x_runtime_authorization or authorization, body)

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
        _rmtree(home)

    if err:
        # stderr is diagnostics only — capped + scrubbed, never the result.
        sys.stderr.write(f"[turn {body.get('turn_id')}] child stderr: {_scrub(err)[:4000]}\n")
    if len(out) > MAX_STDOUT_BYTES:
        raise HTTPException(502, "child stdout exceeded cap")
    try:
        resp = json.loads(out.strip().splitlines()[-1])   # last line = result channel
    except (ValueError, IndexError):
        raise HTTPException(502, "child produced no valid result")
    if resp.get("turn_id") != body.get("turn_id") or "status" not in resp:
        raise HTTPException(502, "child result failed schema check")
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


def _scrub(b: bytes) -> str:
    """Best-effort redaction of token shapes in stderr (defense in depth)."""
    import re

    s = b.decode("utf-8", "replace")
    return re.sub(r"(ip_sk_[0-9a-f]+|sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+)", "[REDACTED]", s)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# /healthz appears to be intercepted by the Cloud Run frontend on this org; expose
# an app-owned readiness path too.
@app.get("/readyz")
async def readyz():
    return {"ok": True}
