# Hermes Plugin: Sprites

A standalone [Hermes Agent](https://github.com/NousResearch/hermes-agent) terminal backend for [Fly.io Sprites](https://sprites.dev): stateful cloud sandboxes with checkpoint and restore.

This is the plugin extraction of [NousResearch/hermes-agent#93523](https://github.com/NousResearch/hermes-agent/pull/93523), built on the generic terminal-provider API merged in [#94400](https://github.com/NousResearch/hermes-agent/pull/94400). The original backend was authored by [@kylemclaren](https://github.com/kylemclaren); the extraction retains the hardening developed across #93523's review rounds.

## Security and lifecycle contract

- Persistent Sprite identity is scoped to the active Hermes profile and task.
- Names are DNS-bounded to 63 characters; a 48-bit digest of the raw, length-prefixed identity survives truncation.
- Profile resolution fails closed instead of falling into the default profile's live VM.
- Concurrent first creation is race-safe: a loser adopts the exact-name winner.
- Non-persistent sessions mint unique names, never adopt, and delete their Sprite on cleanup.
- Missing or nonpositive command deadlines become a bounded 3600-second SDK deadline; paid commands are never issued unbounded.
- `SPRITES_TOKEN` and the accepted `SPRITE_TOKEN` alias are declared through `strip_env_keys`, so Hermes removes them from model-authored subprocesses.
- Credential-file deletion failures propagate into Hermes' sync transaction rather than silently committing stale remote secrets.

## Requirements

- Python `>=3.11,<3.14`
- Hermes Agent containing [#94400](https://github.com/NousResearch/hermes-agent/pull/94400) (`0484910787df66ee5527d67d102ade80020b54f3`) or later
- `sprites-py>=0.5.0,<0.6`
- A Sprites API token

## Install from a repository checkout

```bash
git clone <plugin-repository-url> ~/.hermes/plugins/sprites
python -m pip install 'sprites-py>=0.5.0,<0.6'
hermes plugins enable sprites
hermes config set terminal.backend sprites
```

Store the token in the active Hermes profile's `.env`:

```bash
SPRITES_TOKEN=...
```

`SPRITE_TOKEN` remains accepted for compatibility. A restricted token scoped to the `hermes-` name prefix is recommended.

Hermes' native installer can be used after this tree is published as its own repository:

```bash
hermes plugins install OWNER/hermes-plugin-sprites
hermes plugins enable sprites
hermes config set terminal.backend sprites
```

## Persistence

`terminal.container_persistent: true` resumes a deterministic Sprite. The default profile retains historical short names such as `hermes-default`; named profiles use `hermes-{display}-{digest12}`.

`terminal.container_persistent: false` creates a run-unique Sprite and deletes it on cleanup. The provider declares `session_isolated_when_nonpersistent = true`, so Hermes also keys independent sessions separately before construction.

Named-profile naming in #93523 is intentionally stronger than the original implementation. Sprites created under older lossy names are not automatically migrated and may continue billing until explicitly deleted.

## Verify

Against an installed current Hermes checkout:

```bash
python -m pip install -e '.[test]'
pytest -m 'not integration'
hermes plugins doctor . --ci
```

Live tests are opt-in and use a run-unique `hermes-test-<uuid>` namespace:

```bash
SPRITES_TOKEN=... pytest -m integration -v
```

## Architecture

The plugin owns only vendor-specific behavior:

- Sprite construction/resume/delete
- profile-qualified identity and name bounds
- SDK execution and timeout translation
- Sprite filesystem synchronization
- provider metadata and setup diagnostics

Hermes core owns dispatch and policy classification through `TerminalEnvironmentProvider`: remote/container path semantics, approval behavior, cache-path translation, session isolation, setup/status/dashboard surfaces, and subprocess secret stripping. No backend-name frozenset or vendor-specific branch is reintroduced into core.

## License and attribution

MIT, matching Hermes Agent. See [`NOTICE`](./NOTICE) for provenance and [`MIGRATION.md`](./MIGRATION.md) for the exact core-to-plugin ownership map.
