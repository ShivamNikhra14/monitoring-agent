"""
tests/test_e2e.py
─────────────────
End-to-end test for the full monitoring pipeline.

Uses a mocked LLM (no real API calls).

Scenario
────────
A temporary log file is written with a mix of:
  • DEBUG/INFO noise                   → 0 alerts
  • Health check pings                 → 0 alerts
  • Successful retry                   → 0 alerts (triaged as non-actionable)
  • Python AttributeError traceback    → 1 alert
  • Duplicate of same traceback        → suppressed (count incremented)
  • Database connection failure        → 1 alert
  • Distinct KeyError traceback        → 1 alert

Expected: 3 unique alerts emitted, 1 suppressed duplicate.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from io import StringIO
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import Settings
from app.correlation.grouper import CorrelationGrouper
from app.filtering.prefilter import PreFilter
from app.ingestion.normalizer import LogNormalizer
from app.models.incidents import Category, IncidentCluster, Severity, TriageResult
from app.models.log_event import LogLevel
from app.output.terminal import TerminalOutput
from app.state.sqlite import SQLiteStateStore, generate_fingerprint
from app.triage.agent import TriageAgent


# ── Mock triage provider ───────────────────────────────────────────────────────

def make_mock_triage_result(is_actionable: bool, summary: str = "test") -> TriageResult:
    return TriageResult(
        severity=Severity.HIGH if is_actionable else Severity.LOW,
        category=Category.APPLICATION_ERROR if is_actionable else Category.UNKNOWN,
        is_actionable=is_actionable,
        summary=summary,
        root_cause_hypothesis="Test hypothesis",
        confidence=0.9,
        evidence=["Test evidence"],
    )


# Triage mock: non-actionable for retries, actionable for real errors
def make_smart_mock_provider():
    """Returns a mock TriageProvider that uses heuristics to decide actionability."""
    provider = MagicMock()

    async def smart_triage(cluster: IncidentCluster) -> TriageResult:
        primary = (cluster.primary_error or "").lower()
        # Successful retry
        if "re-established" in primary or "retry" in primary:
            return make_mock_triage_result(False, "Transient retry resolved")
        # Real errors
        if "attributeerror" in primary:
            return make_mock_triage_result(True, "AttributeError: user.email is None")
        if "keyerror" in primary:
            return make_mock_triage_result(True, "KeyError: amount missing from payload")
        if "could not connect" in primary or "connection" in primary:
            return make_mock_triage_result(True, "Database connection failure")
        return make_mock_triage_result(True, "Unknown error")

    provider.triage = smart_triage
    return provider


# ── E2E pipeline helper ────────────────────────────────────────────────────────

class FakeOutput:
    """Captures emit() and emit_suppressed() calls for assertions."""

    def __init__(self):
        self.emitted: List[TriageResult] = []
        self.suppressed: List[TriageResult] = []

    async def emit(self, cluster, result, occurrence_count=1):
        self.emitted.append(result)

    async def emit_suppressed(self, cluster, result, occurrence_count):
        self.suppressed.append(result)


SAMPLE_LOGS = """\
2026-09-08 10:30:00,000 DEBUG    app.db: Initializing connection pool
2026-09-08 10:30:00,200 INFO     uvicorn.main: Application startup complete.
2026-09-08 10:30:02,000 INFO     uvicorn.access: INFO:     127.0.0.1:54321 - "GET /healthz HTTP/1.1" 200 OK
2026-09-08 10:30:10,000 INFO     uvicorn.access: INFO:     10.0.0.1:60000 - "GET /api/v1/products HTTP/1.1" 200 OK
2026-09-08 10:30:15,000 WARNING  app.db: Connection to replica timed out; retrying (attempt 1/3)
2026-09-08 10:30:16,000 INFO     app.db: Connection re-established after 2 retries
2026-09-08 10:31:02,010 ERROR    uvicorn.error: Exception in ASGI application
2026-09-08 10:31:02,010 ERROR    uvicorn.error: Traceback (most recent call last):
2026-09-08 10:31:02,013 ERROR    uvicorn.error:   File "/app/app/routers/users.py", line 42, in create_user
2026-09-08 10:31:02,014 ERROR    uvicorn.error: AttributeError: 'NoneType' object has no attribute 'email'
2026-09-08 10:31:30,000 ERROR    sqlalchemy.engine: (psycopg2.OperationalError) could not connect to server: Connection refused
2026-09-08 10:32:00,010 ERROR    uvicorn.error: Exception in ASGI application
2026-09-08 10:32:00,010 ERROR    uvicorn.error: Traceback (most recent call last):
2026-09-08 10:32:00,011 ERROR    uvicorn.error:   File "/app/app/routers/users.py", line 42, in create_user
2026-09-08 10:32:00,012 ERROR    uvicorn.error: AttributeError: 'NoneType' object has no attribute 'email'
2026-09-08 10:33:00,001 ERROR    uvicorn.error: Traceback (most recent call last):
2026-09-08 10:33:00,002 ERROR    uvicorn.error:   File "/app/app/routers/orders.py", line 88, in process_payment
2026-09-08 10:33:00,003 ERROR    uvicorn.error: KeyError: 'amount'
"""


async def run_pipeline_on_text(
    log_text: str,
    tmp_path,
    dedup_window_hours: float = 10.0,
) -> FakeOutput:
    """
    Run the full pipeline (minus the file tail) synchronously on a text block.
    Returns the FakeOutput with emitted/suppressed counts.
    """
    normalizer = LogNormalizer(service="test-service", source="test.log")
    prefilter = PreFilter()
    grouper = CorrelationGrouper(correlation_window_seconds=5, flush_delay_seconds=0.05)
    provider = make_smart_mock_provider()
    triage_agent = TriageAgent(provider=provider)
    state_store = SQLiteStateStore(db_path=str(tmp_path / "e2e_state.db"))
    output = FakeOutput()

    await state_store.setup()

    from app.models.incidents import PreFilterResult

    # Parse all lines into events
    events = []
    for line in log_text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        event = normalizer.normalise(line)
        decision = prefilter.evaluate(event)
        if decision.result != PreFilterResult.IGNORE:
            events.append(event)

    # Feed through grouper
    async def event_gen():
        for ev in events:
            yield ev

    clusters = []
    async for cluster in grouper.process(event_gen()):
        clusters.append(cluster)

    # Triage + dedup + output
    for cluster in clusters:
        result = await triage_agent.triage(cluster)
        if not result.is_actionable:
            continue
        fp = generate_fingerprint(cluster)
        is_dup = await state_store.is_duplicate(fp, dedup_window_hours)
        if is_dup:
            count = await state_store.increment(fp)
            await output.emit_suppressed(cluster, result, count)
        else:
            await state_store.record(fp, cluster, result)
            count = await state_store.get_occurrence_count(fp)
            await output.emit(cluster, result, count)

    await state_store.teardown()
    return output


# ── Tests ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_e2e_alert_count(tmp_path):
    """3 distinct incidents → 3 alerts emitted, 1 suppressed duplicate."""
    out = await run_pipeline_on_text(SAMPLE_LOGS, tmp_path)
    # 3 distinct incidents: AttributeError, DB failure, KeyError
    assert len(out.emitted) == 3, (
        f"Expected 3 alerts, got {len(out.emitted)}: {[r.summary for r in out.emitted]}"
    )
    # 1 duplicate AttributeError
    assert len(out.suppressed) == 1, (
        f"Expected 1 suppressed, got {len(out.suppressed)}: {[r.summary for r in out.suppressed]}"
    )


@pytest.mark.asyncio
async def test_e2e_noise_produces_no_alerts(tmp_path):
    """Pure noise logs (DEBUG, INFO, health checks) produce no alerts."""
    noise_logs = """\
2026-09-08 10:30:00,000 DEBUG    app.db: Initializing connection pool
2026-09-08 10:30:00,200 INFO     uvicorn.main: Application startup complete.
2026-09-08 10:30:02,000 INFO     uvicorn.access: INFO:     127.0.0.1:54321 - "GET /healthz HTTP/1.1" 200 OK
2026-09-08 10:30:10,000 INFO     uvicorn.access: INFO:     10.0.0.1:60000 - "GET /api/v1/products HTTP/1.1" 200 OK
"""
    out = await run_pipeline_on_text(noise_logs, tmp_path)
    assert len(out.emitted) == 0
    assert len(out.suppressed) == 0


@pytest.mark.asyncio
async def test_e2e_traceback_is_single_cluster(tmp_path):
    """A multi-line traceback produces exactly one cluster (and one alert)."""
    traceback_logs = """\
2026-09-08 10:31:02,010 ERROR    uvicorn.error: Exception in ASGI application
2026-09-08 10:31:02,010 ERROR    uvicorn.error: Traceback (most recent call last):
2026-09-08 10:31:02,013 ERROR    uvicorn.error:   File "/app/app/routers/users.py", line 42
2026-09-08 10:31:02,014 ERROR    uvicorn.error: AttributeError: 'NoneType' object has no attribute 'email'
"""
    out = await run_pipeline_on_text(traceback_logs, tmp_path)
    assert len(out.emitted) == 1


@pytest.mark.asyncio
async def test_e2e_duplicate_suppressed(tmp_path):
    """Identical incidents within the dedup window → second is suppressed."""
    dup_logs = """\
2026-09-08 10:31:02,010 ERROR    uvicorn.error: Traceback (most recent call last):
2026-09-08 10:31:02,013 ERROR    uvicorn.error:   File "/app/routers/users.py", line 42
2026-09-08 10:31:02,014 ERROR    uvicorn.error: AttributeError: 'NoneType' object has no attribute 'email'
2026-09-08 10:32:00,010 ERROR    uvicorn.error: Traceback (most recent call last):
2026-09-08 10:32:00,011 ERROR    uvicorn.error:   File "/app/routers/users.py", line 42
2026-09-08 10:32:00,012 ERROR    uvicorn.error: AttributeError: 'NoneType' object has no attribute 'email'
"""
    out = await run_pipeline_on_text(dup_logs, tmp_path)
    assert len(out.emitted) == 1
    assert len(out.suppressed) == 1


@pytest.mark.asyncio
async def test_e2e_distinct_errors_separate_alerts(tmp_path):
    """Two different exception types → two separate alerts."""
    distinct_logs = """\
2026-09-08 10:31:02,010 ERROR    uvicorn.error: Traceback (most recent call last):
2026-09-08 10:31:02,014 ERROR    uvicorn.error: AttributeError: 'NoneType' object has no attribute 'email'
2026-09-08 10:33:00,001 ERROR    uvicorn.error: Traceback (most recent call last):
2026-09-08 10:33:00,003 ERROR    uvicorn.error: KeyError: 'amount'
"""
    out = await run_pipeline_on_text(distinct_logs, tmp_path)
    assert len(out.emitted) == 2
    summaries = [r.summary for r in out.emitted]
    assert any("Attribute" in s or "attribute" in s or "email" in s for s in summaries), summaries
    assert any("Key" in s or "amount" in s for s in summaries), summaries
