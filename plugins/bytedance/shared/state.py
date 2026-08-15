"""Profile-scoped SQLite state store with migrations.

Per the design spec §7.4:
- Profile-scoped SQLite in WAL mode, with migrations and bounded retention.
- webhook_event: composite idempotency key
- conversation: capability snapshots
- publish_intent: immutable approval record
- outbound_operation: provider request ID reconciliation

Retention defaults:
- webhook metadata/digest: 7 days
- conversation capability snapshots: current + 24h history
- publish audit records: 1 year (configurable)
- message content: no durable copy unless explicitly enabled
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Schema version — bumped when migrations are added
_SCHEMA_VERSION = 1

_MIGRATIONS: dict[int, list[str]] = {
    1: [
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS webhook_event (
            profile TEXT NOT NULL,
            route TEXT NOT NULL,
            provider TEXT NOT NULL,
            account_alias TEXT NOT NULL,
            event_id TEXT NOT NULL,
            raw_sha256 TEXT,
            received_at REAL NOT NULL,
            processing_state TEXT NOT NULL DEFAULT 'pending',
            last_error TEXT,
            PRIMARY KEY (profile, route, provider, account_alias, event_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS conversation (
            profile TEXT NOT NULL,
            provider TEXT NOT NULL,
            account_alias TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            peer_id TEXT,
            display_name TEXT,
            last_message_at REAL,
            capability_json TEXT,
            capability_expires_at REAL,
            PRIMARY KEY (profile, provider, account_alias, conversation_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS publish_intent (
            intent_id TEXT PRIMARY KEY,
            profile TEXT NOT NULL,
            provider TEXT NOT NULL,
            account_alias TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            preview_json TEXT,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            approved_at REAL,
            committed_at REAL,
            state TEXT NOT NULL,
            provider_job_id TEXT,
            provider_status TEXT,
            last_error TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS outbound_operation (
            operation_id TEXT PRIMARY KEY,
            profile TEXT NOT NULL,
            provider TEXT NOT NULL,
            account_alias TEXT NOT NULL,
            target_id TEXT,
            operation_type TEXT NOT NULL,
            payload_sha256 TEXT,
            provider_request_id TEXT,
            state TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS message_outbound (
            profile TEXT NOT NULL,
            provider TEXT NOT NULL,
            account_alias TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            sender_is_self INTEGER NOT NULL,
            text TEXT,
            sent_at REAL,
            PRIMARY KEY (profile, provider, account_alias, conversation_id, message_id)
        )
        """,
        # Seed schema version
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', '1')",
    ],
}


@dataclass
class WebhookEventRecord:
    profile: str
    route: str
    provider: str
    account_alias: str
    event_id: str
    raw_sha256: Optional[str] = None
    received_at: float = 0.0
    processing_state: str = "pending"
    last_error: Optional[str] = None


@dataclass
class PublishIntentRecord:
    intent_id: str
    profile: str
    provider: str
    account_alias: str
    actor_id: str
    payload_json: str
    payload_sha256: str
    preview_json: Optional[str]
    created_at: float
    expires_at: float
    approved_at: Optional[float]
    committed_at: Optional[float]
    state: str
    provider_job_id: Optional[str]
    provider_status: Optional[str]
    last_error: Optional[str]


class StateStore:
    """Profile-scoped SQLite store with WAL mode and migrations.

    Each profile gets its own database file (or a shared file with
    profile-scoped tables — we use separate files for isolation).
    """

    def __init__(self, *, db_dir: Optional[Path] = None) -> None:
        if db_dir is None:
            from hermes_constants import get_hermes_home
            try:
                db_dir = Path(get_hermes_home()) / "bytedance_state"
            except Exception:
                db_dir = Path.home() / ".hermes" / "bytedance_state"
        self._db_dir = db_dir
        self._db_dir.mkdir(parents=True, exist_ok=True)
        self._connections: Dict[str, sqlite3.Connection] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._init_lock = threading.Lock()

    def _db_path(self, profile: str) -> Path:
        # Sanitize profile name for filesystem use
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in profile)
        return self._db_dir / f"{safe}.db"

    def _get_conn(self, profile: str) -> sqlite3.Connection:
        with self._init_lock:
            if profile not in self._connections:
                path = self._db_path(profile)
                conn = sqlite3.connect(
                    str(path),
                    isolation_level=None,  # autocommit — we manage transactions
                    check_same_thread=False,
                )
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA busy_timeout=5000")
                self._connections[profile] = conn
                self._locks[profile] = threading.Lock()
                self._run_migrations(conn)
            return self._connections[profile]

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        """Apply all pending schema migrations."""
        current = self._get_schema_version(conn)
        for target_version in sorted(_MIGRATIONS.keys()):
            if target_version <= current:
                continue
            logger.info("Applying migration v%d", target_version)
            with conn:
                for stmt in _MIGRATIONS[target_version]:
                    conn.execute(stmt)
            self._set_schema_version(conn, target_version)

    def _get_schema_version(self, conn: sqlite3.Connection) -> int:
        try:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='version'"
            ).fetchone()
            if row:
                return int(row[0])
        except sqlite3.OperationalError:
            pass
        return 0

    def _set_schema_version(self, conn: sqlite3.Connection, version: int) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
            ("version", str(version)),
        )

    @contextmanager
    def _tx(self, profile: str) -> Iterator[sqlite3.Connection]:
        """Acquire a connection and begin a transaction."""
        conn = self._get_conn(profile)
        lock = self._locks[profile]
        lock.acquire()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            lock.release()

    # ------------------------------------------------------------------
    # Webhook event idempotency
    # ------------------------------------------------------------------

    def insert_webhook_event(
        self,
        profile: str,
        route: str,
        provider: str,
        account_alias: str,
        event_id: str,
        *,
        raw_sha256: Optional[str] = None,
    ) -> bool:
        """Insert a webhook event atomically.

        Returns True if inserted (new event), False if duplicate
        (composite key already existed).
        """
        received_at = time.time()
        with self._tx(profile) as conn:
            try:
                conn.execute(
                    """INSERT INTO webhook_event
                       (profile, route, provider, account_alias, event_id,
                        raw_sha256, received_at, processing_state)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
                    (profile, route, provider, account_alias, event_id,
                     raw_sha256, received_at),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def update_webhook_state(
        self,
        profile: str,
        provider: str,
        account_alias: str,
        event_id: str,
        state: str,
        *,
        error: Optional[str] = None,
    ) -> None:
        """Update processing state of a webhook event."""
        with self._tx(profile) as conn:
            conn.execute(
                """UPDATE webhook_event
                   SET processing_state = ?, last_error = ?
                   WHERE profile = ? AND provider = ?
                     AND account_alias = ? AND event_id = ?""",
                (state, error, profile, provider, account_alias, event_id),
            )

    def get_unprocessed_events(
        self,
        profile: str,
        provider: str,
        account_alias: str,
        *,
        limit: int = 100,
    ) -> List[Tuple[str, str, str]]:
        """Return (event_id, raw_sha256, route) for pending events after restart."""
        with self._tx(profile) as conn:
            rows = conn.execute(
                """SELECT event_id, raw_sha256, route FROM webhook_event
                   WHERE profile = ? AND provider = ? AND account_alias = ?
                     AND processing_state = 'pending'
                   ORDER BY received_at ASC
                   LIMIT ?""",
                (profile, provider, account_alias, limit),
            ).fetchall()
        return rows

    def prune_webhook_events(self, profile: str, older_than_seconds: int = 7 * 86400) -> int:
        """Delete webhook event rows older than the retention window."""
        cutoff = time.time() - older_than_seconds
        with self._tx(profile) as conn:
            cur = conn.execute(
                "DELETE FROM webhook_event WHERE received_at < ?", (cutoff,)
            )
            return cur.rowcount

    # ------------------------------------------------------------------
    # Conversation capability snapshots
    # ------------------------------------------------------------------

    def upsert_conversation(
        self,
        profile: str,
        provider: str,
        account_alias: str,
        conversation_id: str,
        *,
        peer_id: Optional[str] = None,
        display_name: Optional[str] = None,
        last_message_at: Optional[float] = None,
        capability_json: Optional[str] = None,
        capability_expires_at: Optional[float] = None,
    ) -> None:
        with self._tx(profile) as conn:
            conn.execute(
                """INSERT INTO conversation
                   (profile, provider, account_alias, conversation_id,
                    peer_id, display_name, last_message_at,
                    capability_json, capability_expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(profile, provider, account_alias, conversation_id)
                   DO UPDATE SET
                     peer_id = excluded.peer_id,
                     display_name = excluded.display_name,
                     last_message_at = excluded.last_message_at,
                     capability_json = excluded.capability_json,
                     capability_expires_at = excluded.capability_expires_at""",
                (profile, provider, account_alias, conversation_id,
                 peer_id, display_name, last_message_at,
                 capability_json, capability_expires_at),
            )

    def get_conversation_capability(
        self,
        profile: str,
        provider: str,
        account_alias: str,
        conversation_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the cached capability snapshot if not expired."""
        with self._tx(profile) as conn:
            row = conn.execute(
                """SELECT capability_json, capability_expires_at
                   FROM conversation
                   WHERE profile = ? AND provider = ? AND account_alias = ?
                     AND conversation_id = ?""",
                (profile, provider, account_alias, conversation_id),
            ).fetchone()
        if row is None:
            return None
        capability_json, expires_at = row
        if expires_at and time.time() >= expires_at:
            return None  # expired
        if capability_json:
            return json.loads(capability_json)
        return None

    # ------------------------------------------------------------------
    # Publish intent (approval ledger)
    # ------------------------------------------------------------------

    def create_publish_intent(
        self,
        intent_id: str,
        profile: str,
        provider: str,
        account_alias: str,
        actor_id: str,
        payload_json: str,
        payload_sha256: str,
        preview_json: Optional[str],
        expires_at: float,
    ) -> None:
        now = time.time()
        with self._tx(profile) as conn:
            conn.execute(
                """INSERT INTO publish_intent
                   (intent_id, profile, provider, account_alias, actor_id,
                    payload_json, payload_sha256, preview_json,
                    created_at, expires_at, approved_at, committed_at,
                    state, provider_job_id, provider_status, last_error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL,
                    'DRAFT', NULL, NULL, NULL)""",
                (intent_id, profile, provider, account_alias,
                 actor_id, payload_json, payload_sha256, preview_json,
                 now, expires_at),
            )

    def get_publish_intent(
        self, profile: str, intent_id: str
    ) -> Optional[PublishIntentRecord]:
        with self._tx(profile) as conn:
            cursor = conn.execute(
                "SELECT * FROM publish_intent WHERE intent_id = ? AND profile = ?",
                (intent_id, profile),
            )
            row = cursor.fetchone()
            cols = [d[0] for d in cursor.description]
        if row is None:
            return None
        return PublishIntentRecord(**dict(zip(cols, row)))

    def update_publish_intent(
        self,
        profile: str,
        intent_id: str,
        **fields: Any,
    ) -> None:
        """Update arbitrary fields on a publish_intent.

        Supported fields: state, approved_at, committed_at,
        provider_job_id, provider_status, last_error.
        """
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [intent_id, profile]
        with self._tx(profile) as conn:
            conn.execute(
                f"UPDATE publish_intent SET {set_clause} WHERE intent_id = ? AND profile = ?",
                vals,
            )

    # ------------------------------------------------------------------
    # Outbound operation (provider request ID reconciliation)
    # ------------------------------------------------------------------

    def create_outbound_operation(
        self,
        operation_id: str,
        profile: str,
        provider: str,
        account_alias: str,
        operation_type: str,
        target_id: Optional[str],
        payload_sha256: str,
    ) -> None:
        now = time.time()
        with self._tx(profile) as conn:
            conn.execute(
                """INSERT INTO outbound_operation
                   (operation_id, profile, provider, account_alias,
                    target_id, operation_type, payload_sha256,
                    provider_request_id, state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'pending', ?, ?)""",
                (operation_id, profile, provider, account_alias,
                 target_id, operation_type, payload_sha256, now, now),
            )

    def get_outbound_operation(
        self, profile: str, operation_id: str
    ) -> Optional[Dict[str, Any]]:
        with self._tx(profile) as conn:
            row = conn.execute(
                "SELECT * FROM outbound_operation WHERE operation_id = ? AND profile = ?",
                (operation_id, profile),
            ).fetchone()
            cols = [d[0] for d in conn.description]
        if row is None:
            return None
        return dict(zip(cols, row))

    def update_outbound_operation(
        self,
        profile: str,
        operation_id: str,
        **fields: Any,
    ) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [operation_id, profile]
        with self._tx(profile) as conn:
            conn.execute(
                f"UPDATE outbound_operation SET {set_clause} WHERE operation_id = ? AND profile = ?",
                vals,
            )

    # ------------------------------------------------------------------
    # Outbound message echo tracking
    # ------------------------------------------------------------------

    def record_sent_message(
        self,
        profile: str,
        provider: str,
        account_alias: str,
        conversation_id: str,
        message_id: str,
        *,
        text: Optional[str] = None,
    ) -> None:
        now = time.time()
        with self._tx(profile) as conn:
            conn.execute(
                """INSERT OR IGNORE INTO message_outbound
                   (profile, provider, account_alias, conversation_id,
                    message_id, sender_is_self, text, sent_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
                (profile, provider, account_alias, conversation_id,
                 message_id, text, now),
            )

    def is_known_outbound_message(
        self,
        profile: str,
        provider: str,
        account_alias: str,
        conversation_id: str,
        message_id: str,
    ) -> bool:
        """Check if a message_id is one we sent (echo suppression)."""
        with self._tx(profile) as conn:
            row = conn.execute(
                """SELECT 1 FROM message_outbound
                   WHERE profile = ? AND provider = ? AND account_alias = ?
                     AND conversation_id = ? AND message_id = ?""",
                (profile, provider, account_alias, conversation_id, message_id),
            ).fetchone()
        return row is not None

    def close(self) -> None:
        """Close all database connections."""
        for name, conn in list(self._connections.items()):
            try:
                conn.close()
            except Exception:
                pass
        self._connections.clear()
        self._locks.clear()


# Global singleton — initialized per-process
_global_store: Optional[StateStore] = None


def get_state_store() -> StateStore:
    """Return the global StateStore singleton."""
    global _global_store
    if _global_store is None:
        _global_store = StateStore()
    return _global_store
