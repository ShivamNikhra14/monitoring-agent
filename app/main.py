"""
app/main.py
───────────
Monitoring pipeline orchestrator.

Wires all components together and runs the event-driven pipeline:

    TextFileCollector
        → PreFilter
        → CorrelationGrouper
        → TriageAgent
        → SQLiteStateStore (deduplication)
        → TerminalOutput

Entry points
────────────
    python -m app.main                  # normal run using .env / env vars
    python -m app.main --help           # show config options
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Optional

from app.config import settings
from app.correlation.grouper import CorrelationGrouper
from app.filtering.prefilter import PreFilter
from app.ingestion.text_file import TextFileCollector
from app.models.incidents import PreFilterResult
from app.models.log_event import LogEvent
from app.output.terminal import TerminalOutput
from app.state.sqlite import SQLiteStateStore, generate_fingerprint
from app.triage.agent import OllamaTriageProvider, OpenAITriageProvider, TriageAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("monitoring_agent")


class MonitoringPipeline:
    """
    End-to-end monitoring pipeline.

    Parameters
    ----------
    All parameters default to values from ``settings`` (loaded from .env).
    Pass overrides explicitly for testing.
    """

    def __init__(
        self,
        log_file_path: Optional[str] = None,
        correlation_window_seconds: Optional[float] = None,
        flush_delay_seconds: Optional[float] = None,
        dedup_window_hours: Optional[float] = None,
        state_db_path: Optional[str] = None,
        triage_agent: Optional[TriageAgent] = None,
        output=None,
        state_store=None,
    ) -> None:
        self._log_file = log_file_path or settings.log_file_path
        corr_window = correlation_window_seconds or settings.correlation_window_seconds
        flush_delay = flush_delay_seconds or settings.flush_delay_seconds
        self._dedup_window = dedup_window_hours or settings.dedup_window_hours

        # Components
        self._collector = TextFileCollector(
            file_path=self._log_file,
            service=None,  # service name comes from log content
            poll_interval_seconds=0.2,
        )
        self._prefilter = PreFilter()
        self._grouper = CorrelationGrouper(
            correlation_window_seconds=corr_window,
            flush_delay_seconds=flush_delay,
        )
        self._triage_agent = triage_agent or self._build_triage_agent()
        self._state_store = state_store or SQLiteStateStore(
            db_path=state_db_path or settings.state_db_path
        )
        self._output = output or TerminalOutput()
        self._running = False

    def _build_triage_agent(self) -> TriageAgent:
        """
        Build the triage agent from ``settings``.

        Dispatches on ``LLM_PROVIDER``:
          ollama  — local Ollama server (default, no API key needed)
          openai  — OpenAI cloud API (requires OPENAI_API_KEY)
        """
        provider_name = settings.llm_provider.lower().strip()

        if provider_name == "ollama":
            logger.info(
                "Triage provider: Ollama  model=%s  base_url=%s",
                settings.llm_model,
                settings.ollama_base_url,
            )
            provider = OllamaTriageProvider(
                model=settings.llm_model,
                base_url=settings.ollama_base_url,
                custom_rules=settings.custom_triage_rules or None,
            )

        elif provider_name == "openai":
            if not settings.openai_api_key:
                raise ValueError(
                    "LLM_PROVIDER=openai but OPENAI_API_KEY is not set.\n"
                    "Add it to your .env file or set the environment variable."
                )
            logger.info(
                "Triage provider: OpenAI  model=%s",
                settings.llm_model,
            )
            provider = OpenAITriageProvider(
                api_key=settings.openai_api_key,
                model=settings.llm_model,
                base_url=settings.openai_base_url,
                custom_rules=settings.custom_triage_rules or None,
            )

        else:
            raise ValueError(
                f"Unknown LLM_PROVIDER='{settings.llm_provider}'. "
                "Supported values: 'ollama', 'openai'."
            )

        return TriageAgent(provider=provider)

    async def run(self) -> None:
        """Start the monitoring pipeline. Runs until cancelled or interrupted."""
        await self._state_store.setup()
        self._running = True

        logger.info("Monitoring Agent started — watching %s", self._log_file)
        logger.info(
            "Config: corr_window=%.1fs  flush_delay=%.1fs  dedup=%.1fh",
            settings.correlation_window_seconds,
            settings.flush_delay_seconds,
            self._dedup_window,
        )

        try:
            await self._run_pipeline()
        except asyncio.CancelledError:
            logger.info("Pipeline cancelled — shutting down.")
        finally:
            await self._state_store.teardown()
            logger.info("Monitoring Agent stopped.")

    async def _run_pipeline(self) -> None:
        """Inner async pipeline loop."""

        async def _filtered_events():
            """Yield only CANDIDATE and IMMEDIATE events from the collector."""
            async for event in self._collector.stream():
                decision = self._prefilter.evaluate(event)
                if decision.result == PreFilterResult.IGNORE:
                    logger.debug(
                        "IGNORE [%s] %s — %s",
                        event.level,
                        event.message[:80],
                        decision.reason,
                    )
                    continue
                logger.debug(
                    "%s [%s] %s — %s",
                    decision.result,
                    event.level,
                    event.message[:80],
                    decision.reason,
                )
                yield event

        async for cluster in self._grouper.process(_filtered_events()):
            await self._handle_cluster(cluster)

    async def _handle_cluster(self, cluster) -> None:
        """Triage one cluster, apply deduplication, and emit alert."""
        logger.info(
            "Cluster ready: %d events, primary_error=%s",
            len(cluster.events),
            (cluster.primary_error or "")[:100],
        )

        # LLM triage
        result = await self._triage_agent.triage(cluster)

        if not result.is_actionable:
            logger.info("Cluster triaged as non-actionable (%s) — skipped.", result.summary[:80])
            return

        # Deduplication
        fingerprint = generate_fingerprint(cluster)
        is_dup = await self._state_store.is_duplicate(fingerprint, self._dedup_window)

        if is_dup:
            count = await self._state_store.increment(fingerprint)
            logger.info("DUPLICATE (fp=%s, count=%d) — suppressed.", fingerprint, count)
            await self._output.emit_suppressed(cluster, result, count)
            return

        # New incident
        await self._state_store.record(fingerprint, cluster, result)
        count = await self._state_store.get_occurrence_count(fingerprint)
        await self._output.emit(cluster, result, occurrence_count=count)


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, main_task: asyncio.Task) -> None:
    """Install SIGINT / SIGTERM handlers on Unix-like platforms."""
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, main_task.cancel)
    except NotImplementedError:
        # Windows doesn't support add_signal_handler for all signals
        pass


async def _main() -> None:
    pipeline = MonitoringPipeline()
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    if task:
        _install_signal_handlers(loop, task)
    await pipeline.run()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\nMonitoring Agent interrupted.", file=sys.stderr)
