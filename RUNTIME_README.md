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
  runs the Hermes agent loop once, and emits a single JSON response line on
  stdout. Logs go to stderr.

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
- **`cloudbuild.yaml`** builds and pushes that image to Artifact Registry.

`RUNTIME_JWT_PUBLIC_KEY`, `E2B_API_KEY`, and `E2B_TEMPLATE` are provided at
deploy time. The service runs with internal ingress + IAM so the Django service
account is the only invoker.
