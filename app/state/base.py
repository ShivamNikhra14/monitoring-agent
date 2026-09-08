"""
app/state/base.py
─────────────────
StateStore protocol — pluggable persistence layer for deduplication.

Swap implementations to move from SQLite → PostgreSQL / Redis / etc.
without changing anything else in the pipeline.
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from app.models.incidents import IncidentCluster, TriageResult


@runtime_checkable
class StateStore(Protocol):
    """
    Async interface for incident deduplication state.

    Lifecycle
    ─────────
    1. ``is_duplicate(fingerprint)`` — check before alerting.
    2. If not duplicate: ``record(fingerprint, cluster, result)`` — persist.
    3. If duplicate: ``increment(fingerprint)`` — update occurrence count.
    """

    async def setup(self) -> None:
        """Initialise the backing store (create tables, open connections, etc.)."""
        ...

    async def is_duplicate(
        self,
        fingerprint: str,
        dedup_window_hours: float = 10.0,
    ) -> bool:
        """
        Return True if an alert with this fingerprint was emitted within
        ``dedup_window_hours`` hours of now.
        """
        ...

    async def record(
        self,
        fingerprint: str,
        cluster: IncidentCluster,
        result: TriageResult,
    ) -> None:
        """Persist a new incident. Called only for the first occurrence."""
        ...

    async def increment(self, fingerprint: str) -> int:
        """
        Increment the occurrence counter for an existing fingerprint.
        Returns the new count.
        """
        ...

    async def get_occurrence_count(self, fingerprint: str) -> int:
        """Return the current occurrence count for a fingerprint (0 if unknown)."""
        ...

    async def teardown(self) -> None:
        """Clean up resources (close DB connections, etc.)."""
        ...
