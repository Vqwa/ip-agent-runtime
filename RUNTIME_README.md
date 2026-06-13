# InsightfulPipe Hosted Agents Runtime

This is InsightfulPipe's Hosted Agents runtime — a fork of
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) with a
thin headless serving layer and a hardened sandbox backend layered on top. The
upstream Hermes agent loop is unchanged; everything here is additive.

## Per-turn headless model

The runtime executes exactly **one agent turn per HTTP request** — no long-lived
agent process, no shared state between turns.

- **`server.py`** — a FastAPI parent. It verifies a per-turn RS256 JWT against a
  baked-in public key, then spawns a fresh subprocess per request with a unique
  ephemeral `HERMES_HOME`, a minimal allowlisted environment (no tenant secrets),
  and a hard wall-clock deadline. Tenant secrets are passed to the child via
  **stdin only**, never env/argv. Its stdout is read and schema-validated as the
  turn result; stderr is captured, capped, and scrubbed.
- **`turn_worker.py`** — the headless child entry point. It reads a turn-contract
  request as JSON on stdin, materializes config/memory into the ephemeral home,
  runs the Hermes agent loop once, and emits the **framed** result as its final
  stdout write: a `===TURN_RESULT_v1===` sentinel line followed by exactly one JSON
  line (H24). The parent parses the line after the LAST sentinel and treats all other
  stdout as noise, so library/tool/agent prints can't spoof the result. Logs go to
  stderr (value-scrubbed for request secrets + the parent E2B key, H23).

## E2B sandbox backend

**`tools/environments/e2b.py`** runs all agent code/shell in a disposable E2B
Firecracker microVM, selected via `TERMINAL_ENV=e2b`. Unlike the stock remote
backends it **never syncs `~/.hermes`** into the sandbox (no credentials to
steal), denies egress by default, and is ephemeral (one microVM per turn). The
backend is registered in `tools/terminal_tool.py` and declared in
`tools/lazy_deps.py`.

## Deploy to Cloud Run

- **`Dockerfile.runtime`** builds the runtime image (`python:3.12-slim`, Hermes
  core + E2B/MCP/JWT/uvicorn, non-root) serving `server:app` on port 8080.
- **`cloudbuild.yaml`** builds and pushes that image (`:v10`) to Artifact Registry.

### Deploy-time env (NOT baked secrets)

| Var | Purpose |
|---|---|
| `RUNTIME_JWT_PUBLIC_KEY` | The single baked-in RS256 **public** PEM used to verify kid-less turn JWTs. |
| `RUNTIME_JWT_PUBLIC_KEYS_JSON` | Optional `{kid: PEM}` verifier set (H30). The KMS signer's `kid` = its key-version resource; rotation = add-PEM → switch signer → drop old. Malformed JSON fails at import (deploy, not turns). |
| `RUNTIME_MCP_ENDPOINT_ALLOWLIST` | Comma-separated allowlist the worker pins `config.mcp.endpoint` to (H25, default `https://main.insightfulmcp.com/`). The parent passes it through to the child env (`_child_env`); without it the worker silently falls back to its baked default. |
| `E2B_API_KEY`, `E2B_TEMPLATE` | Platform E2B sandbox key + template; the only secret-shaped vars the child env is allowed to carry (gate-16 / `_assert_sandbox_env_contained`). |
| `RUNTIME_MAX_WALL_SECONDS` | Wall-clock budget, default 300 (Cloud Run `timeoutSeconds=360` sits above it). |

The service runs with internal ingress + IAM so the Django service account is the
only invoker. Tenant secrets (`config.llm.api_key`, `config.mcp.token`, BYOK keys)
are NEVER deploy env — they arrive per-turn on stdin.

### CI guard

`ci_guard_runtime.py` enforces the runtime invariants (message-expansion
preprocessors + CLI/gateway unreachable, plugin manager inert, gate-16 spawn-shape,
`tools/url_safety` metadata floor, H24 sentinel parity across `server.py`/`turn_worker.py`).
**It is a standalone script — run it in CI/pre-deploy (`python ci_guard_runtime.py`).
It is NOT yet wired into `cloudbuild.yaml` or `Dockerfile.runtime`, so the build does
not block on it today; that wiring is the remaining step to make the gate self-enforcing.**
