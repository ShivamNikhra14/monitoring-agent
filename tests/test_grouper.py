"""tests/test_grouper.py — unit tests for CorrelationGrouper."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, List

import pytest

from app.correlation.grouper import CorrelationGrouper
from app.models.log_event import LogEvent, LogLevel
from tests.conftest import make_event


def ts(offset_seconds: float = 0.0) -> datetime:
    """Return a UTC datetime with an offset from a fixed base."""
    base = datetime(2026, 9, 8, 10, 31, 0, tzinfo=timezone.utc)
    return base + timedelta(seconds=offset_seconds)


async def events_to_list(
    grouper: CorrelationGrouper,
    events: List[LogEvent],
) -> list:
    """Helper: feed a list of events through the grouper and collect clusters."""
    results = []

    async def gen():
        for ev in events:
            yield ev

    async for cluster in grouper.process(gen()):
        results.append(cluster)
    return results


@pytest.mark.asyncio
class TestTracebackGrouping:
    async def test_traceback_collected_as_single_cluster(self):
        """A multi-line Python traceback must become ONE cluster."""
        grouper = CorrelationGrouper(correlation_window_seconds=5, flush_delay_seconds=0.05)
        events = [
            make_event("Exception in ASGI application", level=LogLevel.ERROR, ts=ts(0)),
            make_event("Traceback (most recent call last):", level=LogLevel.ERROR, ts=ts(0.01)),
            make_event('  File "users.py", line 42, in create_user', level=LogLevel.ERROR, ts=ts(0.01)),
            make_event("    return {\"email\": user.email}", level=LogLevel.ERROR, ts=ts(0.01)),
            make_event("AttributeError: 'NoneType' object has no attribute 'email'", level=LogLevel.ERROR, ts=ts(0.02)),
        ]
        clusters = await events_to_list(grouper, events)
        assert len(clusters) == 1
        cluster = clusters[0]
        assert len(cluster.events) == 5
        assert "AttributeError" in (cluster.exception_type or "")

    async def test_traceback_lines_captured(self):
        """traceback_lines should contain the traceback content."""
        grouper = CorrelationGrouper(correlation_window_seconds=5, flush_delay_seconds=0.05)
        events = [
            make_event("Traceback (most recent call last):", level=LogLevel.ERROR, ts=ts(0)),
            make_event('  File "users.py", line 42', level=LogLevel.ERROR, ts=ts(0.01)),
            make_event("AttributeError: 'NoneType' object has no attribute 'email'", level=LogLevel.ERROR, ts=ts(0.02)),
        ]
        clusters = await events_to_list(grouper, events)
        assert len(clusters) == 1
        assert any("Traceback" in line for line in clusters[0].traceback_lines)

    async def test_exception_type_extracted(self):
        grouper = CorrelationGrouper(correlation_window_seconds=5, flush_delay_seconds=0.05)
        events = [
            make_event("Traceback (most recent call last):", level=LogLevel.ERROR, ts=ts(0)),
            make_event("KeyError: 'amount'", level=LogLevel.ERROR, ts=ts(0.01)),
        ]
        clusters = await events_to_list(grouper, events)
        assert clusters[0].exception_type == "KeyError"


@pytest.mark.asyncio
class TestTemporalGrouping:
    async def test_events_within_window_grouped(self):
        grouper = CorrelationGrouper(correlation_window_seconds=5, flush_delay_seconds=0.05)
        events = [
            make_event("Error A", level=LogLevel.ERROR, ts=ts(0)),
            make_event("Error B", level=LogLevel.ERROR, ts=ts(2)),   # 2s gap → same cluster
        ]
        clusters = await events_to_list(grouper, events)
        assert len(clusters) == 1
        assert len(clusters[0].events) == 2

    async def test_events_outside_window_separated(self):
        grouper = CorrelationGrouper(correlation_window_seconds=5, flush_delay_seconds=0.05)
        events = [
            make_event("Error A", level=LogLevel.ERROR, ts=ts(0)),
            make_event("Error B", level=LogLevel.ERROR, ts=ts(10)),  # 10s gap → new cluster
        ]
        clusters = await events_to_list(grouper, events)
        assert len(clusters) == 2

    async def test_different_services_separated(self):
        grouper = CorrelationGrouper(correlation_window_seconds=5, flush_delay_seconds=0.05)
        events = [
            make_event("Error A", level=LogLevel.ERROR, service="service-a", ts=ts(0)),
            make_event("Error B", level=LogLevel.ERROR, service="service-b", ts=ts(1)),
        ]
        clusters = await events_to_list(grouper, events)
        assert len(clusters) == 2

    async def test_same_request_id_grouped(self):
        grouper = CorrelationGrouper(correlation_window_seconds=5, flush_delay_seconds=0.05)
        events = [
            make_event("Error A", level=LogLevel.ERROR, request_id="req-001", ts=ts(0)),
            make_event("Error B", level=LogLevel.ERROR, request_id="req-001", ts=ts(3)),
        ]
        clusters = await events_to_list(grouper, events)
        assert len(clusters) == 1

    async def test_different_request_ids_separated(self):
        grouper = CorrelationGrouper(correlation_window_seconds=5, flush_delay_seconds=0.05)
        events = [
            make_event("Error A", level=LogLevel.ERROR, request_id="req-001", ts=ts(0)),
            make_event("Error B", level=LogLevel.ERROR, request_id="req-002", ts=ts(1)),
        ]
        clusters = await events_to_list(grouper, events)
        assert len(clusters) == 2


@pytest.mark.asyncio
class TestClusterMetadata:
    async def test_primary_error_set(self):
        grouper = CorrelationGrouper(correlation_window_seconds=5, flush_delay_seconds=0.05)
        events = [
            make_event("Something happened", level=LogLevel.INFO, ts=ts(0)),
            make_event("Actual error here", level=LogLevel.ERROR, ts=ts(0.5)),
        ]
        clusters = await events_to_list(grouper, events)
        assert "Actual error" in (clusters[0].primary_error or "")

    async def test_time_range_correct(self):
        grouper = CorrelationGrouper(correlation_window_seconds=5, flush_delay_seconds=0.05)
        t0, t1 = ts(0), ts(2)
        events = [
            make_event("A", level=LogLevel.ERROR, ts=t0),
            make_event("B", level=LogLevel.ERROR, ts=t1),
        ]
        clusters = await events_to_list(grouper, events)
        assert clusters[0].first_seen == t0
        assert clusters[0].last_seen == t1
