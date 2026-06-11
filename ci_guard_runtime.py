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
"""
import os
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

if failures:
    print("INV-RT-1 GUARD FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("INV-RT-1 guard PASSED: preprocessors unreachable, gateway.run/cli not loaded, plugin hooks=0")
