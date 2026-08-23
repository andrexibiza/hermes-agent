#!/usr/bin/env python3
"""Executable forensic reproduction for NousResearch/hermes-agent #93064.

The vulnerable operation mirrors Strategy 2 from
main@f293e7206b4ddd66042329442c6afebc19a8808d:

    PRAGMA writable_schema=ON;
    DELETE FROM sqlite_master WHERE name LIKE 'messages_fts%';
    bump schema cookie;
    PRAGMA writable_schema=OFF;
    COMMIT;
    VACUUM;

The fixed operation snapshots under one live SQLite exclusion guard, runs the
mutating strategy only on the candidate, validates it, and promotes only a
proven-clean candidate through SQLite's transactional backup API.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import multiprocessing
import os
import shutil
import sqlite3
import struct
from pathlib import Path
from typing import Any, Iterator

PAGE_SIZE = 4096
TARGET_PAGES = 3048
EXPECTED_SESSIONS = 29
EXPECTED_MESSAGES = 2537


@dataclasses.dataclass(frozen=True)
class Fingerprint:
    sha256: str
    size_bytes: int
    page_count_from_header: int

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def fingerprint(path: Path) -> Fingerprint:
    data = path.read_bytes()
    pages = struct.unpack(">I", data[28:32])[0] if len(data) >= 32 else 0
    return Fingerprint(
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        page_count_from_header=pages,
    )


def _page_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA page_count").fetchone()[0])


def create_incident_scale_db(path: Path) -> dict[str, int]:
    """Create a deterministic 3,048-page DB with the incident row counts."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(f"PRAGMA page_size={PAGE_SIZE}")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
        conn.executemany(
            "INSERT INTO sessions (name) VALUES (?)",
            [(f"session-{i}",) for i in range(EXPECTED_SESSIONS)],
        )
        # This payload shape lands just below the target on the SQLite build in
        # this environment. A one-page filler then reaches 3,048 exactly.
        conn.executemany(
            "INSERT INTO messages (body) VALUES (?)",
            [(f"message body {i}" * 287,) for i in range(EXPECTED_MESSAGES)],
        )
        conn.commit()
        conn.execute("CREATE TABLE filler (id INTEGER PRIMARY KEY, payload BLOB)")
        conn.commit()

        if _page_count(conn) > TARGET_PAGES:
            raise RuntimeError(
                f"fixture exceeded {TARGET_PAGES} pages before filler: "
                f"{_page_count(conn)}"
            )
        while _page_count(conn) < TARGET_PAGES:
            previous = _page_count(conn)
            conn.execute("INSERT INTO filler(payload) VALUES(zeroblob(3900))")
            conn.commit()
            current = _page_count(conn)
            if current > TARGET_PAGES:
                raise RuntimeError(
                    "fixture filler overshot the target page count: "
                    f"{previous} -> {current}"
                )

        sessions = int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
        messages = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        pages = _page_count(conn)
    finally:
        conn.close()

    assert pages == TARGET_PAGES
    assert sessions == EXPECTED_SESSIONS
    assert messages == EXPECTED_MESSAGES
    assert path.stat().st_size == TARGET_PAGES * PAGE_SIZE
    return {"pages": pages, "sessions": sessions, "messages": messages}


def break_schema_btree(path: Path) -> None:
    """Aim page 1's rightmost child at the final data page.

    This reproduces the ``malformed database schema ()`` corruption class used
    by #87409's deterministic regression fixture.
    """
    data = bytearray(path.read_bytes())
    page_count = struct.unpack(">I", data[28:32])[0]
    if page_count < 3:
        raise RuntimeError("fixture must be multi-page")

    data[100] = 0x05  # page 1 becomes an interior table b-tree page
    struct.pack_into(">H", data, 103, 1)  # one cell
    struct.pack_into(">I", data, 108, page_count)  # rightmost -> data page
    struct.pack_into(">H", data, 112, PAGE_SIZE - 6)  # cell pointer
    struct.pack_into(">I", data, PAGE_SIZE - 6, page_count)
    path.write_bytes(data)


def health_error(path: Path) -> str | None:
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        if rows != [("ok",)]:
            return "; ".join(str(row[0]) for row in rows[:10])
        # Force schema traversal, not merely a header-level open.
        conn.execute("SELECT name, sql FROM sqlite_master").fetchall()
        return None
    except sqlite3.DatabaseError as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            conn.close()


def _bump_schema_cookie(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    conn.execute(f"PRAGMA schema_version={version + 1}")


def legacy_in_place_strategy2(path: Path) -> dict[str, Any]:
    """Run the pinned vulnerable Strategy 2 directly on *path*."""
    error: str | None = None
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute("DELETE FROM sqlite_master WHERE name LIKE 'messages_fts%'")
        _bump_schema_cookie(conn)
        conn.execute("PRAGMA writable_schema=OFF")
        conn.commit()
        conn.execute("VACUUM")
    except sqlite3.DatabaseError as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()

    probe = health_error(path)
    return {
        "strategy_error": error,
        "health_error": probe,
        "repaired": probe is None,
    }


@contextlib.contextmanager
def exclusive_repair_guard(path: Path) -> Iterator[sqlite3.Connection]:
    """Hold SQLite mutation authority continuously through promotion."""
    conn = sqlite3.connect(str(path), timeout=0.0, isolation_level=None)
    try:
        conn.execute("PRAGMA locking_mode=EXCLUSIVE")
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("ROLLBACK")
        yield conn
    finally:
        try:
            conn.execute("PRAGMA locking_mode=NORMAL")
        except sqlite3.Error:
            pass
        conn.close()


def sqlite_snapshot(
    source_conn: sqlite3.Connection,
    destination: Path,
    *,
    pages_per_step: int = 128,
) -> None:
    """Create a complete SQLite-level snapshot, including committed WAL."""
    destination.unlink(missing_ok=True)
    dest_conn = sqlite3.connect(str(destination), isolation_level=None)
    try:
        source_conn.backup(dest_conn, pages=pages_per_step)
    except BaseException:
        dest_conn.close()
        destination.unlink(missing_ok=True)
        raise
    else:
        dest_conn.close()
        with destination.open("rb") as fh:
            os.fsync(fh.fileno())


def transactional_promote(
    candidate: Path,
    guarded_destination: sqlite3.Connection,
    *,
    progress=None,
    pages_per_step: int = 128,
) -> None:
    """Promote into the existing inode through SQLite's backup transaction."""
    if health_error(candidate) is not None:
        raise sqlite3.DatabaseError("candidate failed pre-promotion validation")
    source = sqlite3.connect(f"file:{candidate}?mode=ro", uri=True, isolation_level=None)
    try:
        source.backup(
            guarded_destination,
            pages=pages_per_step,
            progress=progress,
        )
    finally:
        source.close()


def staged_strategy2(path: Path) -> dict[str, Any]:
    """Run Strategy 2 on a snapshot and promote only if it becomes healthy."""
    before = fingerprint(path)
    scratch = path.with_name(f".{path.name}.{os.getpid()}.repair-scratch")
    candidate_result: dict[str, Any]
    promoted = False
    try:
        with exclusive_repair_guard(path) as guard:
            sqlite_snapshot(guard, scratch)
            candidate_result = legacy_in_place_strategy2(scratch)
            if candidate_result["repaired"]:
                transactional_promote(scratch, guard)
                if health_error(path) is not None:
                    raise sqlite3.DatabaseError("post-promotion validation failed")
                promoted = True
    finally:
        scratch.unlink(missing_ok=True)

    after = fingerprint(path)
    return {
        "candidate": candidate_result,
        "promoted": promoted,
        "canonical_before": before.as_dict(),
        "canonical_after": after.as_dict(),
        "canonical_byte_identical": before == after,
    }


def _writer_process(
    db_path: str,
    ready: multiprocessing.synchronize.Event,
    start: multiprocessing.synchronize.Event,
    result: multiprocessing.queues.Queue,
    timeout: float,
) -> None:
    conn = sqlite3.connect(db_path, timeout=timeout, isolation_level=None)
    try:
        ready.set()
        if not start.wait(10):
            result.put(("not-started", None))
            return
        try:
            conn.execute("INSERT INTO messages(body) VALUES('committed-after-stage')")
            result.put(("committed", None))
        except sqlite3.Error as exc:
            result.put(("failed", str(exc)))
    finally:
        conn.close()


def _leave_hot_wal_row(db_path: str) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("INSERT INTO messages(body) VALUES('committed-wal-row')")
    conn.commit()
    os._exit(0)


def run_reproduction(output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    template = output_dir / "incident-scale-template.db"
    vulnerable = output_dir / "vulnerable-live.db"
    fixed = output_dir / "fixed-live.db"

    for path in (template, vulnerable, fixed):
        path.unlink(missing_ok=True)

    fixture = create_incident_scale_db(template)
    break_schema_btree(template)
    shutil.copy2(template, vulnerable)
    shutil.copy2(template, fixed)

    vulnerable_before = fingerprint(vulnerable)
    vulnerable_result = legacy_in_place_strategy2(vulnerable)
    vulnerable_after = fingerprint(vulnerable)

    fixed_result = staged_strategy2(fixed)

    report = {
        "fixture": fixture,
        "sqlite_version": sqlite3.sqlite_version,
        "vulnerable": {
            "before": vulnerable_before.as_dict(),
            "after": vulnerable_after.as_dict(),
            "canonical_changed_despite_failed_repair": (
                vulnerable_before != vulnerable_after
                and not vulnerable_result["repaired"]
            ),
            **vulnerable_result,
        },
        "fixed": fixed_result,
        "scope_note": (
            "This deterministic synthetic fixture reproduces the destructive "
            "mutation-on-failure invariant at the incident scale. The private "
            "production database's specific 3048-to-113 page collapse cannot "
            "be replayed without that private b-tree image."
        ),
    }
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("./issue-93064-repro-output"),
    )
    args = parser.parse_args()
    report = run_reproduction(args.output_dir)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
