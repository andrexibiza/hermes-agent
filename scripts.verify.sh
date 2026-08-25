#!/usr/bin/env bash
set -euo pipefail
python -m compileall -q __init__.py hermes_plugin_sprites tests
pytest -m 'not integration'
if command -v hermes >/dev/null 2>&1; then
  hermes plugins doctor . --ci
fi
