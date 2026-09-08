"""
app/ingestion/text_file.py
──────────────────────────
TextFileCollector — watches a local text file and yields new LogEvents
as lines are appended, similar to ``tail -f``.

Behaviour
─────────
* On startup, seeks to the end of the file (does not replay history).
* Polls for new content every ``poll_interval_seconds`` seconds.
* Passes each new line through ``LogNormalizer`` to produce a ``LogEvent``.
* Continues indefinitely until the caller stops consuming.

No business logic about whether a log is an incident lives here.
"""
from __future__ import annotations

import asyncio
import os
from typing import AsyncIterator, Optional

import aiofiles

from app.ingestion.base import LogCollector  # noqa: F401 (imported for type checking)
from app.ingestion.normalizer import LogNormalizer
from app.models.log_event import LogEvent


class TextFileCollector:
    """
    Async log collector that tails a local text file.

    Parameters
    ----------
    file_path:
        Path to the log file to watch.
    service:
        Logical service name to attach to every emitted event.
    poll_interval_seconds:
        How often to check for new data when the file hasn't changed.
    encoding:
        File encoding (default: utf-8).
    """

    def __init__(
        self,
        file_path: str,
        service: Optional[str] = None,
        poll_interval_seconds: float = 0.5,
        encoding: str = "utf-8",
    ) -> None:
        self.file_path = file_path
        self.poll_interval_seconds = poll_interval_seconds
        self.encoding = encoding
        self._normalizer = LogNormalizer(service=service, source=file_path)

    async def stream(self) -> AsyncIterator[LogEvent]:
        """
        Async generator that yields ``LogEvent`` objects for every new line
        appended to the watched file.

        Creates the file if it does not yet exist.
        """
        # Ensure the file exists so we can open it
        os.makedirs(os.path.dirname(self.file_path) or ".", exist_ok=True)
        if not os.path.exists(self.file_path):
            open(self.file_path, "a").close()  # noqa: WPS515

        async with aiofiles.open(self.file_path, mode="r", encoding=self.encoding) as fh:
            # Seek to end — we only care about new content
            await fh.seek(0, 2)

            while True:
                line = await fh.readline()
                if line:
                    line = line.rstrip("\n\r")
                    if line.strip():  # skip empty lines
                        event = self._normalizer.normalise(line)
                        yield event
                else:
                    # No new data yet — yield control briefly
                    await asyncio.sleep(self.poll_interval_seconds)
