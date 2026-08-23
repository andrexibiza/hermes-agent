# #93064 — destructive `state.db` repair reproduction

This standalone harness reproduces the mutation-on-failure defect reported in NousResearch/hermes-agent#93064 and validates the non-destructive staging/promotion boundary implemented by #87409.

## Fixture

The synthetic database matches the reported incident scale exactly:

- 3,048 pages at 4 KiB per page
- 29 sessions
- 2,537 messages
- page-1 schema-btree corruption producing `malformed database schema ()`

The private production database is not included. Its specific 3,048-to-113-page collapse cannot be replayed without that private b-tree image. The harness instead reproduces the load-bearing defect: the pinned legacy Strategy 2 changes the canonical file and still reports failed recovery.

## Run

```bash
cd repros/93064
python state_db_repair_repro.py --output-dir ./output
python -m pytest -q test_state_db_repair_repro.py
```

The harness uses only Python's standard library; the test suite additionally requires `pytest`.

## Expected evidence

The vulnerable path runs the former in-place sequence:

```sql
PRAGMA writable_schema=ON;
DELETE FROM sqlite_master WHERE name LIKE 'messages_fts%';
PRAGMA writable_schema=OFF;
COMMIT;
VACUUM;
```

Against the deterministic fixture it returns `repaired=false`, raises SQLite corruption errors, and changes the canonical SHA-256.

The staged path snapshots under one retained SQLite exclusion guard, mutates only the candidate, and promotes only a proven-clean candidate through SQLite's transactional backup API. For the same failed repair, the canonical SHA-256 remains byte-identical.

The eight tests also cover committed hot-WAL frames, writer races in DELETE and WAL modes, inode-preserving promotion, and transactional rollback when promotion is interrupted.

`report.json` records the exact observed hashes from the reference run on SQLite 3.46.1.