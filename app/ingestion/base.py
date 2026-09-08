"""
app/ingestion/base.py
─────────────────────
Protocol that every log collector must satisfy.

A collector's only responsibility is ingestion + normalisation.
It must NOT contain business logic about whether a log line is an incident.

Future collectors to add (do not implement here):
  DockerCollector, WebhookCollector, KafkaCollector,
  CloudWatchCollector, KubernetesCollector
"""
from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable

from app.models.log_event import LogEvent


@runtime_checkable
class LogCollector(Protocol):
    """
    Async generator protocol for log collectors.

    Usage::

        async for event in collector.stream():
            process(event)

    The generator must run indefinitely (or until the source is exhausted),
    yielding one ``LogEvent`` per log record.
    """

    async def stream(self) -> AsyncIterator[LogEvent]:  # type: ignore[override]
        """
        Yield normalised log events as they arrive.

        Must be an async generator — implementations use ``yield``.
        """
        ...
