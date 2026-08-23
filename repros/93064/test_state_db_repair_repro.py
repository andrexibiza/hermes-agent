from __future__ import annotations

import multiprocessing
import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from state_db_repair_repro import (
    EXPECTED_MESSAGES,
    EXPECTED_SESSIONS,
    TARGET_PAGES,
    _leave_hot_wal_row,
    _writer_process,
    break_schema_btree,
    create_incident_scale_db,
    exclusive_repair_guard,
    fingerprint,
    health_error,
    legacy_in_place_strategy2,
    sqlite_snapshot,
    staged_strategy2,
    transactional_promote,
)


@pytest.fixture(scope="session")
def incident_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("incident") / "state.db"
    counts = create_incident_scale_db(path)
    assert counts == {
        "pages": TARGET_PAGES,
        "sessions": EXPECTED_SESSIONS,
        "messages": EXPECTED_MESSAGES,
    }
    break_schema_btree(path)
    assert health_error(path) is not None
    return path


def copy_template(template: Path, tmp_path: Path) -> Path:
    target = tmp_path / "state.db"
    shutil.copy2(template, target)
    return target


def make_small_db(path: Path, journal_mode: str = "delete") -> None:
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.execute("CREATE TABLE sessions(id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO sessions(name) VALUES('seed')")
        conn.execute("INSERT INTO messages(body) VALUES('seed')")
        actual = conn.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0]
        if journal_mode == "wal" and actual != "wal":
            pytest.skip("SQLite/filesystem does not support WAL")
    finally:
        conn.close()


def test_incident_scale_fixture_is_exact(incident_template: Path):
    fp = fingerprint(incident_template)
    assert fp.page_count_from_header == 3048
    assert fp.size_bytes == 3048 * 4096
    assert "malformed database schema" in health_error(incident_template)


def test_pinned_legacy_strategy_mutates_canonical_then_fails(
    incident_template: Path, tmp_path: Path
):
    db = copy_template(incident_template, tmp_path)
    before = fingerprint(db)

    report = legacy_in_place_strategy2(db)

    after = fingerprint(db)
    assert report["repaired"] is False
    assert report["strategy_error"] is not None
    assert before.sha256 != after.sha256
    assert before.page_count_from_header == after.page_count_from_header == 3048


def test_staged_failure_preserves_canonical_bytes(
    incident_template: Path, tmp_path: Path
):
    db = copy_template(incident_template, tmp_path)
    before = fingerprint(db)

    report = staged_strategy2(db)

    assert report["candidate"]["repaired"] is False
    assert report["promoted"] is False
    assert report["canonical_byte_identical"] is True
    assert fingerprint(db) == before
    assert not list(tmp_path.glob("*repair-scratch*"))


def test_successful_candidate_is_promoted_without_inode_replacement(tmp_path: Path):
    db = tmp_path / "state.db"
    candidate = tmp_path / "candidate.db"
    make_small_db(db)
    inode_before = os.stat(db).st_ino

    source = sqlite3.connect(str(db), isolation_level=None)
    try:
        sqlite_snapshot(source, candidate)
    finally:
        source.close()
    with sqlite3.connect(str(candidate)) as conn:
        conn.execute("INSERT INTO sessions(name) VALUES('healed-on-candidate')")
        conn.commit()

    with exclusive_repair_guard(db) as guard:
        transactional_promote(candidate, guard)

    inode_after = os.stat(db).st_ino
    with sqlite3.connect(str(db)) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sessions")}
    assert inode_after == inode_before
    assert "healed-on-candidate" in names


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_writer_racing_staging_cannot_be_silently_overwritten(
    tmp_path: Path, journal_mode: str
):
    db = tmp_path / "state.db"
    candidate = tmp_path / "candidate.db"
    make_small_db(db, journal_mode=journal_mode)

    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    start = ctx.Event()
    result = ctx.Queue()
    writer = ctx.Process(
        target=_writer_process,
        args=(str(db), ready, start, result, 5.0),
    )
    writer.start()
    assert ready.wait(10)

    with exclusive_repair_guard(db) as guard:
        sqlite_snapshot(guard, candidate)
        with sqlite3.connect(str(candidate)) as conn:
            conn.execute("INSERT INTO sessions(name) VALUES('repair-marker')")
            conn.commit()
        start.set()
        # Give the child time to reach its blocked INSERT while the guard is held.
        time.sleep(0.3)
        transactional_promote(candidate, guard)

    writer.join(10)
    assert not writer.is_alive()
    outcome, detail = result.get(timeout=5)
    assert outcome in {"committed", "failed"}, detail

    with sqlite3.connect(str(db)) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sessions")}
        bodies = {row[0] for row in conn.execute("SELECT body FROM messages")}
    assert "repair-marker" in names
    if outcome == "committed":
        assert "committed-after-stage" in bodies


def test_snapshot_includes_committed_hot_wal_frames(tmp_path: Path):
    db = tmp_path / "state.db"
    candidate = tmp_path / "candidate.db"
    make_small_db(db, journal_mode="wal")

    ctx = multiprocessing.get_context("spawn")
    child = ctx.Process(target=_leave_hot_wal_row, args=(str(db),))
    child.start()
    child.join(10)
    assert child.exitcode == 0

    with exclusive_repair_guard(db) as guard:
        sqlite_snapshot(guard, candidate)

    with sqlite3.connect(str(candidate)) as conn:
        bodies = {row[0] for row in conn.execute("SELECT body FROM messages")}
    assert "committed-wal-row" in bodies


def test_interrupted_promotion_rolls_back_destination(tmp_path: Path):
    db = tmp_path / "state.db"
    candidate = tmp_path / "candidate.db"
    make_small_db(db)

    with sqlite3.connect(str(db), isolation_level=None) as source:
        sqlite_snapshot(source, candidate)
    with sqlite3.connect(str(candidate)) as conn:
        conn.executemany(
            "INSERT INTO messages(body) VALUES(?)",
            [("candidate-row-" + str(i) + "-" + "x" * 4000,) for i in range(100)],
        )
        conn.commit()

    before = fingerprint(db)
    callbacks = 0

    def interrupt(_status: int, remaining: int, total: int) -> None:
        nonlocal callbacks
        callbacks += 1
        if total > 1 and remaining < total:
            raise RuntimeError("simulated interruption")

    with pytest.raises(RuntimeError, match="simulated interruption"):
        with exclusive_repair_guard(db) as guard:
            transactional_promote(
                candidate,
                guard,
                progress=interrupt,
                pages_per_step=1,
            )

    assert callbacks >= 1
    assert fingerprint(db) == before
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone() == (1,)
