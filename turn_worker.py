"""Headless per-turn entry for InsightfulPipe Hosted Agents.

One invocation = one agent turn. Reads a turn-contract request as JSON on
**stdin** (so secrets never touch env/argv), runs the Hermes agent loop once,
and writes the FRAMED result on **stdout**: a `===TURN_RESULT_v1===` sentinel
line followed by one JSON line, as the final stdout write. All logs go to
stderr. Contract: docs/hosted_agents/TURN_CONTRACT.md.

CRITICAL ORDERING: HERMES_HOME and the runtime env are set BEFORE importing
run_agent, because ~30 path constants freeze get_hermes_home() at import.
The FastAPI parent normally sets HERMES_HOME before spawning this process; we
create a fresh ephemeral one if unset so the worker is runnable standalone.
"""

import json
import os
import re
import sys
import tempfile
import time
import uuid

# --- 1) Environment, BEFORE any hermes import -------------------------------
# In the FastAPI parent path HERMES_HOME is provided + rmtree'd by server.py. If we
# create it (standalone/dev), we own cleanup — see main()'s finally.
_PARENT_PROVIDED_HOME = bool(os.environ.get("HERMES_HOME"))
_HOME = os.environ.get("HERMES_HOME") or tempfile.mkdtemp(prefix="turn-")
os.environ["HERMES_HOME"] = _HOME
os.environ.setdefault("HERMES_DISABLE_LAZY_INSTALLS", "1")  # no runtime pip
os.environ.setdefault("TERMINAL_ENV", "e2b")  # code-exec runs in the E2B sandbox
# E2B_API_KEY (platform key) + E2B_TEMPLATE are injected by the parent's env.

# H24: framed result channel — the parent extracts the result by the LAST sentinel line.
RESULT_SENTINEL = "===TURN_RESULT_v1==="

# Env names that look like credentials (E2B_TEMPLATE deliberately not matched).
_SECRET_ENV_NAME_RE = re.compile(r"(_API_KEY|_TOKEN(_ID)?|_SECRET|_KEY)$|^(MODAL_|DAYTONA_)")
_MIN_SECRET_LEN = 6
# H23: parent-injected platform secrets (the E2B key today), snapshotted at import —
# BEFORE any BYOK injection or _scrub_platform_sandbox_keys pop — for by-value scrubbing.
_PARENT_ENV_SECRETS = frozenset(
    v for k, v in os.environ.items() if _SECRET_ENV_NAME_RE.search(k) and len(v) >= _MIN_SECRET_LEN
)
# H22: every BYOK env var we inject is recorded here and deleted in main()'s finally.
# Residual (documented): /proc/<this worker's pid>/environ shows them WHILE the turn
# runs — Hermes plugins read them via os.getenv at call time, so they must be in env.
_INJECTED_ENV: dict[str, str] = {}


class TurnRefused(Exception):
    """Refusal with a stable contract error type (surfaces as error.type)."""

    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type


def _inject_env(name: str, value: str) -> None:
    """H22: the only sanctioned way to put a request-borne secret into os.environ."""
    _INJECTED_ENV[name] = value
    os.environ[name] = value


def _clear_injected_env() -> None:
    # Values stay recorded in _INJECTED_ENV so the error-path scrub set still has them.
    for name in _INJECTED_ENV:
        os.environ.pop(name, None)


_SECRET_KEY_HINTS = ("api_key", "apikey", "token", "secret", "password", "bearer", "authorization")


def _collect_secret_values(obj, _under_secret: bool = False) -> set[str]:
    """Every string value under a secret-shaped key, at any nesting depth. String values
    packing JSON (browserbase's api_key blob) are parsed and walked as secrets too."""
    found: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            hit = _under_secret or kl == "key" or any(h in kl for h in _SECRET_KEY_HINTS)
            found |= _collect_secret_values(v, hit)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            found |= _collect_secret_values(v, _under_secret)
    elif _under_secret and isinstance(obj, str) and len(obj) >= _MIN_SECRET_LEN:
        found.add(obj)
        try:
            inner = json.loads(obj)
        except ValueError:
            inner = None
        if isinstance(inner, (dict, list)):
            found |= _collect_secret_values(inner, True)
    return found


def _scrub_set(req) -> set[str]:
    """H23: BOTH secret populations — request-borne values + parent-env platform keys."""
    secrets = set(_PARENT_ENV_SECRETS)
    secrets |= {v for v in _INJECTED_ENV.values() if len(v) >= _MIN_SECRET_LEN}
    if isinstance(req, dict):
        secrets |= _collect_secret_values(req)
    return secrets


def _scrub_by_value(text: str, secrets) -> str:
    for s in sorted(secrets, key=len, reverse=True):
        if s:  # an empty needle would scramble the text
            text = text.replace(s, "[REDACTED]")
    return text


def _normalize_endpoint(url: str) -> str:
    return url.strip().rstrip("/")


def _mcp_endpoint_allowlist() -> set[str]:
    raw = os.environ.get("RUNTIME_MCP_ENDPOINT_ALLOWLIST", "https://main.insightfulmcp.com/")
    return {_normalize_endpoint(u) for u in raw.split(",") if u.strip()}


def _check_mcp_endpoint(mcp: dict | None) -> None:
    """H25: the MCP bearer only ever travels to a pinned endpoint. Exact match after
    trailing-slash normalization; MUST run before config.yaml is written."""
    endpoint = (mcp or {}).get("endpoint")
    if endpoint and _normalize_endpoint(endpoint) not in _mcp_endpoint_allowlist():
        raise TurnRefused("mcp_endpoint_refused", f"mcp endpoint {endpoint!r} not in the runtime allowlist")


def _assert_sandbox_env_contained() -> None:
    """H22 containment, pre-agent-start. The E2B sandbox env is built remotely
    (tools/environments/e2b.py forwards no host env), so this worker's env is the only
    residence of key material — refuse the turn if an unrecorded credential is present."""
    byok = os.environ.get("TERMINAL_ENV") in ("modal", "daytona")
    allowed = set(_INJECTED_ENV)
    if not byok:
        allowed.add("E2B_API_KEY")  # the platform key the e2b SDK itself reads host-side
    leaked = sorted(k for k in os.environ if _SECRET_ENV_NAME_RE.search(k) and k not in allowed)
    if leaked:  # names only, never values
        raise TurnRefused("env_key_leak", f"unexpected credential env vars before agent start: {leaked}")


_PROVIDER_BASE_URLS = {  # base_url is NOT free-form (PLAN §6); pin per provider
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "anthropic": "https://api.anthropic.com",
    "nexos": "https://api.nexos.ai/v1",
    "deepseek": "https://api.deepseek.com/v1",
    # "xai" removed — dropped product-wide; keep in lockstep with Django.
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    # OpenAI-compatible providers (Hermes canonical set) — not in _HERMES_KNOWN_PROVIDERS,
    # so provider is passed as None and Hermes auto-detects the OpenAI wire from base_url.
    "alibaba": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "zai": "https://api.z.ai/api/paas/v4",
    "moonshot": "https://api.moonshot.ai/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "huggingface": "https://router.huggingface.co/v1",
    "novita": "https://api.novita.ai/openai/v1",
    # ChatGPT-subscription Codex backend (device-code OAuth). The api_key is the OAuth
    # JWT; the codex transport derives the ChatGPT-Account-ID header from its claims.
    "openai-codex": "https://chatgpt.com/backend-api/codex",
}

# Backend name -> the env var its Hermes provider reads (None = keyless). Names are
# the providers' exact .name values; searxng's "key" is the instance URL.
_WEB_BACKENDS = {
    "ddgs": None,
    "oxylabs": "OXYLABS_API_KEY",
    "tavily": "TAVILY_API_KEY",
    "exa": "EXA_API_KEY",
    "firecrawl": "FIRECRAWL_API_KEY",
    "parallel": "PARALLEL_API_KEY",
    "brave-free": "BRAVE_SEARCH_API_KEY",
    # NOTE: searxng intentionally excluded — its "key" is an instance URL the worker
    # would fetch host-side (SSRF to metadata/private IPs). All others auth via a key.
}
_BROWSER_PROVIDERS = {"browser-use": "BROWSER_USE_API_KEY", "browserbase": "BROWSERBASE_API_KEY", "firecrawl": "FIRECRAWL_API_KEY"}
_IMAGE_GEN_PROVIDERS = {"fal": "FAL_KEY", "krea": "KREA_API_KEY", "openai": "OPENAI_API_KEY"}
# Provider names Hermes' registries know — passed explicitly so vision/model routing
# is deterministic (base_url auto-detection leaves provider='' for several of these).
_HERMES_KNOWN_PROVIDERS = {"openai", "anthropic", "openrouter", "deepseek", "gemini", "openai-codex"}
# Baked at image build; copied per-turn into HERMES_HOME so models.dev capability
# lookups (supports_vision etc.) work offline — the ephemeral home is always cold.
_MODELS_DEV_SNAPSHOT = os.environ.get("MODELS_DEV_SNAPSHOT", "/app/models_dev_snapshot.json")


def _scrub_platform_sandbox_keys() -> None:
    """Defense-in-depth for BYOK (Modal/Daytona) turns: drop PLATFORM secrets from the
    env so they can never reach the customer's own cloud sandbox. The worker env is
    already allowlisted (server._child_env passes only the E2B platform keys + locale),
    and the Modal/Daytona backends don't forward host env today — this guards against a
    future Hermes version that does, and against the allowlist growing. None of these is
    needed once code-exec runs on the customer's account."""
    for k in (
        "E2B_API_KEY",
        "E2B_TEMPLATE",
        "RUNTIME_JWT_PUBLIC_KEY",
        "RUNTIME_JWT_PRIVATE_KEY",
        "RUNTIME_EXTRA_BASE_URLS",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_APPLICATION_CREDENTIALS_JSON",
        "GCP_SERVICE_ACCOUNT_KEY",
        "GCLOUD_SERVICE_KEY",
    ):
        os.environ.pop(k, None)


def _materialize_home(req: dict) -> None:
    """Write config.yaml (MCP server + bearer) and memory files into HERMES_HOME."""
    os.makedirs(os.path.join(_HOME, "memories"), exist_ok=True)
    # Seed the models.dev disk cache from the baked snapshot (stale-disk fallback
    # keeps vision capability lookups working when the live fetch fails).
    try:
        if os.path.exists(_MODELS_DEV_SNAPSHOT):
            import shutil

            shutil.copyfile(_MODELS_DEV_SNAPSHOT, os.path.join(_HOME, "models_dev_cache.json"))
    except OSError:
        pass
    mcp = req["config"].get("mcp")
    if mcp and mcp.get("endpoint"):
        # One remote MCP server, bearer = the agent's read-only ip_sk_.
        config_yaml = (
            "mcp_servers:\n"
            "  insightfulpipe:\n"
            f"    url: {json.dumps(mcp['endpoint'])}\n"
            "    transport: streamable_http\n"
            "    headers:\n"
            f"      Authorization: {json.dumps('Bearer ' + mcp['token'])}\n"
        )
    else:
        config_yaml = "mcp_servers: {}\n"
    # Backend menus mirror stock Hermes setup: names whitelisted (interpolated into
    # YAML) and each provider's exact env key injected from the agent's BYOK value.
    web = req["config"].get("web") or {}
    backend = web.get("backend") if web.get("backend") in _WEB_BACKENDS else "ddgs"
    config_yaml += f"web:\n  backend: {backend}\n"
    if web.get("api_key") and _WEB_BACKENDS.get(backend):
        _inject_env(_WEB_BACKENDS[backend], web["api_key"])
    # Cloud browser — cloud mode, no local Chromium in the image. Browserbase packs
    # {"api_key","project_id"} as JSON in api_key (both required by its provider).
    browser = req["config"].get("browser") or {}
    bprov = browser.get("provider") if browser.get("provider") in _BROWSER_PROVIDERS else "browser-use"
    if browser.get("api_key"):
        if bprov == "browserbase":
            try:
                bb = json.loads(browser["api_key"])
                _inject_env("BROWSERBASE_API_KEY", bb.get("api_key", ""))
                _inject_env("BROWSERBASE_PROJECT_ID", bb.get("project_id", ""))
            except ValueError:
                _inject_env("BROWSERBASE_API_KEY", browser["api_key"])
        else:
            _inject_env(_BROWSER_PROVIDERS[bprov], browser["api_key"])
        config_yaml += f"browser:\n  cloud_provider: {bprov}\n"
    # Image generation — Hermes' bundled plugins (fal/krea/openai).
    image_gen = req["config"].get("image_gen") or {}
    iprov = image_gen.get("provider") if image_gen.get("provider") in _IMAGE_GEN_PROVIDERS else "fal"
    if image_gen.get("api_key"):
        _inject_env(_IMAGE_GEN_PROVIDERS[iprov], image_gen["api_key"])
        config_yaml += f"image_gen:\n  provider: {iprov}\n"
    # Code-exec SANDBOX: platform E2B by default (module default TERMINAL_ENV=e2b +
    # the platform key from the parent env). BYOK Modal/Daytona run on the CUSTOMER'S
    # cloud account — their key, their bill, their isolation boundary. Provider names
    # whitelisted; keys land only in this per-turn subprocess env.
    sandbox = req["config"].get("sandbox") or {}
    sprov = sandbox.get("provider")
    if sprov == "modal" and sandbox.get("token_id") and sandbox.get("token_secret"):
        os.environ["TERMINAL_ENV"] = "modal"
        os.environ["TERMINAL_MODAL_MODE"] = "direct"  # never the Nous-managed gateway
        _inject_env("MODAL_TOKEN_ID", sandbox["token_id"])
        _inject_env("MODAL_TOKEN_SECRET", sandbox["token_secret"])
        _scrub_platform_sandbox_keys()
    elif sprov == "daytona" and sandbox.get("api_key"):
        os.environ["TERMINAL_ENV"] = "daytona"
        _inject_env("DAYTONA_API_KEY", sandbox["api_key"])
        _scrub_platform_sandbox_keys()

    # Optional DEDICATED VISION model (auxiliary.vision). Only used when the main model
    # can't see images — multimodal main models attach images natively, no aux needed.
    # The explicit base_url+api_key takes Hermes' direct-endpoint path (auxiliary_client
    # _resolve_task_provider_model), bypassing its credential pool. SECURITY: the vision
    # base_url is held to the SAME registry allow-list as the main model — an off-registry
    # aux endpoint is a host-side credential/transcript exfil vector just like the main one.
    vision = req["config"].get("vision_model") or {}
    v_base = vision.get("base_url")
    if vision.get("model") and vision.get("api_key") and v_base in set(_PROVIDER_BASE_URLS.values()):
        config_yaml += (
            "auxiliary:\n"
            "  vision:\n"
            f"    model: {json.dumps(vision['model'])}\n"
            f"    base_url: {json.dumps(v_base)}\n"
            f"    api_key: {json.dumps(vision['api_key'])}\n"
        )
    # H26: config.yaml carries the MCP bearer — owner-only (0600) from creation.
    fd = os.open(os.path.join(_HOME, "config.yaml"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(config_yaml)
    mem = req.get("memory", {})
    with open(os.path.join(_HOME, "memories", "MEMORY.md"), "w") as f:
        f.write(mem.get("memory_md", ""))
    with open(os.path.join(_HOME, "memories", "USER.md"), "w") as f:
        f.write(mem.get("user_md", ""))


def _read_memory() -> dict:
    def _r(name):
        try:
            with open(os.path.join(_HOME, "memories", name)) as f:
                return f.read()
        except OSError:
            return ""
    return {"memory_md": _r("MEMORY.md"), "user_md": _r("USER.md")}


def run_turn(req: dict) -> dict:
    cfg = req["config"]
    _check_mcp_endpoint(cfg.get("mcp"))  # H25: refuse before anything touches disk
    llm = cfg["llm"]
    provider = llm["provider"]
    base_url = llm.get("base_url") or _PROVIDER_BASE_URLS.get(provider)
    allowed = set(_PROVIDER_BASE_URLS.values())
    # Off-registry base_url is a credential/transcript-exfil vector (the LLM call
    # originates from THIS process, outside the sandbox egress floor). The extra-URL
    # env is honored ONLY in the gated local/no-IAM mode; prod ignores it entirely.
    if os.environ.get("RUNTIME_ALLOW_LOCAL_NO_IAM") == "1":
        extra = os.environ.get("RUNTIME_EXTRA_BASE_URLS", "")
        allowed |= {u.strip() for u in extra.split(",") if u.strip()}
    if base_url not in allowed:
        raise ValueError(f"refusing off-registry base_url for provider {provider!r}")

    _materialize_home(req)
    _assert_sandbox_env_contained()  # H22: no unrecorded credential reaches agent start

    # Import only now — HERMES_HOME is fixed.
    from run_agent import AIAgent
    from tools.mcp_tool import discover_mcp_tools

    discover_mcp_tools()

    limits = cfg.get("limits", {})
    session = req.get("session", {})

    # Map logical tool names (contract) -> Hermes toolsets:
    #   "mcp"  -> the MCP server name ("insightfulpipe", see _materialize_home)
    #   "code" -> "terminal" (code-exec, backed by the E2B sandbox)
    mcp_on = bool(req["config"].get("mcp", {}).get("endpoint"))
    toolsets: list[str] = []
    # No "clarify" default: there is no synchronous human in an async turn, so the
    # tool can never resolve — advertising it just burns iterations.
    for t in cfg.get("tools", ["memory"]):
        if t == "mcp":
            if mcp_on:
                toolsets.append("insightfulpipe")
        elif t == "code":
            toolsets.append("terminal")
        else:
            toolsets.append(t)

    agent = AIAgent(
        model=cfg["model"],
        api_key=llm["api_key"],
        base_url=base_url,
        provider=provider if provider in _HERMES_KNOWN_PROVIDERS else None,
        enabled_toolsets=toolsets,
        max_iterations=int(limits.get("max_iterations", 90)),  # stock Hermes default (parity)
        ephemeral_system_prompt=session.get("system_prompt") or None,
        quiet_mode=True,
        skip_context_files=True,
        session_db=None,
        save_trajectories=False,
    )

    result = agent.run_conversation(
        req["message"]["text"],
        conversation_history=session.get("history") or [],
        task_id=session.get("id") or uuid.uuid4().hex,
    )
    try:
        agent.close()
    except Exception:
        pass

    return {
        "contract_version": "1",
        "turn_id": req["turn_id"],
        "status": "ok",
        "reply_text": result.get("final_response", ""),
        "messages": result.get("messages", []),
        "memory": _read_memory(),
        "usage": {
            "input_tokens": result.get("input_tokens", 0),
            "output_tokens": result.get("output_tokens", 0),
            "api_calls": result.get("api_calls", 0),
        },
    }


def main() -> None:
    t0 = time.monotonic()
    req = None
    try:
        try:
            req = json.loads(sys.stdin.read())
            resp = run_turn(req)
        finally:
            _clear_injected_env()  # H22: BYOK keys leave the env the moment the run ends
    except Exception as e:
        msg = _scrub_by_value(str(e), _scrub_set(req))  # H23: by-value, request + parent-env
        resp = {
            "contract_version": "1",
            "turn_id": (req.get("turn_id") if isinstance(req, dict) else None),
            "status": "error",
            "error": {"type": getattr(e, "error_type", type(e).__name__), "message": msg},
        }
        print(f"[turn_worker] error: {msg}", file=sys.stderr)
    resp.setdefault("usage", {})["wall_ms"] = int((time.monotonic() - t0) * 1000)
    # H24 framing: sentinel + ONE result line as the FINAL stdout write. The leading
    # newline closes any unterminated stray output so the sentinel is its own line.
    sys.stdout.write("\n" + RESULT_SENTINEL + "\n" + json.dumps(resp) + "\n")
    sys.stdout.flush()
    # If WE created the ephemeral home (standalone/dev), delete it — it holds the
    # config.yaml with the MCP bearer. The parent path is cleaned by server.py.
    if not _PARENT_PROVIDED_HOME:
        import shutil

        shutil.rmtree(_HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
