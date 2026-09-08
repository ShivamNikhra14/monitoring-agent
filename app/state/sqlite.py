"""
app/state/sqlite.py
────────────────────
SQLite-backed StateStore for PoC deduplication.

Table schema
────────────
incidents
  fingerprint     TEXT PRIMARY KEY
  first_seen      TEXT  (ISO-8601 UTC)
  last_seen       TEXT  (ISO-8601 UTC)
  alerted_at      TEXT  (ISO-8601 UTC)
  occurrence_count INTEGER DEFAULT 1
  service         TEXT
  summary         TEXT

Fingerprint generation
───────────────────────
SHA-256 of (service + exception_type + normalised_exception_message +
            source_file + endpoint), truncated to 16 hex chars.

Normalisation removes line numbers and dynamic values so the same bug
gets the same fingerprint across deployments.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite

from app.models.incidents import IncidentCluster, TriageResult

logger = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS incidents (
    fingerprint      TEXT PRIMARY KEY,
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL,
    alerted_at       TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    service          TEXT,
    summary          TEXT
);
"""

# Patterns to strip from messages before fingerprinting
_DYNAMIC_RE = re.compile(
    r"(\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\s]*"  # timestamps
    r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"  # UUIDs
    r"|\b\d+\.\d+\.\d+\.\d+\b"  # IP addresses
    r"|\b0x[0-9a-fA-F]+\b"  # hex addresses
    r"|\b\d{5,}\b"  # long numbers (IDs, ports)
    r")",
    re.IGNORECASE,
)


def generate_fingerprint(cluster: IncidentCluster) -> str:
    """
    Produce a stable fingerprint for an IncidentCluster.

    The fingerprint is the same for repeated occurrences of the same bug
    but different for different exception types, endpoints, or source locations.
    """
    def normalise(text: Optional[str]) -> str:
        if not text:
            return ""
        # Remove dynamic values (IDs, timestamps, addresses)
        cleaned = _DYNAMIC_RE.sub("", text)
        # Collapse whitespace
        return " ".join(cleaned.split()).lower()

    parts = [
        normalise(cluster.service),
        normalise(cluster.exception_type),
        normalise(cluster.primary_error),
        normalise(cluster.source),
        normalise(cluster.endpoint),
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class SQLiteStateStore:
    """
    Async SQLite state store.

    Usage::

        store = SQLiteStateStore("monitoring_state.db")
        await store.setup()
        ...
        await store.teardown()
    """

    def __init__(self, db_path: str = "monitoring_state.db") -> None:
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def setup(self) -> None:
        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.execute(_CREATE_TABLE_SQL)
        await self._conn.commit()
        logger.debug("SQLiteStateStore ready at %s", self.db_path)

    async def is_duplicate(
        self,
        fingerprint: str,
        dedup_window_hours: float = 10.0,
    ) -> bool:
        assert self._conn, "Call setup() first"
        cutoff = (
            datetime.now(tz=timezone.utc) - timedelta(hours=dedup_window_hours)
        ).isoformat()
        async with self._conn.execute(
            "SELECT 1 FROM incidents WHERE fingerprint = ? AND alerted_at > ?",
            (fingerprint, cutoff),
        ) as cursor:
            row = await cursor.fetchone()
            return row is not None

    async def record(
        self,
        fingerprint: str,
        cluster: IncidentCluster,
        result: TriageResult,
    ) -> None:
        assert self._conn, "Call setup() first"
        now = datetime.now(tz=timezone.utc).isoformat()
        await self._conn.execute(
            """
            INSERT INTO incidents
                (fingerprint, first_seen, last_seen, alerted_at, occurrence_count, service, summary)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                last_seen = excluded.last_seen,
                alerted_at = excluded.alerted_at,
                occurrence_count = occurrence_count + 1
            """,
            (
                fingerprint,
                cluster.first_seen.isoformat(),
                cluster.last_seen.isoformat(),
                now,
                cluster.service,
                result.summary,
            ),
        )
        await self._conn.commit()

    async def increment(self, fingerprint: str) -> int:
        assert self._conn, "Call setup() first"
        now = datetime.now(tz=timezone.utc).isoformat()
        await self._conn.execute(
            "UPDATE incidents SET occurrence_count = occurrence_count + 1, last_seen = ? WHERE fingerprint = ?",
            (now, fingerprint),
        )
        await self._conn.commit()
        return await self.get_occurrence_count(fingerprint)

    async def get_occurrence_count(self, fingerprint: str) -> int:
        assert self._conn, "Call setup() first"
        async with self._conn.execute(
            "SELECT occurrence_count FROM incidents WHERE fingerprint = ?",
            (fingerprint,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def teardown(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
