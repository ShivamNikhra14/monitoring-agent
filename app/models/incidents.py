"""
app/models/incidents.py
───────────────────────
Domain models for the stages downstream of log ingestion:
  PreFilterDecision  — output of the rule-based prefilter
  IncidentCluster    — grouped set of related log events
  TriageResult       — LLM triage output (structured JSON)
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field

from app.models.log_event import LogEvent


# ── Pre-filter ─────────────────────────────────────────────────────────────────

class PreFilterResult(str, Enum):
    IGNORE = "IGNORE"
    CANDIDATE = "CANDIDATE"
    IMMEDIATE = "IMMEDIATE"


class PreFilterDecision(BaseModel):
    result: PreFilterResult
    reason: str = Field(description="Human-readable explanation of the decision.")


# ── Incident cluster ───────────────────────────────────────────────────────────

class IncidentCluster(BaseModel):
    """A group of related log events that together represent one incident."""

    incident_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique ID for this cluster.",
    )
    first_seen: datetime = Field(description="Timestamp of the earliest event.")
    last_seen: datetime = Field(description="Timestamp of the latest event.")

    service: Optional[str] = Field(
        default=None,
        description="Logical service name shared by events in this cluster.",
    )
    source: str = Field(
        description="Origin file/module that emitted most events in this cluster."
    )

    events: List[LogEvent] = Field(
        default_factory=list,
        description="All log events that belong to this cluster.",
    )

    # ── Error context ──────────────────────────────────────────────────────────
    primary_error: Optional[str] = Field(
        default=None,
        description="The most descriptive error message extracted from events.",
    )
    traceback_lines: List[str] = Field(
        default_factory=list,
        description="Python traceback lines, in order.",
    )
    exception_type: Optional[str] = Field(
        default=None,
        description="Python exception class name if detected.",
    )

    # ── Request / trace context ────────────────────────────────────────────────
    request_id: Optional[str] = Field(default=None)
    trace_id: Optional[str] = Field(default=None)
    endpoint: Optional[str] = Field(default=None)
    http_method: Optional[str] = Field(default=None)

    @property
    def duration_seconds(self) -> float:
        return (self.last_seen - self.first_seen).total_seconds()

    def context_text(self) -> str:
        """Render a human-readable summary of the cluster for the LLM prompt."""
        lines: List[str] = []
        lines.append(f"Service: {self.service or 'unknown'}")
        lines.append(f"Source: {self.source}")
        if self.endpoint:
            lines.append(f"Endpoint: {self.http_method or ''} {self.endpoint}".strip())
        if self.request_id:
            lines.append(f"Request-ID: {self.request_id}")
        lines.append(
            f"Time range: {self.first_seen.isoformat()} – {self.last_seen.isoformat()}"
        )
        lines.append("")
        lines.append("Log events (in order):")
        for ev in self.events:
            ts = ev.timestamp.strftime("%H:%M:%S")
            lines.append(f"  [{ts}] [{ev.level}] {ev.message}")
        if self.traceback_lines:
            lines.append("")
            lines.append("Traceback:")
            for tb in self.traceback_lines:
                lines.append(f"  {tb}")
        return "\n".join(lines)


# ── Triage result ──────────────────────────────────────────────────────────────

class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Category(str, Enum):
    APPLICATION_ERROR = "APPLICATION_ERROR"
    DATABASE_ERROR = "DATABASE_ERROR"
    DEPENDENCY_ERROR = "DEPENDENCY_ERROR"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
    UNKNOWN = "UNKNOWN"


class TriageResult(BaseModel):
    """Structured output from the LLM triage agent."""

    severity: Severity
    category: Category
    is_actionable: bool = Field(
        description="True when a developer should investigate / act on this."
    )
    summary: str = Field(description="One-sentence summary of the incident.")
    root_cause_hypothesis: str = Field(
        description="Best-effort hypothesis of the root cause, based on evidence."
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="0–1 confidence in the root_cause_hypothesis.",
    )
    evidence: List[str] = Field(
        default_factory=list,
        description="Specific observations from the logs that support the hypothesis.",
    )

    model_config = {"use_enum_values": True}
