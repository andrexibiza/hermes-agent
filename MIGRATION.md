# Extraction map

This repository is the standalone form of the Sprites backend reviewed in [NousResearch/hermes-agent#93523](https://github.com/NousResearch/hermes-agent/pull/93523). It depends on the generic terminal-provider API merged in [#94400](https://github.com/NousResearch/hermes-agent/pull/94400).

## Moved into this plugin

| Former core surface | Standalone owner |
|---|---|
| `tools/environments/sprites.py` | `hermes_plugin_sprites/environment.py` |
| Sprites availability and diagnostics | `SpritesProvider.is_available`, `probe`, `doctor_checks` |
| Setup guidance and SDK requirement | `SpritesProvider.setup_instructions`, `plugin.yaml`, `pyproject.toml` |
| Unit and live regression coverage | `tests/` |

## Intentionally left in Hermes core

Hermes core owns the generic registry and every cross-cutting classification site:

- terminal dispatch and environment construction
- remote/container path semantics
- dangerous-command approval policy
- cache-path translation
- session isolation for non-persistent backends
- setup, status, doctor, and dashboard enumeration
- subprocess secret stripping

The plugin supplies these semantics declaratively through `TerminalEnvironmentProvider`; it does not add a `sprites` branch or backend-name allowlist to core.

## Extraction-specific changes

- Removed the in-core `tools.lazy_deps.ensure("terminal.sprites")` call. Standalone dependencies are declared in `plugin.yaml` and `pyproject.toml`; Hermes Doctor reports missing requirements.
- Added both directory-plugin and pip-entry-point packaging paths.
- Preserved `SPRITE_TOKEN` as an accepted alias without declaring a `requires_env` manifest gate that would reject it before registration.
- Pinned CI actions by commit SHA and tests both the minimum provider-API commit and the extraction-time main commit.
