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
| `plugins/web/oxylabs/` | New web provider (AI-Search + AI-Scraper via `oxylabs-ai-studio`), mirroring the `ddgs`/`firecrawl` plugin shape. BYOK `OXYLABS_API_KEY`, injected per-turn. Not in upstream (the Hostinger template's Oxylabs field is packaging, not upstream code). |

## Modified upstream files (kept minimal; each line justified)

| File | Change | Why |
|---|---|---|
| `tools/terminal_tool.py` | Register `e2b` in `_create_environment` + `check_terminal_requirements`. | Make the E2B backend selectable via `TERMINAL_ENV=e2b`. |
| `tools/env_probe.py` | Recognize the `e2b` environment. | Probe correctness for the new backend. |
| `tools/lazy_deps.py` | Declare `terminal.e2b` dep. | Lazy-dep metadata for the E2B SDK. |
| `agent/prompt_builder.py` | (1) Add `e2b` to `_REMOTE_TERMINAL_BACKENDS`. (2) Add `e2b` to `_BACKEND_FALLBACK_DESCRIPTIONS`. | Treat E2B as a remote terminal; give the agent an accurate sandbox hint. No prompt-guidance text changed. |
| `tools/environments/e2b.py` | Call `self.init_session()` at end of `__init__` (guarded). | **Parity** with `docker.py`/`modal.py`: persist shell env/functions across terminal calls within a turn. Guarded → snapshot failure degrades to stateless, never breaks exec. |
| `server.py` | `RUNTIME_MAX_WALL_SECONDS` default `150 → 300`. | Headroom for the stock 90-iteration budget so turns finish gracefully instead of hitting SIGKILL. |
| `Dockerfile.runtime` | Runtime pip extras: `e2b`, `ddgs` (keyless web search), the web/image plugins, and the BYOK sandbox SDKs `modal==1.3.4` + `daytona==0.155.0`. | These ship in the image because Hermes leaves them as optional extras; our serving path needs them present. |
| `deploy/service.yaml` | Image `:v3 → :v4`; add `timeoutSeconds: 360`. | Ship the parity build; request deadline above the 300s wall. |
| `.dockerignore` | Exclude `stub_llm.py`, `stub_mcp.py`. | Stop shipping test stubs (`stub_mcp` logs the inbound bearer to `/tmp`). |

## Runtime configuration the wrapper injects (not file forks)

These are set by `turn_worker.py` / Django per turn — they configure stock
Hermes, they don't modify it:

- **Toolset** (`enabled_toolsets`, the parity set): `insightfulpipe` (MCP, read-only),
  `memory`, `clarify`, `terminal` (E2B), `file` (E2B-scoped), `todo`, `web`
  (SSRF-gated), `vision` (SSRF-gated). All are upstream toolsets, unmodified.
- **`max_iterations = 90`** — stock Hermes default (was 16).
- **`web.backend`** in `config.yaml` — `oxylabs` when the request carries a BYOK
  key (injected as `OXYLABS_API_KEY`), else keyless `ddgs`. Name whitelisted.
- **Cloud browser + image-gen (BYOK)** — `browser.cloud_provider: browser-use` +
  `BROWSER_USE_API_KEY`, and `image_gen.provider: fal` + `FAL_KEY`, written only
  when the request carries the agent's key. Hermes' own plugins, cloud mode (no
  Chromium in the image), Hermes SSRF gating on cloud backends.
- **Providers** — base-URL allowlist, kept in **LOCKSTEP** with Django
  `tasks._PROVIDER_BASE_URLS` (the runtime rejects any off-registry base_url):
  openai, openrouter, anthropic, nexos (`api.nexos.ai/v1`), deepseek, gemini,
  plus the OpenAI-compatible set alibaba / zai / moonshot / nvidia / huggingface
  / novita, plus the ChatGPT-subscription Codex backend `openai-codex`
  (`chatgpt.com/backend-api/codex`). **xAI removed** (dropped product-wide).
  Only openai/anthropic/openrouter/deepseek/gemini/openai-codex are in
  `_HERMES_KNOWN_PROVIDERS` (passed verbatim); the rest go `provider=None` and
  Hermes auto-detects the OpenAI wire from the host.
- **Subscription OAuth** — for anthropic + openai, `auth_kind=oauth` ships the
  OAuth JWT as `api_key`. An OpenAI-OAuth agent routes to `openai-codex` (Django
  `_dispatch_provider`); the codex transport derives the `ChatGPT-Account-ID` +
  Cloudflare headers from the JWT — no extra wiring. Codex accepts only the codex
  model list (gpt-5.3-codex / 5.4 / 5.4-mini / 5.5).
- **BYOK code-exec sandbox** — `config.sandbox` (our contract field) chooses the
  backend: absent → platform E2B; `{provider:modal, token_id, token_secret}` →
  `TERMINAL_ENV=modal` (+ direct mode, never the Nous-managed gateway);
  `{provider:daytona, api_key}` → `TERMINAL_ENV=daytona`. On any BYOK turn,
  `_scrub_platform_sandbox_keys()` drops the platform E2B key (+ would-be
  runtime/GCP secrets) so they can't reach the customer's own cloud. `modal` +
  `daytona` SDKs are baked into `Dockerfile.runtime`.
- **Dedicated vision model (optional)** — `config.vision_model`
  `{provider, model, base_url, api_key}` (base_url held to the same allowlist) is
  written as `auxiliary.vision` in config.yaml, taking Hermes' direct-endpoint aux
  path (no credential pool). Empty by default — multimodal main models see images
  natively.
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
- **BYOK Modal/Daytona = the customer's own cloud isolation,** not ours. This is
  identical to how any Hermes self-hoster runs those backends (their creds, their
  sandbox). Confirmed by trace: neither backend forwards host env, and file-sync
  carries only registered credential/skill/cache files — all empty in the hosted
  `HERMES_HOME`, so the MCP token in `config.yaml` is NOT synced in.
  **GUARDRAIL: never add `skills/`, `cache/`, or `terminal.credential_files` to
  the hosted `HERMES_HOME`** — that is the only path that would sync the MCP token
  into a customer sandbox.

## How to pull an upstream Hermes update

The fork is additive, so most upstream changes merge for free:

1. `git fetch origin && git merge origin/main` (or rebase our ~dozen commits).
   Our NEW files never conflict; the agent loop / tools / transports / providers
   we never touched come in clean.
2. Conflicts can only occur in the 4 lightly-forked files below — re-apply our
   additive `e2b` entries (this ledger lists each line):
   `tools/terminal_tool.py`, `tools/env_probe.py`, `tools/lazy_deps.py`,
   `agent/prompt_builder.py`.
3. Smoke-test the contract: a turn still runs iff `AIAgent.run_conversation`'s
   signature and the `enabled_toolsets` / config-yaml keys are unchanged — both
   are exercised by one real turn.
4. Rebuild + redeploy: `gcloud builds submit --config cloudbuild.yaml
   --project=ip-agent-runtime .` then `gcloud run deploy ip-agent-runtime
   --image …@<digest> --region europe-west1 --project ip-agent-runtime`.

Regenerate the raw footprint any time with `git diff --name-status origin/main`.

## Pre-existing upstream issues we did NOT introduce (left as-is)

- `agent/prompt_builder.py` imports `get_environment` from `tools.environments`,
  which doesn't export it → the live in-sandbox env probe always falls back. Upstream
  bug; we mitigate only the cosmetic side (added the `e2b` fallback description).
