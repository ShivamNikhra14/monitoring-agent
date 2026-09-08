"""tests/test_normalizer.py — unit tests for LogNormalizer."""
from __future__ import annotations

from datetime import timezone

import pytest

from app.ingestion.normalizer import LogNormalizer
from app.models.log_event import LogLevel


@pytest.fixture
def norm():
    return LogNormalizer(service="test-svc", source="test-file.log")


# ── JSON format ────────────────────────────────────────────────────────────────

class TestJsonParsing:
    def test_json_basic(self, norm):
        line = '{"level": "ERROR", "message": "Something broke", "timestamp": "2026-09-08T10:00:00Z"}'
        ev = norm.normalise(line)
        assert ev.level == LogLevel.ERROR
        assert ev.message == "Something broke"
        assert ev.raw_message == line

    def test_json_preserves_raw(self, norm):
        line = '{"level": "INFO", "message": "hello", "timestamp": "2026-09-08T10:00:00Z"}'
        ev = norm.normalise(line)
        assert ev.raw_message == line

    def test_json_with_request_id(self, norm):
        line = '{"level": "ERROR", "message": "fail", "timestamp": "2026-09-08T10:00:00Z", "request_id": "abc-123"}'
        ev = norm.normalise(line)
        assert ev.request_id == "abc-123"

    def test_json_with_status_code(self, norm):
        line = '{"level": "ERROR", "message": "fail", "timestamp": "2026-09-08T10:00:00Z", "status_code": 500}'
        ev = norm.normalise(line)
        assert ev.status_code == 500

    def test_json_structlog_keys(self, norm):
        """structlog uses 'event' instead of 'message'."""
        line = '{"level": "warning", "event": "cache miss", "timestamp": "2026-09-08T10:00:00Z"}'
        ev = norm.normalise(line)
        assert ev.level == LogLevel.WARNING
        assert ev.message == "cache miss"

    def test_json_unknown_level(self, norm):
        line = '{"level": "VERBOSE", "message": "trace", "timestamp": "2026-09-08T10:00:00Z"}'
        ev = norm.normalise(line)
        assert ev.level == LogLevel.UNKNOWN


# ── uvicorn access log format ─────────────────────────────────────────────────

class TestUvicornAccessLog:
    def test_200_response(self, norm):
        line = 'INFO:     127.0.0.1:54321 - "GET /healthz HTTP/1.1" 200 OK'
        ev = norm.normalise(line)
        assert ev.level == LogLevel.INFO
        assert ev.endpoint == "/healthz"
        assert ev.http_method == "GET"
        assert ev.status_code == 200

    def test_500_response(self, norm):
        line = 'INFO:     10.0.0.1:60000 - "POST /api/v1/users HTTP/1.1" 500 Internal Server Error'
        ev = norm.normalise(line)
        assert ev.status_code == 500
        assert ev.http_method == "POST"
        assert ev.endpoint == "/api/v1/users"


# ── Standard Python logging format ───────────────────────────────────────────

class TestPythonLogFormat:
    def test_error_line(self, norm):
        line = "2026-09-08 10:31:02,010 ERROR    uvicorn.error: Exception in ASGI application"
        ev = norm.normalise(line)
        assert ev.level == LogLevel.ERROR
        assert ev.message == "Exception in ASGI application"
        assert ev.logger == "uvicorn.error"

    def test_debug_line(self, norm):
        line = "2026-09-08 10:30:01,000 DEBUG    app.db: Initializing connection pool"
        ev = norm.normalise(line)
        assert ev.level == LogLevel.DEBUG
        assert "connection pool" in ev.message.lower()

    def test_timestamp_parsed(self, norm):
        line = "2026-09-08 10:31:02,010 ERROR    app.mod: Something"
        ev = norm.normalise(line)
        assert ev.timestamp.year == 2026
        assert ev.timestamp.month == 9
        assert ev.timestamp.day == 8
        assert ev.timestamp.tzinfo is not None

    def test_raw_message_always_preserved(self, norm):
        line = "2026-09-08 10:31:02,010 ERROR    app.mod: test error"
        ev = norm.normalise(line)
        assert ev.raw_message == line


# ── Fallback handling ─────────────────────────────────────────────────────────

class TestFallback:
    def test_plain_error_line(self, norm):
        line = "AttributeError: 'NoneType' object has no attribute 'email'"
        ev = norm.normalise(line)
        assert ev.level in (LogLevel.ERROR, LogLevel.UNKNOWN)
        assert ev.raw_message == line

    def test_empty_line_is_debug(self, norm):
        ev = norm.normalise("")
        assert ev.level == LogLevel.DEBUG

    def test_service_attached(self, norm):
        line = "2026-09-08 10:00:00 INFO Some message"
        ev = norm.normalise(line)
        assert ev.service == "test-svc"
