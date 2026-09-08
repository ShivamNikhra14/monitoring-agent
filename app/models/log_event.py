"""
app/models/log_event.py
───────────────────────
Common internal representation of a single log line after normalisation.
Every collector must produce LogEvent objects — nothing downstream
cares about the original format.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


class LogEvent(BaseModel):
    """Normalised representation of a single log record."""

    # ── Required fields ────────────────────────────────────────────────────────
    timestamp: datetime = Field(description="When the event occurred.")
    level: LogLevel = Field(description="Severity level of the log record.")
    message: str = Field(description="Human-readable log message.")
    source: str = Field(
        description="Origin of the log: filename, module path, or collector name."
    )
    raw_message: str = Field(
        description="The original unmodified log line, always preserved."
    )

    # ── Commonly available fields ──────────────────────────────────────────────
    service: Optional[str] = Field(
        default=None,
        description="Logical service name (e.g. 'user-service').",
    )
    logger: Optional[str] = Field(
        default=None,
        description="Logger name (e.g. 'uvicorn.error', 'sqlalchemy.engine').",
    )

    # ── Request / trace context ────────────────────────────────────────────────
    request_id: Optional[str] = Field(
        default=None,
        description="Unique identifier for the HTTP request.",
    )
    trace_id: Optional[str] = Field(
        default=None,
        description="Distributed trace identifier.",
    )

    # ── HTTP context ───────────────────────────────────────────────────────────
    endpoint: Optional[str] = Field(
        default=None,
        description="URL path of the request (e.g. '/users').",
    )
    http_method: Optional[str] = Field(
        default=None,
        description="HTTP verb (GET, POST, …).",
    )
    status_code: Optional[int] = Field(
        default=None,
        description="HTTP response status code.",
    )

    # ── Exception context ──────────────────────────────────────────────────────
    exception_type: Optional[str] = Field(
        default=None,
        description="Python exception class name (e.g. 'AttributeError').",
    )

    model_config = {"use_enum_values": True}
