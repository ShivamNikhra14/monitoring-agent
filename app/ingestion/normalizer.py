"""
app/ingestion/normalizer.py
────────────────────────────
Converts a raw log line into a ``LogEvent``.

Strategy (tried in order):
  1. JSON — handles structured loggers (structlog, python-json-logger)
  2. uvicorn / gunicorn access log pattern
  3. Standard Python logging format  ``YYYY-MM-DD HH:MM:SS,ms LEVEL logger: message``
  4. ISO-8601 timestamp prefix fallback
  5. Plain fallback (no timestamp/level detected)

The ``raw_message`` field is always preserved verbatim.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Optional

from app.models.log_event import LogEvent, LogLevel

# ── Timestamp patterns ─────────────────────────────────────────────────────────

_TS_PATTERNS = [
    # 2026-09-08 10:31:02,123
    (
        r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)",
        [
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S,%f",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
        ],
    ),
    # 08/Sep/2026:10:31:02 +0530  (nginx/gunicorn access log)
    (
        r"(\d{2}/\w+/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4})",
        ["%d/%b/%Y:%H:%M:%S %z"],
    ),
]

_LEVEL_MAP = {
    "debug": LogLevel.DEBUG,
    "info": LogLevel.INFO,
    "warning": LogLevel.WARNING,
    "warn": LogLevel.WARNING,
    "error": LogLevel.ERROR,
    "critical": LogLevel.CRITICAL,
    "fatal": LogLevel.CRITICAL,
}

# ── Regex patterns ─────────────────────────────────────────────────────────────

# Standard Python logging:  2026-09-08 10:31:02,123 ERROR module.path: message
_PY_LOG_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)"
    r"\s+(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)"
    r"(?:\s+(?P<logger>[\w.]+))?[:\s]+(?P<message>.+)$",
    re.IGNORECASE,
)

# uvicorn access log:  INFO:     127.0.0.1:12345 - "GET /health HTTP/1.1" 200 OK
_UVICORN_ACCESS_RE = re.compile(
    r"^(?P<level>INFO|ERROR|WARNING|CRITICAL):\s+"
    r"(?P<client>[\d.]+:\d+)\s+-\s+"
    r'"(?P<method>[A-Z]+)\s+(?P<path>[^\s"]+)[^"]*"\s+(?P<status>\d{3})',
    re.IGNORECASE,
)

# Exception type at end of traceback, e.g.:  AttributeError: 'NoneType' object …
_EXCEPTION_RE = re.compile(
    r"^(?P<exc>[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*Error"
    r"|[A-Za-z][A-Za-z0-9_]*Exception"
    r"|KeyboardInterrupt|SystemExit|GeneratorExit"
    r"|StopIteration|StopAsyncIteration)"
    r"(?::\s*(?P<detail>.+))?$"
)

# request_id / trace_id patterns
_REQ_ID_RE = re.compile(r'(?:request[_-]?id|req[_-]?id)["\s:=]+([a-f0-9-]{8,})', re.IGNORECASE)
_TRACE_ID_RE = re.compile(r'(?:trace[_-]?id)["\s:=]+([a-f0-9-]{8,})', re.IGNORECASE)


def _parse_timestamp(raw_ts: str) -> Optional[datetime]:
    for _, fmts in _TS_PATTERNS:
        for fmt in fmts:
            try:
                dt = datetime.strptime(raw_ts.strip(), fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue
    return None


def _parse_level(raw: str) -> LogLevel:
    return _LEVEL_MAP.get(raw.lower().strip(), LogLevel.UNKNOWN)


def _extract_request_id(text: str) -> Optional[str]:
    m = _REQ_ID_RE.search(text)
    return m.group(1) if m else None


def _extract_trace_id(text: str) -> Optional[str]:
    m = _TRACE_ID_RE.search(text)
    return m.group(1) if m else None


def _extract_exception_type(message: str) -> Optional[str]:
    m = _EXCEPTION_RE.match(message.strip())
    return m.group("exc") if m else None


class LogNormalizer:
    """
    Converts a raw log line string into a ``LogEvent``.

    Parameters
    ----------
    service:
        Default service name to attach when the line doesn't specify one.
    source:
        Default source label (e.g. file path or collector identifier).
    """

    def __init__(self, service: Optional[str] = None, source: str = "text-file"):
        self.service = service
        self.source = source

    def normalise(self, line: str) -> LogEvent:
        line = line.rstrip("\n\r")
        if not line.strip():
            # Return a minimal DEBUG event for empty lines so they get filtered
            return LogEvent(
                timestamp=datetime.now(tz=timezone.utc),
                level=LogLevel.DEBUG,
                message="",
                source=self.source,
                raw_message=line,
                service=self.service,
            )

        # Try parsers in priority order
        event = (
            self._try_json(line)
            or self._try_uvicorn_access(line)
            or self._try_python_log(line)
            or self._try_timestamp_prefix(line)
            or self._plain_fallback(line)
        )
        return event

    # ── JSON ──────────────────────────────────────────────────────────────────

    def _try_json(self, line: str) -> Optional[LogEvent]:
        if not line.startswith("{"):
            return None
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None

        # Normalise common key names used by different JSON loggers
        raw_level = (
            data.get("level")
            or data.get("levelname")
            or data.get("severity")
            or "UNKNOWN"
        )
        raw_ts = (
            data.get("timestamp")
            or data.get("time")
            or data.get("asctime")
            or data.get("datetime")
        )
        message = (
            data.get("message")
            or data.get("msg")
            or data.get("event")
            or str(data)
        )
        ts = _parse_timestamp(str(raw_ts)) if raw_ts else datetime.now(tz=timezone.utc)

        return LogEvent(
            timestamp=ts or datetime.now(tz=timezone.utc),
            level=_parse_level(str(raw_level)),
            message=str(message),
            source=data.get("filename") or data.get("module") or self.source,
            raw_message=line,
            service=data.get("service") or self.service,
            logger=data.get("logger") or data.get("name"),
            request_id=data.get("request_id") or _extract_request_id(line),
            trace_id=data.get("trace_id") or _extract_trace_id(line),
            endpoint=data.get("path") or data.get("endpoint"),
            http_method=data.get("method"),
            status_code=int(data["status_code"]) if data.get("status_code") else None,
            exception_type=(
                data.get("exception_type") or _extract_exception_type(str(message))
            ),
        )

    # ── uvicorn access log ────────────────────────────────────────────────────

    def _try_uvicorn_access(self, line: str) -> Optional[LogEvent]:
        m = _UVICORN_ACCESS_RE.match(line)
        if not m:
            return None
        return LogEvent(
            timestamp=datetime.now(tz=timezone.utc),
            level=_parse_level(m.group("level")),
            message=line,
            source=self.source,
            raw_message=line,
            service=self.service,
            logger="uvicorn.access",
            endpoint=m.group("path"),
            http_method=m.group("method"),
            status_code=int(m.group("status")),
        )

    # ── Standard Python logging ───────────────────────────────────────────────

    def _try_python_log(self, line: str) -> Optional[LogEvent]:
        m = _PY_LOG_RE.match(line)
        if not m:
            return None
        ts = _parse_timestamp(m.group("ts"))
        message = m.group("message").strip()
        return LogEvent(
            timestamp=ts or datetime.now(tz=timezone.utc),
            level=_parse_level(m.group("level")),
            message=message,
            source=m.group("logger") or self.source,
            raw_message=line,
            service=self.service,
            logger=m.group("logger"),
            request_id=_extract_request_id(message),
            trace_id=_extract_trace_id(message),
            exception_type=_extract_exception_type(message),
        )

    # ── ISO timestamp prefix fallback ─────────────────────────────────────────

    def _try_timestamp_prefix(self, line: str) -> Optional[LogEvent]:
        for pattern, _ in _TS_PATTERNS:
            m = re.match(pattern, line)
            if m:
                ts = _parse_timestamp(m.group(1))
                if ts:
                    rest = line[m.end():].strip()
                    # Try to extract a level word from rest
                    level_match = re.match(
                        r"^(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\b[:\s]*",
                        rest,
                        re.IGNORECASE,
                    )
                    if level_match:
                        level = _parse_level(level_match.group(1))
                        message = rest[level_match.end():].strip()
                    else:
                        level = LogLevel.UNKNOWN
                        message = rest
                    return LogEvent(
                        timestamp=ts,
                        level=level,
                        message=message,
                        source=self.source,
                        raw_message=line,
                        service=self.service,
                        exception_type=_extract_exception_type(message),
                    )
        return None

    # ── Plain fallback ────────────────────────────────────────────────────────

    def _plain_fallback(self, line: str) -> LogEvent:
        # Last resort: treat entire line as message, level UNKNOWN
        upper = line.upper()
        if "ERROR" in upper or "EXCEPTION" in upper or "TRACEBACK" in upper:
            level = LogLevel.ERROR
        elif "CRITICAL" in upper or "FATAL" in upper:
            level = LogLevel.CRITICAL
        elif "WARN" in upper:
            level = LogLevel.WARNING
        elif "DEBUG" in upper:
            level = LogLevel.DEBUG
        elif "INFO" in upper:
            level = LogLevel.INFO
        else:
            level = LogLevel.UNKNOWN
        return LogEvent(
            timestamp=datetime.now(tz=timezone.utc),
            level=level,
            message=line.strip(),
            source=self.source,
            raw_message=line,
            service=self.service,
            exception_type=_extract_exception_type(line.strip()),
        )
