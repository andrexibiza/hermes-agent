"""Windows search path regressions for native rg invoked from Git Bash."""

from unittest.mock import MagicMock

from tools.environments import local as local_mod
from tools.file_operations import ShellFileOperations


def _ops_with_commands(commands, outputs=None):
    outputs = list(outputs or [])
    env = MagicMock(cwd="C:/Users/davep")

    def execute(command, **kwargs):
        commands.append(command)
        if outputs:
            return outputs.pop(0)
        return {"output": "", "returncode": 0}

    env.execute.side_effect = execute
    return ShellFileOperations(env)


def test_native_search_path_quoting_preserves_drive_path_for_rg(monkeypatch):
    monkeypatch.setattr(local_mod, "_IS_WINDOWS", True)
    ops = ShellFileOperations(MagicMock(cwd="C:/Users/davep"))

    # Bash builtins/coreutils still get the MSYS spelling.
    assert ops._escape_shell_arg("C:/Users/davep/project") == "'/c/Users/davep/project'"

    # Native search executables get a Windows-native argv spelling.
    assert ops._escape_native_tool_arg("C:/Users/davep/project") == "'C:/Users/davep/project'"
    assert ops._escape_native_tool_arg(r"C:\Users\davep\project") == "'C:/Users/davep/project'"
    assert ops._escape_native_tool_arg("/c/Users/davep/project") == "'C:/Users/davep/project'"


def test_rg_content_search_uses_native_windows_path(monkeypatch):
    monkeypatch.setattr(local_mod, "_IS_WINDOWS", True)
    commands = []
    ops = _ops_with_commands(
        commands,
        outputs=[
            {
                "output": "C:/Users/davep/project/hit.txt:1:needle\n",
                "returncode": 0,
            }
        ],
    )

    result = ops._search_with_rg(
        "needle",
        "C:/Users/davep/project",
        file_glob=None,
        limit=50,
        offset=0,
        output_mode="content",
        context=0,
    )

    assert result.error is None
    assert result.total_count == 1
    assert commands
    rg_command = commands[0]
    assert "'C:/Users/davep/project'" in rg_command
    assert "'/c/Users/davep/project'" not in rg_command


def test_rg_file_search_surfaces_path_error_instead_of_false_zero(monkeypatch):
    monkeypatch.setattr(local_mod, "_IS_WINDOWS", True)
    commands = []
    ops = _ops_with_commands(
        commands,
        outputs=[
            {"output": "rg: C:/Users/davep/missing: IO error\n", "returncode": 2},
            {"output": "rg: C:/Users/davep/missing: IO error\n", "returncode": 2},
        ],
    )

    result = ops._search_files_rg("*.py", "C:/Users/davep/missing", limit=50, offset=0)

    assert result.error is not None
    assert "Search failed" in result.error
    assert result.total_count == 0
    assert len(commands) == 2  # sorted attempt, then plain fallback
    assert all("'C:/Users/davep/missing'" in command for command in commands)
    assert all("'/c/Users/davep/missing'" not in command for command in commands)


def test_split_files_only_diagnostics_preserves_whitespace_paths():
    from tools.file_operations import _split_files_only_diagnostics

    diagnostics, payload = _split_files_only_diagnostics(
        "C:/Users/davep/project/my file.py\n"
        "rg: C:/Users/davep/project/unreadable dir: Permission denied\n"
        "regex parse error:\n"
        "    [\n"
        "    ^\n"
        "error: unclosed character class\n"
        "C:/Users/davep/project/other file.txt\n"
    )

    assert payload.split("\n") == [
        "C:/Users/davep/project/my file.py",
        "C:/Users/davep/project/other file.txt",
    ]
    assert "Permission denied" in diagnostics
    assert "unclosed character class" in diagnostics


def test_rg_file_search_preserves_paths_with_whitespace(monkeypatch):
    monkeypatch.setattr(local_mod, "_IS_WINDOWS", True)
    commands = []
    ops = _ops_with_commands(
        commands,
        outputs=[
            {
                "output": "C:/Users/davep/project/my file.py\n"
                          "C:/Users/davep/project/other.txt\n",
                "returncode": 0,
            }
        ],
    )

    result = ops._search_files_rg("*.py", "C:/Users/davep/project", limit=50, offset=0)

    assert result.error is None
    assert result.files == [
        "C:/Users/davep/project/my file.py",
        "C:/Users/davep/project/other.txt",
    ]
    assert result.total_count == 2
    # First attempt succeeded; the empty-result fallback must not run.
    assert len(commands) == 1


def test_rg_file_search_keeps_spaced_matches_alongside_diagnostics(monkeypatch):
    monkeypatch.setattr(local_mod, "_IS_WINDOWS", True)
    commands = []
    ops = _ops_with_commands(
        commands,
        outputs=[
            {
                "output": "C:/Users/davep/project/my file.py\n"
                          "rg: C:/Users/davep/project/unreadable dir: "
                          "Permission denied\n",
                "returncode": 2,
            }
        ],
    )

    result = ops._search_files_rg("*.py", "C:/Users/davep/project", limit=50, offset=0)

    # Partial failure: the readable match is kept, the diagnostic is not a path.
    assert result.error is None
    assert result.files == ["C:/Users/davep/project/my file.py"]
    assert result.total_count == 1
    assert len(commands) == 1


def test_rg_content_files_only_preserves_paths_with_whitespace(monkeypatch):
    monkeypatch.setattr(local_mod, "_IS_WINDOWS", True)
    commands = []
    ops = _ops_with_commands(
        commands,
        outputs=[
            {
                "output": "C:/Users/davep/project/my file.py\n",
                "returncode": 0,
            }
        ],
    )

    result = ops._search_with_rg(
        "needle",
        "C:/Users/davep/project",
        file_glob=None,
        limit=50,
        offset=0,
        output_mode="files_only",
        context=0,
    )

    assert result.error is None
    assert result.files == ["C:/Users/davep/project/my file.py"]
    assert result.total_count == 1
