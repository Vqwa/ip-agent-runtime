"""E2B Firecracker sandbox execution backend (InsightfulPipe Hosted Agents).

Runs the agent's code/shell in a disposable E2B microVM.

SECURITY — differs deliberately from daytona.py / modal.py:
  * NO FileSyncManager. The stock remote backends upload ``~/.hermes`` (which
    contains credentials, skills, cache) INTO the sandbox. We never do that —
    the sandbox receives only data the agent explicitly writes ("data-in,
    credentials-NOT"). There is no token in the sandbox to steal.
  * Egress denied by default (``allow_internet_access=False``) — a malicious or
    prompt-injected script cannot exfiltrate or reach another tenant.
  * Ephemeral: one microVM per environment, killed on cleanup. No persistence.

Selected via ``TERMINAL_ENV=e2b`` (register the case in
tools/terminal_tool.py:_create_environment). Requires the ``e2b`` package
(declare ``terminal.e2b`` in tools/lazy_deps.py LAZY_DEPS, or pre-bake it).
"""

import logging
import shlex

from tools.environments.base import BaseEnvironment, _ThreadedProcessHandle

logger = logging.getLogger(__name__)


class E2BEnvironment(BaseEnvironment):
    """E2B Firecracker microVM backend — ephemeral, egress-off, no credential sync."""

    _stdin_mode = "heredoc"
    _snapshot_timeout = 60

    def __init__(
        self,
        image: str = "base",
        cwd: str = "/home/user",
        timeout: int = 60,
        cpu: int = 1,
        memory: int = 5120,
        disk: int = 10240,
        persistent_filesystem: bool = False,  # hosted agents are always ephemeral
        task_id: str = "default",
        *,
        allow_internet_access: bool = False,  # our no-egress guardrail
        sandbox_lifetime: int = 300,
        # Production: a custom template pre-baking pandas/numpy/etc (faster cold start,
        # our exact libs). The base template has no data stack. Settable via E2B_TEMPLATE.
        template: str | None = None,
    ):
        super().__init__(cwd=cwd, timeout=timeout)
        import os as _os

        template = template or _os.environ.get("E2B_TEMPLATE") or None

        try:
            from tools.lazy_deps import ensure as _lazy_ensure

            _lazy_ensure("terminal.e2b", prompt=False)
        except ImportError:
            pass
        except Exception as e:
            raise ImportError(str(e))

        from e2b import Sandbox

        # One disposable microVM. NB: intentionally no FileSyncManager — no
        # ~/.hermes (no secrets) is ever uploaded here.
        create_kwargs = {"timeout": sandbox_lifetime, "allow_internet_access": allow_internet_access}
        if template:
            create_kwargs["template"] = template
        self._sandbox = Sandbox.create(**create_kwargs)
        self._allow_internet = allow_internet_access
        logger.info(
            "E2B: created sandbox %s (egress=%s)",
            getattr(self._sandbox, "sandbox_id", "?"),
            "on" if allow_internet_access else "OFF",
        )
        # Parity with docker.py/modal.py: snapshot the login shell so env vars and
        # functions persist across terminal calls. Guarded — failure = stateless, not broken.
        try:
            self.init_session()
        except Exception as e:
            logger.warning("E2B: init_session snapshot failed, continuing stateless: %s", e)

    def _before_execute(self) -> None:
        # Ephemeral single-turn sandbox: nothing to sync. A dead/expired sandbox
        # surfaces as a command error, handled in exec_fn.
        return None

    def _run_bash(
        self, cmd_string: str, *, login: bool = False, timeout: int = 120, stdin_data: str | None = None
    ):
        sandbox = self._sandbox

        def cancel():
            try:
                sandbox.kill()
            except Exception:
                pass

        shell_cmd = f"bash -l -c {shlex.quote(cmd_string)}" if login else f"bash -c {shlex.quote(cmd_string)}"

        def exec_fn() -> tuple[str, int]:
            try:
                r = sandbox.commands.run(shell_cmd, timeout=timeout)
                return ((r.stdout or "") + (r.stderr or ""), r.exit_code)
            except Exception as e:
                # E2B raises CommandExitException on non-zero exit; recover the
                # captured output + code so the agent sees the real failure.
                ec = getattr(e, "exit_code", None)
                out = (getattr(e, "stdout", "") or "") + (getattr(e, "stderr", "") or "")
                if ec is not None:
                    return (out or str(e), ec)
                return (f"[e2b exec error] {e}", 1)

        return _ThreadedProcessHandle(exec_fn, cancel_fn=cancel)

    def cleanup(self):
        if getattr(self, "_sandbox", None) is None:
            return
        try:
            self._sandbox.kill()
            logger.info("E2B: killed sandbox")
        except Exception as e:
            logger.warning("E2B: cleanup failed: %s", e)
        self._sandbox = None
