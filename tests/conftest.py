"""Minimal Hermes host stubs for repository-local unit tests.

CI also runs `hermes plugins doctor . --ci` against a real Hermes checkout;
these stubs isolate vendor behavior without copying core implementation.
"""

from __future__ import annotations

import io
import os
import sys
import types
from pathlib import Path


def _install_host_stubs() -> None:
    try:
        import agent.terminal_env_provider  # noqa: F401
        import tools.environments.base  # noqa: F401
        return
    except ImportError:
        pass

    agent = sys.modules.setdefault("agent", types.ModuleType("agent"))
    agent.__path__ = []

    provider_mod = types.ModuleType("agent.terminal_env_provider")

    class TerminalEnvironmentProvider:
        pass

    provider_mod.TerminalEnvironmentProvider = TerminalEnvironmentProvider
    sys.modules[provider_mod.__name__] = provider_mod
    agent.terminal_env_provider = provider_mod

    secret_mod = types.ModuleType("agent.secret_scope")
    secret_mod.get_secret = lambda key: os.getenv(key)
    sys.modules[secret_mod.__name__] = secret_mod
    agent.secret_scope = secret_mod

    file_safety_mod = types.ModuleType("agent.file_safety")
    file_safety_mod._hermes_root_path = lambda: Path.home() / ".hermes"
    file_safety_mod._hermes_home_path = lambda: Path.home() / ".hermes"
    sys.modules[file_safety_mod.__name__] = file_safety_mod
    agent.file_safety = file_safety_mod

    tools = sys.modules.setdefault("tools", types.ModuleType("tools"))
    tools.__path__ = []
    environments = types.ModuleType("tools.environments")
    environments.__path__ = []
    sys.modules[environments.__name__] = environments
    tools.environments = environments

    base_mod = types.ModuleType("tools.environments.base")

    class BaseEnvironment:
        def __init__(self, cwd: str, timeout: int):
            self.cwd = cwd
            self.timeout = timeout

        def init_session(self):
            return None

    class _ThreadedProcessHandle:
        def __init__(self, execute, cancel_fn=None):
            self._execute = execute
            self._cancel_fn = cancel_fn
            self.stdout = io.StringIO()
            self.returncode = None

        def wait(self):
            if self.returncode is None:
                output, exit_code = self._execute()
                self.stdout = io.StringIO(output)
                self.returncode = exit_code
            return self.returncode

    base_mod.BaseEnvironment = BaseEnvironment
    base_mod._ThreadedProcessHandle = _ThreadedProcessHandle
    sys.modules[base_mod.__name__] = base_mod
    environments.base = base_mod

    sync_mod = types.ModuleType("tools.environments.file_sync")

    class FileSyncManager:
        def __init__(self, get_files_fn, upload_fn, delete_fn):
            self.get_files_fn = get_files_fn
            self.upload_fn = upload_fn
            self.delete_fn = delete_fn
            self.calls = []

        def sync(self, force=False):
            self.calls.append(force)

    sync_mod.FileSyncManager = FileSyncManager
    sync_mod.iter_sync_files = lambda remote_home: []
    sys.modules[sync_mod.__name__] = sync_mod
    environments.file_sync = sync_mod


_install_host_stubs()
