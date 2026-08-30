"""One-shot fork-only trigger for the existing staged-patch runner.

This file is temporary and guarded to one historical Actions run. It is never
part of the #98776 product and the carrier branch is restored after publication.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

_RUN_ID = "32293970161"
_CARRIER_SHA = "061bcf3a3cb68e33f73168f8d533b7e369dda5d0"
_SENTINEL = Path("/tmp/pr98776-materializer-started")


def _extract_run_block(text: str) -> str:
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == "run: |"]
    if len(starts) != 1:
        raise RuntimeError(f"expected one run block, found {len(starts)}")
    start = starts[0]
    key_indent = len(lines[start]) - len(lines[start].lstrip())
    content_indent = key_indent + 2
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip():
            indent = len(line) - len(line.lstrip())
            if indent <= key_indent:
                break
            if indent < content_indent:
                raise RuntimeError(f"invalid block indentation: {line!r}")
            body.append(line[content_indent:])
        else:
            body.append("")
    if not body:
        raise RuntimeError("empty carrier run block")
    return "\n".join(body) + "\n"


def _should_run() -> bool:
    return (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("GITHUB_REPOSITORY") == "andrexibiza/hermes-agent"
        and os.environ.get("GITHUB_WORKFLOW") == "Hermes apply staged patch"
        and os.environ.get("GITHUB_RUN_ID") == _RUN_ID
        and not _SENTINEL.exists()
    )


if _should_run():
    # Set before spawning any child Python process so sitecustomize cannot
    # recursively invoke the carrier.
    _SENTINEL.write_text("started\n", encoding="utf-8")
    subprocess.run(
        [
            "git",
            "fetch",
            "--no-tags",
            "origin",
            "refs/heads/fix/memory-prefetch-cancel",
        ],
        check=True,
    )
    carrier_sha = subprocess.check_output(
        ["git", "rev-parse", "FETCH_HEAD"], text=True
    ).strip()
    if carrier_sha != _CARRIER_SHA:
        raise RuntimeError(
            f"carrier moved: expected {_CARRIER_SHA}, found {carrier_sha}"
        )
    carrier = subprocess.check_output(
        [
            "git",
            "show",
            f"{carrier_sha}:.github/workflows/one-shot-fix-88796.yml",
        ],
        text=True,
    )
    script = Path("/tmp/materialize-pr98776.sh")
    script.write_text(_extract_run_block(carrier), encoding="utf-8")
    subprocess.run(["bash", "-n", str(script)], check=True)
    subprocess.run(["bash", str(script)], check=True)
