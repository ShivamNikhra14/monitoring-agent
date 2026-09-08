"""tests/test_fingerprint.py — unit tests for fingerprint generation."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models.incidents import IncidentCluster
from app.models.log_event import LogLevel
from app.state.sqlite import generate_fingerprint
from tests.conftest import make_event


def make_cluster(
    service="user-service",
    exception_type="AttributeError",
    primary_error="'NoneType' object has no attribute 'email'",
    source="app/routers/users.py",
    endpoint="/api/v1/users",
) -> IncidentCluster:
    ev = make_event(message=primary_error, level=LogLevel.ERROR)
    return IncidentCluster(
        first_seen=datetime.now(tz=timezone.utc),
        last_seen=datetime.now(tz=timezone.utc),
        service=service,
        source=source,
        events=[ev],
        primary_error=primary_error,
        exception_type=exception_type,
        endpoint=endpoint,
    )


class TestFingerprintStability:
    def test_same_inputs_produce_same_fingerprint(self):
        c1 = make_cluster()
        c2 = make_cluster()
        assert generate_fingerprint(c1) == generate_fingerprint(c2)

    def test_fingerprint_is_16_chars(self):
        c = make_cluster()
        fp = generate_fingerprint(c)
        assert len(fp) == 16

    def test_fingerprint_is_hex(self):
        c = make_cluster()
        fp = generate_fingerprint(c)
        int(fp, 16)  # should not raise


class TestFingerprintUniqueness:
    def test_different_exception_type_gives_different_fingerprint(self):
        c1 = make_cluster(exception_type="AttributeError")
        c2 = make_cluster(exception_type="KeyError")
        assert generate_fingerprint(c1) != generate_fingerprint(c2)

    def test_different_service_gives_different_fingerprint(self):
        c1 = make_cluster(service="user-service")
        c2 = make_cluster(service="order-service")
        assert generate_fingerprint(c1) != generate_fingerprint(c2)

    def test_different_endpoint_gives_different_fingerprint(self):
        c1 = make_cluster(endpoint="/api/v1/users")
        c2 = make_cluster(endpoint="/api/v1/orders")
        assert generate_fingerprint(c1) != generate_fingerprint(c2)


class TestFingerprintNormalisation:
    def test_dynamic_ids_stripped(self):
        """Long numeric IDs should be stripped so the fingerprint is stable."""
        c1 = make_cluster(primary_error="User 123456789 not found")
        c2 = make_cluster(primary_error="User 987654321 not found")
        # After stripping long numbers both become "User  not found"
        assert generate_fingerprint(c1) == generate_fingerprint(c2)

    def test_uuid_stripped(self):
        c1 = make_cluster(primary_error="Record 550e8400-e29b-41d4-a716-446655440000 missing")
        c2 = make_cluster(primary_error="Record 6ba7b810-9dad-11d1-80b4-00c04fd430c8 missing")
        assert generate_fingerprint(c1) == generate_fingerprint(c2)
