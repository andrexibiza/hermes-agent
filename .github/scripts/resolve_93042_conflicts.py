from __future__ import annotations

import re
from pathlib import Path


def resolve_conflict(path: str, ours: str, theirs: str, resolved: str) -> None:
    target = Path(path)
    content = target.read_text(encoding="utf-8")
    pattern = re.compile(
        re.escape("<<<<<<< HEAD\n" + ours + "=======\n" + theirs)
        + r">>>>>>>[^\n]+\n"
    )
    content, count = pattern.subn(resolved, content, count=1)
    if count != 1:
        raise RuntimeError(f"expected conflict shape not found in {path}")
    target.write_text(content, encoding="utf-8")


resolve_conflict(
    "apps/desktop/src/app/skills/index.test.tsx",
    """describe('SkillsView toolset management', () => {
  // This is the first lazy renderer import in the full CI shard; cold module
  // transformation can exceed Vitest's default while the focused test stays fast.
""",
    """// SkillsView is a heavy module: the first test pays the whole dynamic-import
// cost, and the file legitimately runs ~14s on CI runners — right against the
// global 15s per-test budget, so slow runners cascade-fail all 11 tests
// (2× in a row on PR #93612, plus a main run the same hour). Give this file
// headroom; the tests are not slow individually.
describe('SkillsView toolset management', { timeout: 60_000 }, () => {
""",
    """// SkillsView is a heavy lazy renderer: the first test pays the cold module
// transformation cost, and the file can run ~14s on CI runners — right against
// the global 15s per-test budget. Give this file headroom; the focused tests are
// not slow individually.
describe('SkillsView toolset management', { timeout: 60_000 }, () => {
""",
)

resolve_conflict(
    "apps/desktop/src/store/updates.ts",
    """import { applyFleetUpdates, type FleetUpdateResult } from '@/store/fleet-updates'
""",
    """import { reconnectGateway } from '@/store/gateway-reconnect'
""",
    """import { applyFleetUpdates, type FleetUpdateResult } from '@/store/fleet-updates'
import { reconnectGateway } from '@/store/gateway-reconnect'
""",
)

resolve_conflict(
    "plugins/platforms/telegram/adapter.py",
    """import faulthandler
import hashlib
""",
    """""",
    """import faulthandler
import hashlib
""",
)

main_path = Path("hermes_cli/main.py")
main = main_path.read_text(encoding="utf-8")
cmd_update_start = main.index("def cmd_update(args):")
except_start = main.index("    except SystemExit as _update_exit:\n", cmd_update_start)
except_end = main.index("    except BaseException as _update_exc:\n", except_start)
main = (
    main[:except_start]
    + '''    except SystemExit as _update_exit:
        # Receipt boundary (#91283 review): the impl has many early
        # sys.exit paths (concurrent-instance preflight, venv-holder
        # refusal, head-pinned no-op, fetch failure) that never reach an
        # inner finalize. Persist any still-open receipt with the real
        # exit code, then let the exit proceed unchanged. No-op when an
        # inner path already finalized (exactly-once by construction).
        if isinstance(_update_exit.code, int):
            _code = _update_exit.code
        elif _update_exit.code is None:
            _code = 0
        else:
            # Match Python's SystemExit semantics: a non-empty object is an
            # error exit, while a bare sys.exit() is success.
            _code = 1
        try:
            from hermes_cli.update_receipt import finalize_pending_update_receipt

            finalize_pending_update_receipt(_code, f"sys.exit({_code})")
        except Exception:
            pass
        _publish_gateway_update_terminal_status(_code)
        _update_handoff_exit_code = _code
        raise
'''
    + main[except_end:]
)

base_exception_start = main.index("    except BaseException as _update_exc:\n", except_start)
else_start = main.index("    else:\n", base_exception_start)
next_def = main.index("\n\ndef _coalesce_session_name_args", else_start)
main = (
    main[:else_start]
    + '''    else:
        try:
            from hermes_cli.update_receipt import finalize_pending_update_receipt

            finalize_pending_update_receipt(0, "completed at command boundary")
        except Exception:
            pass
        _publish_gateway_update_terminal_status(0)
        _update_handoff_exit_code = 0
    finally:
        _update_lock.release()
        _finalize_update_output(_update_io_state)
        if _external_coordinator:
            schedule_windows_coordinator_cleanup(Path(PROJECT_ROOT))
        # Windows hand-off child (#93581): the re-exec'd venv child cannot
        # rely on graceful interpreter shutdown — a leftover non-daemon
        # thread from the update tail keeps the console busy long after
        # the receipt is durable (success, exit 0, "completed at command
        # boundary"), freezing the PowerShell window for minutes. By this
        # point every durable step is done: the receipt/status is finalized,
        # the install lock is released, coordinator cleanup is scheduled, and
        # stdio is restored. On the re-exec hand-off path only, flush and exit
        # hard instead of waiting for the interpreter to unwind.
        if (
            _update_handoff_exit_code is not None
            and os.environ.get(_UPDATE_REEXEC_ENV) == "1"
        ):
            logger.debug(
                "Update hand-off child %s exiting via os._exit(%s)",
                os.getpid(),
                _update_handoff_exit_code,
            )
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(_update_handoff_exit_code)
'''
    + main[next_def:]
)
main_path.write_text(main, encoding="utf-8")

for path in (
    "apps/desktop/src/app/skills/index.test.tsx",
    "apps/desktop/src/store/updates.ts",
    "hermes_cli/main.py",
    "plugins/platforms/telegram/adapter.py",
):
    content = Path(path).read_text(encoding="utf-8")
    if re.search(r"^(<<<<<<<|=======|>>>>>>>)", content, re.MULTILINE):
        raise RuntimeError(f"unresolved marker remains in {path}")
