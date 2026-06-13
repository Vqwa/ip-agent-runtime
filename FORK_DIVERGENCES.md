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
| `deploy/service.yaml` | Image pinned to `:v10` (in lockstep with `cloudbuild.yaml`); `timeoutSeconds: 360`. | Ship the current parity+hardening build; request deadline above the 300s wall. (Was `:v4` pre-Phase-3/4; bumped as hardening revisions shipped.) |
| `.dockerignore` | Exclude `stub_llm.py`, `stub_mcp.py`. | Stop shipping test stubs (`stub_mcp` logs the inbound bearer to `/tmp`). |

## Runtime configuration the wrapper injects (not file forks)

These are set by `turn_worker.py` / Django per turn — they configure stock
Hermes, they don't modify it:

- **Toolset** (`enabled_toolsets`, the parity set): `insightfulpipe` (MCP, read-only),
  `memory`, `terminal` (E2B), `file` (E2B-scoped), `todo`, `web` (SSRF-gated),
  `vision` (SSRF-gated), plus `browser`/`image_gen` when the agent carries that BYOK
  key. All are upstream toolsets, unmodified. **`clarify` is NOT shipped** — dropped
  from the default (commit 6ec757e8, turn_worker maps logical `tools` with no clarify
  branch) because it can never resolve in an async single-shot turn; advertising it
  just burns iterations.
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

## Phase-3 hardening (2026-06-13, HARDENING_PLAN.md H12/H20-H27/H41 — IP-insightful-pipe-app repo)

| File | Change | Why |
|---|---|---|
| `server.py` | H12: `iat` added to `options.require`; reject `exp - iat > 35` (401). | Bound a stolen turn-JWT's usable lifetime; Django signer already mints iat with ttl=30s — 35 = ttl + skew. |
| `server.py` | H20: in-process jti replay guard — module dict `{jti: exp_epoch}` pruned each verify; re-seen unexpired jti → 401 `jti already used`; jti recorded only after all checks pass. | containerConcurrency=1 makes a plain dict safe (no lock). Residual: cross-instance replay stays IAM-bounded (H40). |
| `server.py` | H21b (verify-IF-PRESENT): `mcp_token_sha256` claim compared (constant-time) to sha256 hex of `config.mcp.token` or `""`; mismatch → 401; absent → allowed + logged once per turn. `TODO(H21b-flip)` to require after Django PR #46 is live. | Binds the MCP bearer in the body to the signed turn JWT so a captured/forged body can't swap in another tenant's `ip_sk_`. |
| `server.py` | H24 parent half: result channel is FRAMED — parse the ONE line after the LAST `===TURN_RESULT_v1===` sentinel line; no sentinel → 502 `child produced no framed result` (replaces the old last-stdout-line parse). | Library/tool noise or attacker-influenced JSON on stdout can no longer be read as the turn result. LOCKSTEP: worker half must emit the sentinel in the same image revision. |
| `server.py` | H23 parent half: stderr scrubbed BY VALUE before the shape regex — `_collect_secret_values()` recursively harvests every string under `*key`/`*token`/`*secret`-named keys in the request body + parent-env `E2B_API_KEY` (len≥8), replaced longest-first with `[REDACTED]`; regex stays as layer 2. | Covers both secret populations: request-borne keys with no recognizable prefix (e.g. Oxylabs) AND the parent-env-injected E2B key the request-only set would miss. |
| `tests_runtime/test_server_hardening.py` | Cluster S unit tests for H12/H20/H21b/H23/H24 parent logic (RSA keypair via cryptography; imports server.py only — no subprocess, no Hermes). Not shipped in the image. | — |
| `turn_worker.py` | H24 (worker half): result framed — `===TURN_RESULT_v1===` sentinel line + ONE JSON line as the FINAL stdout write (success + structured-error paths); a leading newline closes any unterminated stray stdout so the sentinel is always its own line. | Parent extracts by the LAST sentinel line; stray Hermes stdout can no longer corrupt or spoof the result channel. |
| `turn_worker.py` | H25: `config.mcp.endpoint` pinned to env `RUNTIME_MCP_ENDPOINT_ALLOWLIST` (comma-separated, default `https://main.insightfulmcp.com/`), exact match after trailing-slash normalization; an off-allowlist endpoint refuses the turn with `error.type=mcp_endpoint_refused` BEFORE config.yaml is written. | The agent's read-only `ip_sk_` MCP bearer must never travel to a body-supplied URL. |
| `turn_worker.py` | H22 (containment): every BYOK env injection goes through `_inject_env` (recorded in `_INJECTED_ENV`) and is deleted in `main()`'s finally the moment the run ends; `_assert_sandbox_env_contained()` refuses the turn pre-agent-start (`error.type=env_key_leak`, names only) if an unrecorded `*_API_KEY`/`*_KEY`/`*_TOKEN*`/`*_SECRET`/`MODAL_*`/`DAYTONA_*` var is present — platform path allows exactly `E2B_API_KEY`, BYOK Modal/Daytona path requires it scrubbed. | Hermes plugins read BYOK keys via `os.getenv` at call time (plugins/web/*, tools/), so construction-time injection would fork upstream files; E2B builds the sandbox remotely with NO host-env forwarding (`tools/environments/e2b.py`), leaving this worker's env as the only key residence. Residual: `/proc/<worker pid>/environ` shows the keys WHILE the turn runs. |
| `turn_worker.py` | H23 (worker half): `error.message` and the worker's stderr error line scrubbed BY VALUE — `_collect_secret_values()` recursively pulls every string under a secret-shaped key at any nesting depth (incl. JSON packed inside browserbase `api_key`), unioned with parent-env platform secrets snapshotted at import (`E2B_API_KEY` today, pre-`_scrub_platform_sandbox_keys`). | Structured errors can quote BOTH secret populations (request-borne + parent-env); pattern-only scrubbing misses arbitrary key shapes. |
| `turn_worker.py` | H26: config.yaml created `0o600` via `os.open(..., 0o600)` — the only on-disk file holding the MCP bearer. | Owner-only from the first byte; no chmod race window. |
| `tests_runtime/test_worker_hardening.py` | New worker-cluster test module (stdlib-only; never imports `run_agent` — exercises pure helpers + the pre-import refusal paths of `run_turn()`/`main()`). | Regression gates for the H22/H23/H24/H25/H26 worker halves. |
| `tools/environments/e2b.py` | H27: record live sandbox id in `$HERMES_HOME/.sandbox_id` (one per line) on create; unrecord only on confirmed kill; module-level `kill_sandbox(sandbox_id)` reaper; `sandbox_lifetime` clamped to `MAX_SANDBOX_LIFETIME=300`. | A wall-clock SIGKILL of the worker orphaned the microVM until lifetime expiry; the parent reaps via the breadcrumb, and an unreaped orphan now self-expires within one turn budget. |
| `plugins/web/oxylabs/provider.py` | H41: per-URL `is_safe_url` re-check inside `extract()` — blocked URLs become error rows and never reach AI-Scraper. | Defense in depth: `web_extract_tool`'s dispatcher gate (web_tools.py:967) is the primary SSRF check; the fork-owned provider now holds even if invoked directly. |
| `ci_guard_runtime.py` | Gates 4–6: gate-16 spawn-shape check (`server._child_env()` carries no secret-shaped key beyond the `E2B_API_KEY` passthrough), `tools/url_safety` metadata-floor regression pin (169.254.169.254 + metadata.google.internal), and H24 framing-sentinel identity check across `server.py`/`turn_worker.py` (text-level). | Makes the spawn-boundary, SSRF-floor, and result-framing promises self-enforcing in CI. |
| `tests_runtime/test_env_hardening.py` | New (cluster E): unit tests for the H27 reaper/breadcrumb/lifetime clamp and the H41 oxylabs gate; SDK surfaces stubbed via sys.modules, no network, stdlib-only imports. | Falsifiable DONE gates for the fork-side H27/H41 changes. |
| `server.py` | H27 parent half: `finally` reads `$HERMES_HOME/.sandbox_id` and best-effort `kill_sandbox()`s each id before `_rmtree` (normal turns unrecord theirs — orphans only). | A SIGKILLed worker cannot run its own sandbox cleanup; the 300s lifetime clamp is the backstop. |

## Phase-4 hardening (2026-06-13, HARDENING_PLAN.md H29–H31/H34/H35 + H38a — runtime half)

The KMS signing (H29), key rotation (H30), CMEK (H31), abuse caps (H34) and webhook-role
split (H35) are mostly Django-side (PR #48). The runtime fork carries only the H30 verifier
half + the extended IAM audit:

| File | Change | Why |
|---|---|---|
| `server.py` | H30 kid-keyed verifier: `_parse_public_keys_json(RUNTIME_JWT_PUBLIC_KEYS_JSON)` builds a `{kid: PEM}` set; `_verify_jwt` selects the PEM by the token's `kid` header (kid-less → the single `RUNTIME_JWT_PUBLIC_KEY`; unknown kid → 401). | The KMS signer (Django `sign_turn_jwt_kms`) stamps `kid` = the KMS key-version resource, so rotation is add-PEM → switch signer → drop old without a lockstep image rebuild. Malformed env JSON fails at import (deploy), never per-turn. |
| `deploy/audit_iam.sh` | H38a: invoker set asserted == exactly `agent-control@` (not the old gmail); runtime SA `secretAccessor` == exactly `{e2b-api-key}`; auditConfigs now also require `cloudkms.googleapis.com DATA_READ` (H5 extended for the H29 KMS signer). | Makes the Phase-0 identity split + KMS audit-log promise self-enforcing as a deploy-blocking C9 gate. |
| `tests_runtime/test_server_kid.py` | Unit tests for the H30 kid selection paths (RSA keypairs via cryptography; imports `server.py` only). | Falsifiable gate for the kid verifier. Not shipped in the image. |

**Residual (H40):** with no Render OIDC/WIF (H6), KMS removes key *persistence* + adds
revocation/audit but a static-key holder can still CALL KMS to sign — it does not remove the
signing capability from a key thief. CMEK (H31) only protects new secret versions, which is why
H31 mandates rotating every live version. Both stated in HARDENING_PLAN, not implied away here.
