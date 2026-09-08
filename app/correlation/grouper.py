"""
app/correlation/grouper.py
──────────────────────────
CorrelationGrouper — combines related log events into IncidentCluster objects.

Design
──────
The grouper maintains a dict of open "buckets", keyed by a correlation key
derived from (service, request_id | trace_id | endpoint | source).

A new event is added to an existing bucket when:
  • its correlation key matches, AND
  • its timestamp is within ``correlation_window_seconds`` of the bucket's
    last event.

A bucket is flushed (yielded as an IncidentCluster) after
``flush_delay_seconds`` of silence (no new events for that bucket).

Traceback handling
──────────────────
When a line "Traceback (most recent call last):" is seen, the grouper
opens a *traceback context* on the current bucket and collects subsequent
lines as traceback frames until it detects the end of the traceback.

Traceback end detection:
  • A non-indented, non-empty line that is NOT a frame line AND
    does NOT start "During handling …" closes the traceback.
  • An exception line (ExcType: message) ends and is included.

Flush is driven by an asyncio task per bucket.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import AsyncIterator, Dict, List, Optional

from app.models.incidents import IncidentCluster
from app.models.log_event import LogEvent, LogLevel

_TRACEBACK_START = "Traceback (most recent call last):"
_TRACEBACK_FRAME_RE = re.compile(r'^\s+File "')
_TRACEBACK_DURING_RE = re.compile(r"^(During handling|The above exception)", re.IGNORECASE)

# Matches the final line of a Python traceback: ExcType: message
_EXCEPTION_LINE_RE = re.compile(
    r"^([A-Za-z][A-Za-z0-9_.]*(?:Error|Exception|Warning|KeyboardInterrupt"
    r"|SystemExit|GeneratorExit|StopIteration))"
    r"(?::\s*.+)?$"
)


def _correlation_key(event: LogEvent) -> str:
    """Build a string key that groups related events together."""
    parts: List[str] = [event.service or "unknown"]

    # Prefer explicit correlation identifiers
    if event.request_id:
        parts.append(f"req:{event.request_id}")
    elif event.trace_id:
        parts.append(f"trace:{event.trace_id}")
    else:
        # Fall back to endpoint + source
        if event.endpoint:
            parts.append(f"ep:{event.endpoint}")
        parts.append(f"src:{event.source}")

    return "|".join(parts)


def _is_traceback_continuation(msg: str) -> bool:
    """
    True when a *message string* (already stripped of log prefix) looks like
    it belongs inside a Python traceback.
    """
    stripped = msg.strip()
    if not stripped:
        return False
    # File "..." lines
    if _TRACEBACK_FRAME_RE.match(stripped):
        return True
    # Indented source-code lines
    if msg.startswith("    ") or msg.startswith("\t"):
        return True
    # "During handling of …" continuation
    if _TRACEBACK_DURING_RE.match(stripped):
        return True
    # Final exception line  ExcType: message
    if _EXCEPTION_LINE_RE.match(stripped):
        return True
    return False


class _Bucket:
    """Mutable state for one open incident cluster."""

    def __init__(self, first_event: LogEvent, key: str) -> None:
        self.key = key
        self.events: List[LogEvent] = [first_event]
        self.last_event_time: datetime = first_event.timestamp
        self.in_traceback: bool = False
        self.traceback_lines: List[str] = []
        self.flush_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]

        msg = first_event.message.strip()
        if _TRACEBACK_START in msg:
            self.in_traceback = True
            self.traceback_lines.append(msg)

    @property
    def first_event(self) -> LogEvent:
        return self.events[0]

    def add(self, event: LogEvent) -> None:
        self.events.append(event)
        self.last_event_time = event.timestamp
        msg = event.message.strip()

        if self.in_traceback:
            self.traceback_lines.append(msg)
            # Exception line closes the traceback
            if _EXCEPTION_LINE_RE.match(msg):
                self.in_traceback = False
            elif not _is_traceback_continuation(msg):
                # Non-traceback line — traceback ended before this line
                self.in_traceback = False
        elif _TRACEBACK_START in msg:
            self.in_traceback = True
            self.traceback_lines.append(msg)

    def to_cluster(self) -> IncidentCluster:
        events = self.events

        # Pick primary error: prefer ERROR/CRITICAL, then last message
        primary_error: Optional[str] = None
        exception_type: Optional[str] = None
        for ev in reversed(events):
            if ev.level in (LogLevel.ERROR, LogLevel.CRITICAL):
                primary_error = ev.message
                if ev.exception_type:
                    exception_type = ev.exception_type
                break

        # Extract exception type from final traceback line if not already found
        if not exception_type and self.traceback_lines:
            last_tb = self.traceback_lines[-1].strip()
            m = _EXCEPTION_LINE_RE.match(last_tb)
            if m:
                exception_type = m.group(1)
                if primary_error is None:
                    primary_error = last_tb

        # Also try directly scanning event messages for exception patterns
        if not exception_type:
            for ev in reversed(events):
                m = _EXCEPTION_LINE_RE.match(ev.message.strip())
                if m:
                    exception_type = m.group(1)
                    if primary_error is None:
                        primary_error = ev.message
                    break

        if primary_error is None:
            primary_error = events[-1].message

        # Aggregate request/trace/endpoint from events
        request_id = next((e.request_id for e in events if e.request_id), None)
        trace_id = next((e.trace_id for e in events if e.trace_id), None)
        endpoint = next((e.endpoint for e in events if e.endpoint), None)
        http_method = next((e.http_method for e in events if e.http_method), None)
        service = next((e.service for e in events if e.service), None)
        source = events[0].source

        return IncidentCluster(
            first_seen=events[0].timestamp,
            last_seen=events[-1].timestamp,
            service=service,
            source=source,
            events=events,
            primary_error=primary_error,
            traceback_lines=self.traceback_lines,
            exception_type=exception_type,
            request_id=request_id,
            trace_id=trace_id,
            endpoint=endpoint,
            http_method=http_method,
        )


class CorrelationGrouper:
    """
    Groups related log events into IncidentCluster objects.

    Usage (async generator pattern)::

        grouper = CorrelationGrouper(...)
        async for cluster in grouper.process(event_stream):
            await triage_agent.triage(cluster)

    Parameters
    ----------
    correlation_window_seconds:
        Maximum gap between two events to be grouped together.
    flush_delay_seconds:
        Seconds of silence before a bucket is flushed.
    """

    def __init__(
        self,
        correlation_window_seconds: float = 5.0,
        flush_delay_seconds: float = 3.0,
    ) -> None:
        self.correlation_window_seconds = correlation_window_seconds
        self.flush_delay_seconds = flush_delay_seconds
        self._buckets: Dict[str, _Bucket] = {}
        self._queue: asyncio.Queue[IncidentCluster] = asyncio.Queue()

    async def process(
        self, events: AsyncIterator[LogEvent]
    ) -> AsyncIterator[IncidentCluster]:
        """
        Consume an async stream of filtered LogEvents and yield IncidentClusters.
        """
        async for event in events:
            # Flush any clusters that became ready due to a gap
            ready_clusters = self._route_event(event)
            for cluster in ready_clusters:
                yield cluster

            # Drain async-timer-flushed clusters
            while not self._queue.empty():
                yield self._queue.get_nowait()

        # Stream ended — flush remaining open buckets
        await self._flush_all()
        while not self._queue.empty():
            yield self._queue.get_nowait()

    def _route_event(self, event: LogEvent) -> List[IncidentCluster]:
        """
        Route an event to the correct bucket.
        Returns clusters that were immediately flushed due to a time gap.
        """
        key = _correlation_key(event)
        now = event.timestamp
        flushed: List[IncidentCluster] = []

        if key in self._buckets:
            bucket = self._buckets[key]
            gap = (now - bucket.last_event_time).total_seconds()
            if gap <= self.correlation_window_seconds or bucket.in_traceback:
                # Belongs to existing bucket
                bucket.add(event)
                # Reset the flush timer
                self._reset_flush_timer(key, bucket)
                return flushed
            else:
                # Gap too large — flush old bucket synchronously
                old_bucket = self._buckets.pop(key)
                if old_bucket.flush_task and not old_bucket.flush_task.done():
                    old_bucket.flush_task.cancel()
                flushed.append(old_bucket.to_cluster())

        # Start a new bucket
        bucket = _Bucket(event, key)
        self._buckets[key] = bucket
        self._reset_flush_timer(key, bucket)
        return flushed

    def _reset_flush_timer(self, key: str, bucket: _Bucket) -> None:
        if bucket.flush_task and not bucket.flush_task.done():
            bucket.flush_task.cancel()
        bucket.flush_task = asyncio.create_task(self._schedule_flush(key))

    async def _schedule_flush(self, key: str) -> None:
        await asyncio.sleep(self.flush_delay_seconds)
        if key in self._buckets:
            cluster = self._buckets.pop(key).to_cluster()
            await self._queue.put(cluster)

    async def _flush_all(self) -> None:
        """Force-flush all remaining open buckets."""
        for key in list(self._buckets.keys()):
            bucket = self._buckets.pop(key)
            if bucket.flush_task and not bucket.flush_task.done():
                bucket.flush_task.cancel()
            await self._queue.put(bucket.to_cluster())
