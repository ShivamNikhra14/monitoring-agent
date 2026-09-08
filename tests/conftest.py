"""tests/conftest.py — shared fixtures for the test suite."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import List

import pytest
import pytest_asyncio

from app.models.log_event import LogEvent, LogLevel


def make_event(
    message: str = "test message",
    level: LogLevel = LogLevel.INFO,
    source: str = "app.test",
    service: str = "test-service",
    endpoint: str | None = None,
    http_method: str | None = None,
    status_code: int | None = None,
    exception_type: str | None = None,
    request_id: str | None = None,
    trace_id: str | None = None,
    ts: datetime | None = None,
) -> LogEvent:
    """Convenience factory for LogEvent objects in tests."""
    return LogEvent(
        timestamp=ts or datetime.now(tz=timezone.utc),
        level=level,
        message=message,
        source=source,
        raw_message=f"[{level}] {message}",
        service=service,
        endpoint=endpoint,
        http_method=http_method,
        status_code=status_code,
        exception_type=exception_type,
        request_id=request_id,
        trace_id=trace_id,
    )
