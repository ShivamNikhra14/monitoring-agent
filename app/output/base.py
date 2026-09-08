"""
app/output/base.py
──────────────────
AlertOutput protocol — pluggable output interface.

Implement this to add:
  SlackOutput, EmailOutput, WebhookOutput, PagerDutyOutput, DashboardOutput
without changing anything else in the pipeline.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.models.incidents import IncidentCluster, TriageResult


@runtime_checkable
class AlertOutput(Protocol):
    """
    Async interface for alert destinations.

    Parameters passed to ``emit``:
        cluster         — full incident context
        result          — LLM triage result
        occurrence_count — total times this fingerprint has been seen
    """

    async def emit(
        self,
        cluster: IncidentCluster,
        result: TriageResult,
        occurrence_count: int = 1,
    ) -> None:
        """Emit the incident to the destination."""
        ...
