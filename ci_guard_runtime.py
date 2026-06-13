#!/usr/bin/env python3
"""CI guard for the Hosted Agents runtime security invariants (INV-RT-1).

Run in CI: `python ci_guard_runtime.py` — exits non-zero if any invariant breaks.

What this enforces (the round-3-correct version): the message-expansion exploit
surface must be UNREACHABLE from the turn path, and the plugin runtime must stay
INERT. It does NOT require zero gateway modules (benign plumbing like session
keying / Telegram chunking is fine) — only that:
  1. the @file/@git/@url + inline-shell preprocessors are never loaded,
  2. the gateway runner (gateway.run, which calls them) + cli are never loaded,
  3. PluginManager has ZERO hooks (discover_and_load never ran).

Plus the hardening gates (cluster E):
  4. gate-16: server._child_env() carries no secret-shaped key beyond E2B_API_KEY,
  5. tools.url_safety keeps its cloud-metadata blocklist floor (regression pin),
  6. the H24 framing sentinel exists in BOTH server.py and turn_worker.py, identical.
"""
import os
import re
import sys
import tempfile

os.environ.setdefault("HERMES_HOME", tempfile.mkdtemp(prefix="ci-"))

# Import exactly what turn_worker imports to run a turn.
import run_agent  # noqa: F401
from run_agent import AIAgent  # noqa: F401
import model_tools  # noqa: F401
from tools.mcp_tool import discover_mcp_tools  # noqa: F401
from tools.environments.e2b import E2BEnvironment  # noqa: F401

failures = []

# 1) message-expansion preprocessors must NOT be loaded
prepro = sorted(m for m in sys.modules if "context_references" in m or "skill_preprocessing" in m)
if prepro:
    failures.append(f"preprocessor modules reachable from turn path: {prepro}")

# 2) the dangerous callers must NOT be loaded
bad_callers = sorted(m for m in sys.modules if m in ("cli", "gateway.run") or m.startswith("cli."))
if bad_callers:
    failures.append(f"dangerous caller modules loaded: {bad_callers}")

# 3) plugin runtime must be inert (no hooks)
import hermes_cli.plugins as hp

mgr = None
for attr in ("get_plugin_manager", "PLUGIN_MANAGER", "plugin_manager", "_MANAGER"):
    if hasattr(hp, attr):
        v = getattr(hp, attr)
        mgr = v() if callable(v) else v
        break
hooks = getattr(mgr, "_hooks", getattr(mgr, "hooks", None)) if mgr else None
n_hooks = len(hooks) if hooks else 0
if n_hooks:
    failures.append(f"plugin hooks registered ({n_hooks}) — discover_and_load must not run in the turn path")

# 4) gate-16 — spawn shape: the child env must carry NO secret-shaped key beyond
# the allowlisted E2B_API_KEY passthrough (tenant secrets travel via stdin only).
os.environ.setdefault("RUNTIME_JWT_PUBLIC_KEY", "ci-dummy-public-key")  # server requires it at import
os.environ.setdefault("E2B_API_KEY", "e2b_ci_dummy")  # exercise the passthrough branch
import server

_SECRET_KEY_RE = re.compile(r"(?i)(api_key|token|secret|password)")
_child_env = server._child_env(tempfile.mkdtemp(prefix="ci-home-"))
_bad_keys = sorted(k for k in _child_env if _SECRET_KEY_RE.search(k) and k != "E2B_API_KEY")
if _bad_keys:
    failures.append(f"gate-16: secret-shaped keys in child env beyond the E2B_API_KEY allowlist: {_bad_keys}")
if "E2B_API_KEY" not in _child_env:
    failures.append("gate-16: E2B_API_KEY passthrough missing — server._child_env allowlist drifted")

# 5) url_safety regression pin — the metadata blocklist floor must stay intact
import ipaddress

from tools import url_safety

if ipaddress.ip_address("169.254.169.254") not in url_safety._ALWAYS_BLOCKED_IPS:
    failures.append("url_safety: 169.254.169.254 missing from _ALWAYS_BLOCKED_IPS")
if "metadata.google.internal" not in url_safety._BLOCKED_HOSTNAMES:
    failures.append("url_safety: metadata.google.internal missing from _BLOCKED_HOSTNAMES")

# 6) H24 framing — server + worker must pin the IDENTICAL stdout result sentinel
_EXPECTED_SENTINEL = "===TURN_RESULT_v1==="
_SENTINEL_RE = re.compile(r"""["'](===[A-Za-z0-9_]+===)["']""")
_here = os.path.dirname(os.path.abspath(__file__))
_sentinels = {}
for _name in ("server.py", "turn_worker.py"):
    with open(os.path.join(_here, _name)) as _f:
        _sentinels[_name] = set(_SENTINEL_RE.findall(_f.read()))
    if _EXPECTED_SENTINEL not in _sentinels[_name]:
        failures.append(f"framing: {_name} does not define the result sentinel {_EXPECTED_SENTINEL}")
if _sentinels["server.py"] != _sentinels["turn_worker.py"]:
    failures.append(
        f"framing: sentinel sets diverge — server.py={sorted(_sentinels['server.py'])} "
        f"turn_worker.py={sorted(_sentinels['turn_worker.py'])}"
    )

if failures:
    print("INV-RT-1 GUARD FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print(
    "INV-RT-1 guard PASSED: preprocessors unreachable, gateway.run/cli not loaded, plugin hooks=0, "
    "child env clean (gate-16), url_safety floor pinned, framing sentinel consistent"
)
