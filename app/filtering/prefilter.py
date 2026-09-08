"""
app/filtering/prefilter.py
──────────────────────────
Cheap, deterministic, rule-based prefilter.

Purpose
───────
Eliminate obvious noise before any correlation or LLM call.
Cost: O(n) string/regex checks — no external calls, no ML.

Classification
──────────────
IGNORE    — routine noise; discard immediately
CANDIDATE — looks like a potential problem; send to correlator
IMMEDIATE — high-signal failure (traceback, CRITICAL); send to correlator
            with elevated priority

Decision rules (evaluated top-to-bottom, first match wins)
────────────────────────────────────────────────────────────
1. IGNORE  — DEBUG level
2. IGNORE  — empty message
3. IMMEDIATE — "Traceback (most recent call last):" prefix
4. IMMEDIATE — CRITICAL level
5. IMMEDIATE — known crash signals ("unhandled exception", "fatal error")
6. IGNORE  — INFO + health-check path
7. IGNORE  — INFO + 2xx status code
8. IGNORE  — INFO + routine startup/shutdown keywords
9. IGNORE  — INFO (catchall for remaining INFO lines)
10. CANDIDATE — ERROR level
11. CANDIDATE — WARNING level with significant keywords
12. CANDIDATE — 5xx status code
13. CANDIDATE — database/connection error keywords
14. CANDIDATE — timeout / circuit-breaker keywords
15. IGNORE  — WARNING (routine warnings not caught above)
16. IGNORE  — fallback
"""
from __future__ import annotations

import re
from typing import FrozenSet

from app.models.incidents import PreFilterDecision, PreFilterResult
from app.models.log_event import LogEvent, LogLevel

_TRACEBACK_DURING_RE = re.compile(r"^(During handling|The above exception)", re.IGNORECASE)

# ── Keyword sets ──────────────────────────────────────────────────────────────

_HEALTH_CHECK_PATHS: FrozenSet[str] = frozenset(
    {"/health", "/healthz", "/ready", "/readiness", "/liveness", "/ping", "/status"}
)

_STARTUP_SHUTDOWN_KEYWORDS = re.compile(
    r"\b(started|starting|listening|shutdown|shutting down|application startup"
    r"|application shutdown|lifespan|uvicorn running|worker booted"
    r"|server is ready|loaded configuration|initializing)\b",
    re.IGNORECASE,
)

_CRASH_KEYWORDS = re.compile(
    r"\b(unhandled exception|unhandled error|fatal error|process crash"
    r"|segmentation fault|out of memory|oom killer|kernel panic"
    r"|abort trap|core dumped)\b",
    re.IGNORECASE,
)

_DB_ERROR_KEYWORDS = re.compile(
    r"\b(connection refused|connection reset|connection timed out"
    r"|could not connect|lost connection|operational error"
    r"|database is locked|too many connections|max_connections"
    r"|ssl connection|authentication failed|access denied"
    r"|relation does not exist|no such table)\b",
    re.IGNORECASE,
)

_TIMEOUT_KEYWORDS = re.compile(
    r"\b(timeout|timed out|circuit.?breaker|rate.?limit|connection pool"
    r"|retry exhausted|max retries exceeded)\b",
    re.IGNORECASE,
)

_SIGNIFICANT_WARNING_KEYWORDS = re.compile(
    r"\b(deprecated.*critical|memory pressure|disk.?full|certificate.*expir"
    r"|failed to|could not|unable to)\b",
    re.IGNORECASE,
)

# Lines starting with these are part of tracebacks (indented frames)
_TRACEBACK_FRAME_RE = re.compile(r'^\s+File "')
_TRACEBACK_START = "Traceback (most recent call last):"


class PreFilter:
    """
    Stateless rule-based prefilter.

    Usage::

        pf = PreFilter()
        decision = pf.evaluate(event)
        if decision.result != PreFilterResult.IGNORE:
            send_to_correlator(event)
    """

    def evaluate(self, event: LogEvent) -> PreFilterDecision:  # noqa: C901 (complexity)
        msg = event.message.strip()
        level = event.level

        # ── Rule 1: DEBUG always ignored ──────────────────────────────────────
        if level == LogLevel.DEBUG:
            return PreFilterDecision(result=PreFilterResult.IGNORE, reason="DEBUG level")

        # ── Rule 2: Empty message ─────────────────────────────────────────────
        if not msg:
            return PreFilterDecision(result=PreFilterResult.IGNORE, reason="Empty message")

        # ── Rule 2b: Traceback continuation lines ─────────────────────────────
        # Indented frame lines and exception lines that follow a traceback header
        # must not be filtered out; they are collected by the grouper.
        if (
            msg.startswith("  File ")
            or msg.startswith("\tFile ")
            or msg.startswith("    ")
            or _TRACEBACK_DURING_RE.search(msg)
        ):
            # Only pass through if it really looks like part of a traceback
            if (
                'File "' in msg
                or msg.startswith("    ")
                or _TRACEBACK_DURING_RE.search(msg)
            ):
                return PreFilterDecision(
                    result=PreFilterResult.CANDIDATE,
                    reason="Traceback continuation line",
                )

        # ── Rule 3: Python traceback header ───────────────────────────────────
        if msg.startswith(_TRACEBACK_START) or _TRACEBACK_START in msg:
            return PreFilterDecision(
                result=PreFilterResult.IMMEDIATE,
                reason="Python traceback detected",
            )

        # ── Rule 4: CRITICAL level ────────────────────────────────────────────
        if level == LogLevel.CRITICAL:
            return PreFilterDecision(
                result=PreFilterResult.IMMEDIATE,
                reason="CRITICAL severity",
            )

        # ── Rule 5: Known crash phrases ───────────────────────────────────────
        if _CRASH_KEYWORDS.search(msg):
            return PreFilterDecision(
                result=PreFilterResult.IMMEDIATE,
                reason=f"Crash keyword detected: {_CRASH_KEYWORDS.search(msg).group()}",  # type: ignore[union-attr]
            )

        # ── Rules 6–9: INFO-level filtering ──────────────────────────────────
        if level == LogLevel.INFO:
            # Rule 6a: 5xx at INFO level — still a server error (candidate)
            if event.status_code and event.status_code >= 500:
                return PreFilterDecision(
                    result=PreFilterResult.CANDIDATE,
                    reason=f"HTTP {event.status_code} server error (INFO level)",
                )

            # Rule 6b: Health check endpoint
            if event.endpoint and event.endpoint.split("?")[0] in _HEALTH_CHECK_PATHS:
                return PreFilterDecision(
                    result=PreFilterResult.IGNORE,
                    reason=f"Health-check endpoint: {event.endpoint}",
                )
            if any(p in msg.lower() for p in _HEALTH_CHECK_PATHS):
                return PreFilterDecision(
                    result=PreFilterResult.IGNORE,
                    reason="Health-check path in message",
                )

            # Rule 7: Successful HTTP response
            if event.status_code and 200 <= event.status_code < 300:
                return PreFilterDecision(
                    result=PreFilterResult.IGNORE,
                    reason=f"Successful HTTP {event.status_code}",
                )

            # Rule 8: Routine startup/shutdown
            if _STARTUP_SHUTDOWN_KEYWORDS.search(msg):
                return PreFilterDecision(
                    result=PreFilterResult.IGNORE,
                    reason="Routine startup/shutdown message",
                )

            # Rule 9: All other INFO
            return PreFilterDecision(
                result=PreFilterResult.IGNORE,
                reason="INFO level (non-error)",
            )

        # ── Rule 10: ERROR level ──────────────────────────────────────────────
        if level == LogLevel.ERROR:
            return PreFilterDecision(
                result=PreFilterResult.CANDIDATE,
                reason="ERROR level",
            )

        # ── Rule 11: Significant WARNING ─────────────────────────────────────
        if level == LogLevel.WARNING:
            if _SIGNIFICANT_WARNING_KEYWORDS.search(msg):
                return PreFilterDecision(
                    result=PreFilterResult.CANDIDATE,
                    reason="WARNING with significant keywords",
                )
            # Routine WARNING — ignore
            return PreFilterDecision(
                result=PreFilterResult.IGNORE,
                reason="Routine WARNING",
            )

        # ── Rule 12: 5xx status code (any level) ─────────────────────────────
        if event.status_code and event.status_code >= 500:
            return PreFilterDecision(
                result=PreFilterResult.CANDIDATE,
                reason=f"HTTP {event.status_code} server error",
            )

        # ── Rule 13: Database error keywords ─────────────────────────────────
        if _DB_ERROR_KEYWORDS.search(msg):
            return PreFilterDecision(
                result=PreFilterResult.CANDIDATE,
                reason="Database/connection error keyword",
            )

        # ── Rule 14: Timeout / circuit-breaker keywords ───────────────────────
        if _TIMEOUT_KEYWORDS.search(msg):
            return PreFilterDecision(
                result=PreFilterResult.CANDIDATE,
                reason="Timeout/circuit-breaker keyword",
            )

        # ── Rule 15: UNKNOWN level — treat conservatively ─────────────────────
        if level == LogLevel.UNKNOWN:
            # Check if it has any error-like content
            upper = msg.upper()
            if "ERROR" in upper or "EXCEPTION" in upper or "FAIL" in upper:
                return PreFilterDecision(
                    result=PreFilterResult.CANDIDATE,
                    reason="UNKNOWN level with error keywords",
                )

        # ── Rule 16: Fallback — ignore ────────────────────────────────────────
        return PreFilterDecision(result=PreFilterResult.IGNORE, reason="No matching rule")
