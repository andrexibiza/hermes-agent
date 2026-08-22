#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one literal match, found {count}")
    write(path, text.replace(old, new, 1))


def sub_once(path: str, pattern: str, replacement: str) -> None:
    text = read(path)
    new, count = re.subn(pattern, replacement, text, count=1, flags=re.DOTALL)
    if count != 1:
        raise RuntimeError(f"{path}: expected one regex match, found {count}")
    write(path, new)


cmd_test = read("tests/hermes_cli/test_cmd_update_docker.py")
cmd_pattern = (
    r'(def test_cmd_update_in_docker_prints_guidance_and_exits\(.*?'
    r'assert excinfo\.value\.code == )1'
)
cmd_test, count = re.subn(cmd_pattern, r'\g<1>2', cmd_test, count=1, flags=re.DOTALL)
if count != 1:
    raise RuntimeError(
        "tests/hermes_cli/test_cmd_update_docker.py: refusal exit assertion not found"
    )
write("tests/hermes_cli/test_cmd_update_docker.py", cmd_test)

web_test = read("tests/hermes_cli/test_web_server.py")
if '"docker_update_unsupported"' not in web_test:
    raise RuntimeError("test_web_server.py: legacy docker error assertion not found")
web_test = web_test.replace(
    '"docker_update_unsupported"',
    '"image_managed_update_refused"',
)
block_pattern = (
    r'(def test_update_hermes_returns_docker_guidance_without_spawning'
    r'.*?)(?=\n    def |\n\nclass |\Z)'
)
match = re.search(block_pattern, web_test, flags=re.DOTALL)
if match:
    block = match.group(1).replace(
        'status_data["exit_code"] == 1',
        'status_data["exit_code"] == 2',
    )
    web_test = web_test[:match.start(1)] + block + web_test[match.end(1):]
write("tests/hermes_cli/test_web_server.py", web_test)

print("phase3 materialization complete")
