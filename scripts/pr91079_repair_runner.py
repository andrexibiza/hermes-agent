#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
from pathlib import Path

TARGET_BRANCH = "fix/windows-desktop-pack-transaction"
OWNED = [
    "apps/desktop/package.json",
    "apps/desktop/scripts/before-pack-recovery.mjs",
    "apps/desktop/scripts/before-pack.mjs",
    "apps/desktop/scripts/before-pack.test.mjs",
    "apps/desktop/scripts/desktop-builder-runtime.mjs",
    "apps/desktop/scripts/desktop-pack-recovery-composition.test.mjs",
    "apps/desktop/scripts/desktop-pack-transaction.mjs",
    "apps/desktop/scripts/desktop-pack-transaction.test.mjs",
    "apps/desktop/scripts/run-electron-builder.mjs",
    "apps/desktop/scripts/stage-native-deps-recovery.mjs",
    "apps/desktop/scripts/stage-native-deps-recovery.test.mjs",
    "apps/desktop/vitest.config.ts",
    "tests/hermes_cli/test_desktop_pack_transaction_windows.py",
    "hermes_cli/main.py",
    "tests/hermes_cli/test_desktop_exe_integrity.py",
]


def run(*args: str, capture: bool = False) -> str:
    result = subprocess.run(args, check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else ""


def patch_product() -> None:
    source_path = Path("hermes_cli/main.py")
    source = source_path.read_text(encoding="utf-8")
    start_marker = "def _rollback_desktop_from_backup(packaged_executable: Path) -> Optional[Path]:\n"
    end_marker = "\ndef _ensure_desktop_exe_launchable(\n"
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    replacement = '''def _rollback_desktop_from_backup(packaged_executable: Path) -> Optional[Path]:
    """Restore the previous unpacked desktop app from its ``.bak`` tree.

    Returns the restored executable path, or ``None`` when no usable backup
    exists or the filesystem transaction cannot complete. The invalid candidate
    is kept alongside as ``<unpacked-dir>.corrupt`` after a successful rollback.

    The live path is never deleted to make room for the backup. A stale
    quarantine must be retired before either generation moves; the candidate is
    then quarantined by rename, and backup promotion owns the commit point. If
    promotion fails, the candidate is moved back to the live path while the
    backup remains intact. Best-effort: never raises.
    """
    unpacked = packaged_executable.parent
    backup_dir = _desktop_backup_unpacked_dir(packaged_executable)
    backup_exe = backup_dir / packaged_executable.name
    if not backup_exe.exists():
        return None
    if _desktop_exe_integrity_error(backup_exe) is not None:
        return None

    corrupt_dir = unpacked.parent / (unpacked.name + ".corrupt")
    marker_path = backup_dir.with_name(backup_dir.name + ".session")

    if corrupt_dir.exists():
        try:
            shutil.rmtree(corrupt_dir)
        except FileNotFoundError:
            pass
        except OSError:
            return None

    try:
        unpacked.rename(corrupt_dir)
    except OSError:
        return None

    try:
        backup_dir.rename(unpacked)
    except OSError:
        try:
            corrupt_dir.rename(unpacked)
        except OSError:
            # Both generations remain preserved at explicit paths for manual or
            # later recovery; never delete either one here.
            pass
        return None

    try:
        marker_path.unlink()
    except OSError:
        pass

    restored = unpacked / packaged_executable.name
    return restored if restored.exists() else None
'''
    source_path.write_text(source[:start] + replacement + source[end:], encoding="utf-8")

    tests_path = Path("tests/hermes_cli/test_desktop_exe_integrity.py")
    tests = tests_path.read_text(encoding="utf-8")
    sentinel = "def test_rollback_stale_corrupt_cleanup_failure_preserves_both_generations"
    if sentinel not in tests:
        marker = "\n# ─── _ensure_desktop_exe_launchable (the gate)"
        insertion = r'''


def test_rollback_stale_corrupt_cleanup_failure_preserves_both_generations(tmp_path):
    desktop_dir, exe = _win_tree(tmp_path)
    make_pe(exe, PE_AMD64, truncate_to=0x300)
    backup_exe = desktop_dir / "release" / "win-unpacked.bak" / "Hermes.exe"
    make_pe(backup_exe, PE_AMD64)
    corrupt_exe = desktop_dir / "release" / "win-unpacked.corrupt" / "Hermes.exe"
    make_pe(corrupt_exe, PE_AMD64, truncate_to=0x300)

    with patch("hermes_cli.main._windows_native_machine", return_value="AMD64"), \
         patch("hermes_cli.main.shutil.rmtree", side_effect=OSError("quarantine locked")):
        restored = cli_main._rollback_desktop_from_backup(exe)

    assert restored is None
    assert exe.exists()
    assert backup_exe.exists()
    assert corrupt_exe.exists()


def test_rollback_quarantine_rename_failure_preserves_both_generations(tmp_path):
    desktop_dir, exe = _win_tree(tmp_path)
    make_pe(exe, PE_AMD64, truncate_to=0x300)
    backup_exe = desktop_dir / "release" / "win-unpacked.bak" / "Hermes.exe"
    make_pe(backup_exe, PE_AMD64)
    unpacked = exe.parent
    original_rename = Path.rename

    def fail_candidate_rename(self, target):
        if self == unpacked:
            raise OSError("candidate locked")
        return original_rename(self, target)

    with patch("hermes_cli.main._windows_native_machine", return_value="AMD64"), \
         patch.object(Path, "rename", fail_candidate_rename):
        restored = cli_main._rollback_desktop_from_backup(exe)

    assert restored is None
    assert exe.exists()
    assert backup_exe.exists()
    assert not (desktop_dir / "release" / "win-unpacked.corrupt").exists()


def test_rollback_promotion_failure_restores_candidate_and_preserves_backup(tmp_path):
    desktop_dir, exe = _win_tree(tmp_path)
    make_pe(exe, PE_AMD64, truncate_to=0x300)
    backup_dir = desktop_dir / "release" / "win-unpacked.bak"
    backup_exe = backup_dir / "Hermes.exe"
    make_pe(backup_exe, PE_AMD64)
    unpacked = exe.parent
    corrupt_dir = desktop_dir / "release" / "win-unpacked.corrupt"
    original_rename = Path.rename

    def fail_backup_promotion(self, target):
        if self == backup_dir and target == unpacked:
            raise OSError("promotion locked")
        return original_rename(self, target)

    with patch("hermes_cli.main._windows_native_machine", return_value="AMD64"), \
         patch.object(Path, "rename", fail_backup_promotion):
        restored = cli_main._rollback_desktop_from_backup(exe)

    assert restored is None
    assert exe.exists()
    assert backup_exe.exists()
    assert not corrupt_dir.exists()
'''
        tests = tests.replace(marker, insertion + marker, 1)
        tests_path.write_text(tests, encoding="utf-8")

    patched = source_path.read_text(encoding="utf-8")
    assert "shutil.rmtree(unpacked, ignore_errors=True)" not in patched
    assert "corrupt_dir.rename(unpacked)" in patched
    assert "marker_path.unlink()" in patched
    run("python", "-m", "py_compile", "hermes_cli/main.py", "tests/hermes_cli/test_desktop_exe_integrity.py")


def main() -> None:
    original_head = run("git", "rev-parse", "HEAD", capture=True)
    original_parent = run("git", "rev-parse", "HEAD^", capture=True)
    patch_product()

    archive = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "pr91079-files.tar"
    run("tar", "-cf", str(archive), *OWNED)

    subprocess.run(["git", "remote", "add", "upstream", "https://github.com/NousResearch/hermes-agent.git"], check=False)
    run("git", "fetch", "--no-tags", "upstream", "main")

    overlap = run("git", "diff", "--name-only", f"{original_parent}..upstream/main", "--", *OWNED, capture=True)
    if overlap:
        raise SystemExit(f"upstream changed owned path(s):\n{overlap}")

    remote_head = run("git", "ls-remote", "origin", f"refs/heads/{TARGET_BRANCH}", capture=True).split()[0]
    if remote_head != original_head:
        raise SystemExit(f"target branch moved concurrently: expected {original_head}, found {remote_head}")

    run("git", "reset", "--hard", "upstream/main")
    run("tar", "-xf", str(archive))
    run("git", "add", "--", *OWNED)
    changed = run("git", "diff", "--cached", "--name-only", capture=True).splitlines()
    if len(changed) != 15:
        raise SystemExit(f"expected 15 changed files, got {len(changed)}: {changed}")

    run("git", "config", "user.name", "Andrex Ibiza, MBA")
    run("git", "config", "user.email", "andrexibiza@gmail.com")
    run(
        "git", "commit",
        "-m", "fix(desktop): close Windows package rollback transaction (#91079)",
        "-m", "Make Python rollback promotion reversible and add failure-injection coverage while preserving the reviewed package transaction on live upstream main.",
    )
    run(
        "git", "push",
        f"--force-with-lease=refs/heads/{TARGET_BRANCH}:{original_head}",
        "origin", f"HEAD:refs/heads/{TARGET_BRANCH}",
    )
    print(run("git", "rev-parse", "HEAD", capture=True))


if __name__ == "__main__":
    main()
