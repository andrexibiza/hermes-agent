#!/usr/bin/env python3
"""Re-materialize PR #80628's two extraction commits on the checked-out main."""
from __future__ import annotations

import argparse
import ast
import pathlib
import subprocess
import sys
from dataclasses import dataclass
from typing import Iterable

ROOT = pathlib.Path(__file__).resolve().parent
GODFILE = pathlib.Path("agent/context_compressor.py")

TEXT_TARGETS = (
    "_redact_compaction_text",
    "_content_text_for_contains",
)

SKILL_TARGETS = (
    "SKILL_PRUNED_MARKER_PREFIX",
    "_SKILL_VIEW_PRUNE_MIN_CHARS",
    "_MAX_PRUNED_SKILL_MARKERS",
    "_SKILL_PRUNED_MARKER_RE",
    "_PRUNED_SKILLS_SECTION_HEADING",
    "_SKILL_PRUNE_RECENT_WINDOW",
    "_skill_pruned_marker",
    "_extract_pruned_skill_names",
    "_collect_ghosted_skill_names",
    "_reinject_pruned_skill_markers",
    "_skill_view_call_sites",
    "_collect_protected_skill_names",
)

TEXT_IMPORT = """from agent.context_compressor_text_utils import (\n    _content_text_for_contains,\n    _redact_compaction_text,\n)\n"""

SKILL_IMPORT = """from agent.context_compressor_skill_prune import (  # noqa: E402\n    SKILL_PRUNED_MARKER_PREFIX,\n    _MAX_PRUNED_SKILL_MARKERS,\n    _PRUNED_SKILLS_SECTION_HEADING,\n    _SKILL_PRUNE_RECENT_WINDOW,\n    _SKILL_PRUNED_MARKER_RE,\n    _SKILL_VIEW_PRUNE_MIN_CHARS,\n    _collect_ghosted_skill_names,\n    _collect_protected_skill_names,\n    _extract_pruned_skill_names,\n    _reinject_pruned_skill_markers,\n    _skill_pruned_marker,\n    _skill_view_call_sites,\n)\n"""


@dataclass(frozen=True)
class NamedNode:
    name: str
    node: ast.AST


def git_show(commit: str, path: str) -> str:
    return subprocess.check_output(
        ["git", "show", f"{commit}:{path}"], text=True
    )


def top_level_named_nodes(source: str) -> dict[str, ast.AST]:
    tree = ast.parse(source)
    result: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            result[node.name] = node
            continue
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    result[target.id] = node
            continue
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            result[node.target.id] = node
    return result


def node_text(source: str, node: ast.AST) -> str:
    lines = source.splitlines(keepends=True)
    start = getattr(node, "lineno", None)
    end = getattr(node, "end_lineno", None)
    if not isinstance(start, int) or not isinstance(end, int):
        raise RuntimeError(f"node lacks line span: {ast.dump(node, include_attributes=False)}")
    return "".join(lines[start - 1 : end])


def replace_named_nodes(scaffold: str, source: str, names: Iterable[str]) -> str:
    scaffold_nodes = top_level_named_nodes(scaffold)
    source_nodes = top_level_named_nodes(source)
    replacements: list[tuple[int, int, str, str]] = []
    scaffold_lines = scaffold.splitlines(keepends=True)
    for name in names:
        old = scaffold_nodes.get(name)
        new = source_nodes.get(name)
        if old is None:
            raise RuntimeError(f"scaffold missing top-level member {name}")
        if new is None:
            raise RuntimeError(f"current godfile missing top-level member {name}")
        start = getattr(old, "lineno") - 1
        end = getattr(old, "end_lineno")
        replacement = node_text(source, new)
        if replacement and not replacement.endswith("\n"):
            replacement += "\n"
        replacements.append((start, end, replacement, name))
    for start, end, replacement, _name in sorted(replacements, reverse=True):
        scaffold_lines[start:end] = [replacement]
    output = "".join(scaffold_lines)
    ast.parse(output)
    return output


def _span_with_adjacent_comments(lines: list[str], node: ast.AST) -> tuple[int, int]:
    start = getattr(node, "lineno") - 1
    end = getattr(node, "end_lineno")

    # Own the immediately preceding blank/comment banner.  Stop at the first
    # real code line; overlapping spans are merged below, so a contiguous
    # extracted cluster is removed as one clean block.
    i = start - 1
    while i >= 0:
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            i -= 1
            continue
        break
    start = i + 1

    # Own trailing blank lines too, then replace the whole removed region with
    # exactly one top-level separator.
    while end < len(lines) and not lines[end].strip():
        end += 1
    return start, end


def remove_named_nodes(source: str, names: Iterable[str]) -> str:
    nodes = top_level_named_nodes(source)
    lines = source.splitlines(keepends=True)
    spans: list[tuple[int, int]] = []
    for name in names:
        node = nodes.get(name)
        if node is None:
            raise RuntimeError(f"godfile missing top-level member {name}")
        spans.append(_span_with_adjacent_comments(lines, node))

    spans.sort()
    merged: list[list[int]] = []
    for start, end in spans:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)

    for start, end in reversed(merged):
        lines[start:end] = ["\n\n"]
    output = "".join(lines)
    ast.parse(output)
    remaining = top_level_named_nodes(output)
    leftovers = [name for name in names if name in remaining]
    if leftovers:
        raise RuntimeError(f"members remained in godfile: {leftovers}")
    return output


def insert_import_after_module(source: str, module: str, block: str) -> str:
    if block.strip() in source:
        return source
    tree = ast.parse(source)
    candidate: ast.ImportFrom | None = None
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == module:
            candidate = node
    if candidate is None:
        raise RuntimeError(f"cannot find import anchor from {module}")
    lines = source.splitlines(keepends=True)
    idx = getattr(candidate, "end_lineno")
    lines[idx:idx] = [block]
    output = "".join(lines)
    ast.parse(output)
    return output


def write_text(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def verify_module_does_not_cycle(path: pathlib.Path) -> None:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "agent.context_compressor":
            raise RuntimeError(f"{path} imports the godfile and creates a cycle")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "agent.context_compressor":
                    raise RuntimeError(f"{path} imports the godfile and creates a cycle")


def apply_text_utils(original_commit: str) -> None:
    source = GODFILE.read_text(encoding="utf-8")
    scaffold = git_show(original_commit, "agent/context_compressor_text_utils.py")
    module = replace_named_nodes(scaffold, source, TEXT_TARGETS)
    source = remove_named_nodes(source, TEXT_TARGETS)
    source = insert_import_after_module(source, "agent.redact", TEXT_IMPORT)

    write_text(pathlib.Path("agent/context_compressor_text_utils.py"), module)
    write_text(GODFILE, source)
    test = git_show(original_commit, "tests/agent/test_context_compressor_text_utils_seam.py")
    write_text(pathlib.Path("tests/agent/test_context_compressor_text_utils_seam.py"), test)
    verify_module_does_not_cycle(pathlib.Path("agent/context_compressor_text_utils.py"))


def apply_skill_prune(original_commit: str) -> None:
    source = GODFILE.read_text(encoding="utf-8")
    scaffold = git_show(original_commit, "agent/context_compressor_skill_prune.py")
    module = replace_named_nodes(scaffold, source, SKILL_TARGETS)
    source = remove_named_nodes(source, SKILL_TARGETS)
    source = insert_import_after_module(
        source, "agent.context_compressor_text_utils", SKILL_IMPORT
    )

    write_text(pathlib.Path("agent/context_compressor_skill_prune.py"), module)
    write_text(GODFILE, source)
    test = git_show(original_commit, "tests/agent/test_context_compressor_skill_prune_seam.py")
    write_text(pathlib.Path("tests/agent/test_context_compressor_skill_prune_seam.py"), test)
    verify_module_does_not_cycle(pathlib.Path("agent/context_compressor_skill_prune.py"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("text-utils", "skill-prune"))
    parser.add_argument("--original-commit", required=True)
    args = parser.parse_args()

    if args.stage == "text-utils":
        apply_text_utils(args.original_commit)
    else:
        apply_skill_prune(args.original_commit)

    # Parse every touched Python file before returning control to the workflow.
    touched = [GODFILE]
    if args.stage == "text-utils":
        touched += [
            pathlib.Path("agent/context_compressor_text_utils.py"),
            pathlib.Path("tests/agent/test_context_compressor_text_utils_seam.py"),
        ]
    else:
        touched += [
            pathlib.Path("agent/context_compressor_skill_prune.py"),
            pathlib.Path("tests/agent/test_compressor_skill_prune_seam.py"),
        ]
    for path in touched:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        print(f"validated {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
