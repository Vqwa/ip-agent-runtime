"""Headless per-turn entry for InsightfulPipe Hosted Agents.

One invocation = one agent turn. Reads a turn-contract request as JSON on
**stdin** (so secrets never touch env/argv), runs the Hermes agent loop once,
and writes the response as a single JSON line on **stdout**. All logs go to
stderr. Contract: docs/hosted_agents/TURN_CONTRACT.md.

CRITICAL ORDERING: HERMES_HOME and the runtime env are set BEFORE importing
run_agent, because ~30 path constants freeze get_hermes_home() at import.
The FastAPI parent normally sets HERMES_HOME before spawning this process; we
create a fresh ephemeral one if unset so the worker is runnable standalone.
"""

import json
import os
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
        os.environ[_WEB_BACKENDS[backend]] = web["api_key"]
    # Cloud browser — cloud mode, no local Chromium in the image. Browserbase packs
    # {"api_key","project_id"} as JSON in api_key (both required by its provider).
    browser = req["config"].get("browser") or {}
    bprov = browser.get("provider") if browser.get("provider") in _BROWSER_PROVIDERS else "browser-use"
    if browser.get("api_key"):
        if bprov == "browserbase":
            try:
                bb = json.loads(browser["api_key"])
                os.environ["BROWSERBASE_API_KEY"] = bb.get("api_key", "")
                os.environ["BROWSERBASE_PROJECT_ID"] = bb.get("project_id", "")
            except ValueError:
                os.environ["BROWSERBASE_API_KEY"] = browser["api_key"]
        else:
            os.environ[_BROWSER_PROVIDERS[bprov]] = browser["api_key"]
        config_yaml += f"browser:\n  cloud_provider: {bprov}\n"
    # Image generation — Hermes' bundled plugins (fal/krea/openai).
    image_gen = req["config"].get("image_gen") or {}
    iprov = image_gen.get("provider") if image_gen.get("provider") in _IMAGE_GEN_PROVIDERS else "fal"
    if image_gen.get("api_key"):
        os.environ[_IMAGE_GEN_PROVIDERS[iprov]] = image_gen["api_key"]
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
        os.environ["MODAL_TOKEN_ID"] = sandbox["token_id"]
        os.environ["MODAL_TOKEN_SECRET"] = sandbox["token_secret"]
        _scrub_platform_sandbox_keys()
    elif sprov == "daytona" and sandbox.get("api_key"):
        os.environ["TERMINAL_ENV"] = "daytona"
        os.environ["DAYTONA_API_KEY"] = sandbox["api_key"]
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
    with open(os.path.join(_HOME, "config.yaml"), "w") as f:
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
    try:
        req = json.loads(sys.stdin.read())
        resp = run_turn(req)
    except Exception as e:
        resp = {
            "contract_version": "1",
            "turn_id": (req.get("turn_id") if isinstance(locals().get("req"), dict) else None),
            "status": "error",
            "error": {"type": type(e).__name__, "message": str(e)},
        }
        print(f"[turn_worker] error: {e}", file=sys.stderr)
    resp.setdefault("usage", {})["wall_ms"] = int((time.monotonic() - t0) * 1000)
    # Single final JSON line on stdout = the result channel (logs are on stderr).
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()
    # If WE created the ephemeral home (standalone/dev), delete it — it holds the
    # config.yaml with the MCP bearer. The parent path is cleaned by server.py.
    if not _PARENT_PROVIDED_HOME:
        import shutil

        shutil.rmtree(_HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
