"""tests/test_dedup.py — unit tests for SQLiteStateStore deduplication."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app.models.incidents import Category, IncidentCluster, Severity, TriageResult
from app.models.log_event import LogLevel
from app.state.sqlite import SQLiteStateStore, generate_fingerprint
from tests.conftest import make_event


@pytest.fixture
def sample_cluster():
    ev = make_event(message="AttributeError: 'NoneType' object", level=LogLevel.ERROR)
    return IncidentCluster(
        first_seen=datetime.now(tz=timezone.utc),
        last_seen=datetime.now(tz=timezone.utc),
        service="user-service",
        source="app/routers/users.py",
        events=[ev],
        primary_error="AttributeError: 'NoneType' object has no attribute 'email'",
        exception_type="AttributeError",
        endpoint="/api/v1/users",
    )


@pytest.fixture
def sample_result():
    return TriageResult(
        severity=Severity.HIGH,
        category=Category.APPLICATION_ERROR,
        is_actionable=True,
        summary="User lookup returns None",
        root_cause_hypothesis="Missing null check",
        confidence=0.9,
        evidence=["AttributeError on email"],
    )


@pytest_asyncio.fixture
async def store(tmp_path):
    db_path = str(tmp_path / "test_state.db")
    s = SQLiteStateStore(db_path=db_path)
    await s.setup()
    yield s
    await s.teardown()


@pytest.mark.asyncio
class TestDeduplication:
    async def test_first_occurrence_not_duplicate(self, store, sample_cluster, sample_result):
        fp = generate_fingerprint(sample_cluster)
        assert not await store.is_duplicate(fp, dedup_window_hours=10)

    async def test_after_record_is_duplicate(self, store, sample_cluster, sample_result):
        fp = generate_fingerprint(sample_cluster)
        await store.record(fp, sample_cluster, sample_result)
        assert await store.is_duplicate(fp, dedup_window_hours=10)

    async def test_after_window_expires_not_duplicate(self, store, sample_cluster, sample_result):
        fp = generate_fingerprint(sample_cluster)
        await store.record(fp, sample_cluster, sample_result)
        # Window of 0 hours = expired immediately
        assert not await store.is_duplicate(fp, dedup_window_hours=0)

    async def test_increment_increases_count(self, store, sample_cluster, sample_result):
        fp = generate_fingerprint(sample_cluster)
        await store.record(fp, sample_cluster, sample_result)
        count = await store.increment(fp)
        assert count == 2

    async def test_increment_again(self, store, sample_cluster, sample_result):
        fp = generate_fingerprint(sample_cluster)
        await store.record(fp, sample_cluster, sample_result)
        await store.increment(fp)
        count = await store.increment(fp)
        assert count == 3

    async def test_occurrence_count_starts_at_one(self, store, sample_cluster, sample_result):
        fp = generate_fingerprint(sample_cluster)
        await store.record(fp, sample_cluster, sample_result)
        count = await store.get_occurrence_count(fp)
        assert count == 1

    async def test_unknown_fingerprint_returns_zero(self, store):
        count = await store.get_occurrence_count("nonexistent")
        assert count == 0

    async def test_different_fingerprints_independent(self, store, sample_cluster, sample_result):
        fp1 = generate_fingerprint(sample_cluster)
        await store.record(fp1, sample_cluster, sample_result)

        # Create a different cluster
        ev2 = make_event(message="KeyError: 'amount'", level=LogLevel.ERROR)
        cluster2 = IncidentCluster(
            first_seen=datetime.now(tz=timezone.utc),
            last_seen=datetime.now(tz=timezone.utc),
            service="order-service",
            source="app/routers/orders.py",
            events=[ev2],
            primary_error="KeyError: 'amount'",
            exception_type="KeyError",
            endpoint="/api/v1/orders",
        )
        fp2 = generate_fingerprint(cluster2)

        assert not await store.is_duplicate(fp2, dedup_window_hours=10)
