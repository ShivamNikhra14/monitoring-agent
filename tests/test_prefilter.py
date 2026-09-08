"""tests/test_prefilter.py — unit tests for the PreFilter."""
from __future__ import annotations

import pytest

from app.filtering.prefilter import PreFilter
from app.models.incidents import PreFilterResult
from app.models.log_event import LogLevel
from tests.conftest import make_event


@pytest.fixture
def pf():
    return PreFilter()


# ── IGNORE cases ──────────────────────────────────────────────────────────────

class TestIgnored:
    def test_debug_always_ignored(self, pf):
        ev = make_event(level=LogLevel.DEBUG, message="Checking token cache")
        assert pf.evaluate(ev).result == PreFilterResult.IGNORE

    def test_empty_message_ignored(self, pf):
        ev = make_event(level=LogLevel.INFO, message="")
        assert pf.evaluate(ev).result == PreFilterResult.IGNORE

    def test_info_health_check_endpoint_ignored(self, pf):
        ev = make_event(level=LogLevel.INFO, message="GET /healthz 200", endpoint="/healthz", status_code=200)
        assert pf.evaluate(ev).result == PreFilterResult.IGNORE

    def test_info_readiness_probe_ignored(self, pf):
        ev = make_event(level=LogLevel.INFO, message="GET /readiness 200", endpoint="/readiness")
        assert pf.evaluate(ev).result == PreFilterResult.IGNORE

    def test_info_2xx_request_ignored(self, pf):
        ev = make_event(level=LogLevel.INFO, message="GET /api/products 200", status_code=200)
        assert pf.evaluate(ev).result == PreFilterResult.IGNORE

    def test_info_201_created_ignored(self, pf):
        ev = make_event(level=LogLevel.INFO, message="POST /orders 201", status_code=201)
        assert pf.evaluate(ev).result == PreFilterResult.IGNORE

    def test_info_startup_message_ignored(self, pf):
        ev = make_event(level=LogLevel.INFO, message="Application startup complete.")
        assert pf.evaluate(ev).result == PreFilterResult.IGNORE

    def test_info_shutdown_message_ignored(self, pf):
        ev = make_event(level=LogLevel.INFO, message="Shutting down gracefully")
        assert pf.evaluate(ev).result == PreFilterResult.IGNORE

    def test_info_generic_ignored(self, pf):
        ev = make_event(level=LogLevel.INFO, message="User profile loaded")
        assert pf.evaluate(ev).result == PreFilterResult.IGNORE

    def test_routine_warning_ignored(self, pf):
        ev = make_event(level=LogLevel.WARNING, message="Response took 250ms")
        assert pf.evaluate(ev).result == PreFilterResult.IGNORE


# ── CANDIDATE cases ───────────────────────────────────────────────────────────

class TestCandidates:
    def test_error_level_is_candidate(self, pf):
        ev = make_event(level=LogLevel.ERROR, message="Database query failed")
        assert pf.evaluate(ev).result == PreFilterResult.CANDIDATE

    def test_5xx_status_is_candidate(self, pf):
        ev = make_event(level=LogLevel.INFO, message="POST /users 500", status_code=500)
        assert pf.evaluate(ev).result == PreFilterResult.CANDIDATE

    def test_503_is_candidate(self, pf):
        ev = make_event(level=LogLevel.INFO, message="Service unavailable", status_code=503)
        assert pf.evaluate(ev).result == PreFilterResult.CANDIDATE

    def test_db_connection_refused_is_candidate(self, pf):
        ev = make_event(
            level=LogLevel.ERROR,
            message="could not connect to server: Connection refused",
        )
        assert pf.evaluate(ev).result == PreFilterResult.CANDIDATE

    def test_timeout_is_candidate(self, pf):
        ev = make_event(level=LogLevel.ERROR, message="Connection timed out after 30s")
        assert pf.evaluate(ev).result == PreFilterResult.CANDIDATE

    def test_significant_warning_is_candidate(self, pf):
        ev = make_event(level=LogLevel.WARNING, message="Failed to send email notification")
        assert pf.evaluate(ev).result == PreFilterResult.CANDIDATE


# ── IMMEDIATE cases ───────────────────────────────────────────────────────────

class TestImmediate:
    def test_traceback_start_is_immediate(self, pf):
        ev = make_event(
            level=LogLevel.ERROR,
            message="Traceback (most recent call last):",
        )
        assert pf.evaluate(ev).result == PreFilterResult.IMMEDIATE

    def test_critical_level_is_immediate(self, pf):
        ev = make_event(level=LogLevel.CRITICAL, message="System out of memory")
        assert pf.evaluate(ev).result == PreFilterResult.IMMEDIATE

    def test_unhandled_exception_is_immediate(self, pf):
        ev = make_event(level=LogLevel.ERROR, message="Unhandled exception in worker thread")
        assert pf.evaluate(ev).result == PreFilterResult.IMMEDIATE

    def test_fatal_error_is_immediate(self, pf):
        ev = make_event(level=LogLevel.ERROR, message="Fatal error: process cannot continue")
        assert pf.evaluate(ev).result == PreFilterResult.IMMEDIATE


# ── Reason strings ─────────────────────────────────────────────────────────────

class TestReasons:
    def test_decision_has_reason(self, pf):
        ev = make_event(level=LogLevel.DEBUG, message="debug")
        decision = pf.evaluate(ev)
        assert decision.reason  # non-empty string
        assert isinstance(decision.reason, str)
