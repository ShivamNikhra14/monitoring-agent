"""
app/output/terminal.py
───────────────────────
TerminalOutput — prints a formatted incident report to stdout.

No external dependencies. Uses only the Python standard library.

Output format example:

    ============================================================
    ⚡ NEW INCIDENT
    ============================================================
    Incident ID : 8c7a3d...
    Severity    : HIGH
    Category    : APPLICATION_ERROR
    Confidence  : 91%
    ...
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from app.models.incidents import IncidentCluster, Severity, TriageResult

_SEP = "=" * 64
_THIN_SEP = "-" * 64

_SEVERITY_ICONS = {
    Severity.LOW: "ℹ",
    Severity.MEDIUM: "⚠",
    Severity.HIGH: "⚡",
    Severity.CRITICAL: "🔴",
}


def _icon(severity: str) -> str:
    try:
        return _SEVERITY_ICONS[Severity(severity)]
    except (ValueError, KeyError):
        return "•"


class TerminalOutput:
    """
    Prints formatted incident alerts to stdout (or a custom stream).

    Parameters
    ----------
    stream:
        Output stream. Defaults to sys.stdout. Override for testing.
    """

    def __init__(self, stream=None) -> None:
        self._stream = stream or sys.stdout

    async def emit(
        self,
        cluster: IncidentCluster,
        result: TriageResult,
        occurrence_count: int = 1,
    ) -> None:
        icon = _icon(result.severity)
        lines = [
            "",
            _SEP,
            f"{icon}  NEW INCIDENT  {icon}",
            _SEP,
            "",
            f"  Incident ID  : {cluster.incident_id}",
            f"  Severity     : {result.severity}",
            f"  Category     : {result.category}",
            f"  Confidence   : {int(result.confidence * 100)}%",
            f"  Occurrences  : {occurrence_count}",
            "",
            _THIN_SEP,
            f"  Service  : {cluster.service or 'unknown'}",
        ]

        if cluster.endpoint:
            method = cluster.http_method or ""
            lines.append(f"  Endpoint : {method} {cluster.endpoint}".rstrip())
        if cluster.request_id:
            lines.append(f"  Request  : {cluster.request_id}")
        if cluster.trace_id:
            lines.append(f"  Trace    : {cluster.trace_id}")

        time_range = (
            f"{cluster.first_seen.strftime('%H:%M:%S')} – "
            f"{cluster.last_seen.strftime('%H:%M:%S')} UTC"
        )
        lines.append(f"  Time     : {time_range}")

        lines += [
            "",
            _THIN_SEP,
            "  Summary:",
            f"    {result.summary}",
            "",
            "  Root Cause Hypothesis:",
        ]
        # Wrap hypothesis at 60 chars
        for segment in _wrap(result.root_cause_hypothesis, 60):
            lines.append(f"    {segment}")

        if result.evidence:
            lines += ["", "  Evidence:"]
            for item in result.evidence:
                lines.append(f"    • {item}")

        if cluster.traceback_lines:
            lines += ["", _THIN_SEP, "  Traceback:"]
            for tb_line in cluster.traceback_lines[:30]:  # cap at 30 lines
                lines.append(f"    {tb_line}")
            if len(cluster.traceback_lines) > 30:
                remaining = len(cluster.traceback_lines) - 30
                lines.append(f"    … ({remaining} more lines)")

        lines += [
            "",
            f"  Alerted at: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "",
            _SEP,
            "",
        ]

        print("\n".join(lines), file=self._stream, flush=True)

    async def emit_suppressed(
        self,
        cluster: IncidentCluster,
        result: TriageResult,
        occurrence_count: int,
    ) -> None:
        """Print a brief suppression notice for duplicate incidents."""
        print(
            f"  [SUPPRESSED] {result.severity} {cluster.service or ''} — "
            f"{result.summary[:80]} "
            f"(occurrence #{occurrence_count}, dedup window active)",
            file=self._stream,
            flush=True,
        )


def _wrap(text: str, width: int = 60):
    """Very simple word-wrap."""
    words = text.split()
    current: list[str] = []
    current_len = 0
    for word in words:
        if current_len + len(word) + 1 > width:
            yield " ".join(current)
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len += len(word) + 1
    if current:
        yield " ".join(current)
