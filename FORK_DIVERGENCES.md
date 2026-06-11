# Fork Divergences — `insightfulpipe-runtime` vs upstream `origin/main`

Complete ledger of every change this fork makes to NousResearch Hermes. The
goal is **behavioral parity** with stock Hermes: the agent loop, tools, prompt,
and memory are upstream's, unmodified. We only change *plumbing* (serving,
sandbox, secrets) and we keep the upstream file structure intact.

Regenerate the raw list any time with:

```bash
git diff --name-status origin/main
```

## Added files (new — additive serving/sandbox layer, no upstream file touched)

| File | Purpose |
|---|---|
| `server.py` | FastAPI parent: per-turn subprocess, dual-auth (RS256 turn-JWT + Cloud Run IAM), wall-clock, stdout schema-validate. |
| `turn_worker.py` | Headless child: one `AIAgent.run_conversation` per stdin request; secrets via stdin only. |
| `tools/environments/e2b.py` | E2B Firecracker sandbox backend (egress-off, no `~/.hermes` sync, ephemeral). Subclasses upstream `BaseEnvironment`. |
| `Dockerfile.runtime`, `cloudbuild.yaml`, `.gcloudignore` | Runtime image build (Cloud Run, amd64). |
| `deploy/service.yaml`, `deploy/audit_iam.sh`, `deploy/README.md` | Cloud Run IaC + IAM-isolation audit. |
| `ci_guard_runtime.py` | CI assertion that CLI/gateway/preprocessors stay unreachable in the serving path. |
| `RUNTIME_README.md`, `FORK_DIVERGENCES.md` | This fork's docs. |
| `stub_llm.py`, `stub_mcp.py` | Local test stubs only. **Excluded from the image** via `.dockerignore` — never shipped. |

## Modified upstream files (kept minimal; each line justified)

| File | Change | Why |
|---|---|---|
| `tools/terminal_tool.py` | Register `e2b` in `_create_environment` + `check_terminal_requirements`. | Make the E2B backend selectable via `TERMINAL_ENV=e2b`. |
| `tools/env_probe.py` | Recognize the `e2b` environment. | Probe correctness for the new backend. |
| `tools/lazy_deps.py` | Declare `terminal.e2b` dep. | Lazy-dep metadata for the E2B SDK. |
| `agent/prompt_builder.py` | (1) Add `e2b` to `_REMOTE_TERMINAL_BACKENDS`. (2) Add `e2b` to `_BACKEND_FALLBACK_DESCRIPTIONS`. | Treat E2B as a remote terminal; give the agent an accurate sandbox hint. No prompt-guidance text changed. |
| `tools/environments/e2b.py` | Call `self.init_session()` at end of `__init__` (guarded). | **Parity** with `docker.py`/`modal.py`: persist shell env/functions across terminal calls within a turn. Guarded → snapshot failure degrades to stateless, never breaks exec. |
| `server.py` | `RUNTIME_MAX_WALL_SECONDS` default `150 → 300`. | Headroom for the stock 90-iteration budget so turns finish gracefully instead of hitting SIGKILL. |
| `Dockerfile.runtime` | Add `ddgs` to pip install. | Keyless DuckDuckGo backend so the `web` toolset's `web_search` works without a provider key. |
| `deploy/service.yaml` | Image `:v3 → :v4`; add `timeoutSeconds: 360`. | Ship the parity build; request deadline above the 300s wall. |
| `.dockerignore` | Exclude `stub_llm.py`, `stub_mcp.py`. | Stop shipping test stubs (`stub_mcp` logs the inbound bearer to `/tmp`). |

## Runtime configuration the wrapper injects (not file forks)

These are set by `turn_worker.py` / Django per turn — they configure stock
Hermes, they don't modify it:

- **Toolset** (`enabled_toolsets`, the parity set): `insightfulpipe` (MCP, read-only),
  `memory`, `clarify`, `terminal` (E2B), `file` (E2B-scoped), `todo`, `web`
  (SSRF-gated), `vision` (SSRF-gated). All are upstream toolsets, unmodified.
- **`max_iterations = 90`** — stock Hermes default (was 16).
- **`web.backend: ddgs`** in `config.yaml` — keyless search.
- **`skip_context_files=True`, `session_db=None`, `save_trajectories=False`** —
  ephemeral per-turn model; no host project files or cross-session DB.

## Known, intentional behavioral divergences (documented, not "bugs")

- **E2B ignores `TERMINAL_CONTAINER_*` sizing.** It is deliberately absent from the
  `{docker,singularity,modal,daytona}` membership sets in `tools/terminal_tool.py`
  (lines ~1075, ~2011). E2B sizes via `E2B_TEMPLATE`, not per-request CPU/mem/disk,
  so those env vars are intentionally ignored; `cwd` is fixed to `/home/user`.
- **E2B egress is OFF.** Code-exec cannot fetch URLs or pip-install at runtime
  (`HERMES_DISABLE_LAZY_INSTALLS=1`); it relies on the pre-baked `E2B_TEMPLATE`.
- **Wall-clock exists.** Stock Hermes has no wall-clock; we enforce one
  (parent SIGTERM→SIGKILL) for the multi-tenant runtime. Single source: 300s
  (`RUNTIME_MAX_WALL_SECONDS` = Django `_MAX_WALL_SECONDS`; Cloud Run `timeoutSeconds=360`).
- **`clarify` is non-interactive.** In an async single-shot turn there is no
  synchronous human; clarify degrades to a plain question on the channel.

## Pre-existing upstream issues we did NOT introduce (left as-is)

- `agent/prompt_builder.py` imports `get_environment` from `tools.environments`,
  which doesn't export it → the live in-sandbox env probe always falls back. Upstream
  bug; we mitigate only the cosmetic side (added the `e2b` fallback description).
