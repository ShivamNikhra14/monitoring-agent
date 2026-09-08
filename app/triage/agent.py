"""
app/triage/agent.py
────────────────────
Triage agent — the ONLY component that calls an LLM.

Architecture
────────────
``TriageProvider`` is a Protocol (pluggable interface).
``OllamaTriageProvider``  — local Ollama server (default).
``OpenAITriageProvider``  — OpenAI cloud API.
``TriageAgent``           — orchestrates the call and validates the output.

Ollama uses its OpenAI-compatible chat endpoint, so both providers share
the same ``openai`` async client under the hood — no extra packages needed.

To add a new provider: implement ``TriageProvider`` and pass it to
``TriageAgent``.  Nothing else in the pipeline changes.
"""
from __future__ import annotations

import json
import logging
import re
from typing import List, Optional, Protocol, runtime_checkable

from pydantic import ValidationError

from app.models.incidents import (
    Category,
    IncidentCluster,
    Severity,
    TriageResult,
)
from app.triage.prompt import build_system_prompt, build_user_message

logger = logging.getLogger(__name__)


# ── TriageProvider protocol ────────────────────────────────────────────────────

@runtime_checkable
class TriageProvider(Protocol):
    """
    Interface for LLM triage backends.

    Implement this to add a new LLM provider without changing anything else.
    """

    async def triage(self, cluster: IncidentCluster) -> TriageResult:
        """
        Analyse an IncidentCluster and return a structured TriageResult.

        Must not raise; should return a fallback result on error.
        """
        ...


# ── Shared OpenAI-client helper ────────────────────────────────────────────────

def _make_openai_client(base_url: str, api_key: str):
    """
    Return an ``AsyncOpenAI`` client configured for the given endpoint.

    Works for both the official OpenAI API and any OpenAI-compatible server
    (Ollama, LM Studio, vLLM, Azure OpenAI, etc.).
    """
    try:
        from openai import AsyncOpenAI  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "openai package is required. Install it with: pip install openai"
        ) from exc

    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url  # type: ignore[assignment]
    return AsyncOpenAI(**kwargs)


# ── Ollama provider ───────────────────────────────────────────────────────────

class OllamaTriageProvider:
    """
    TriageProvider backed by a local Ollama server.

    Uses Ollama's OpenAI-compatible REST API
    (``http://localhost:11434/v1`` by default).

    Requirements
    ────────────
    1. Ollama installed and running:  https://ollama.com/download
    2. Model pulled:  ``ollama pull llama3.1:8b``

    The ``api_key`` parameter is ignored by Ollama but required by the
    OpenAI client library — any non-empty string works.

    Notes on JSON output
    ────────────────────
    Ollama does not support ``response_format={"type":"json_object"}`` for
    all models, so we use a strict JSON instruction in the prompt instead and
    extract the JSON block from the raw response.
    """

    def __init__(
        self,
        model: str = "llama3.1:8b",
        base_url: str = "http://localhost:11434",
        custom_rules: Optional[List[str]] = None,
        temperature: float = 0.0,
    ) -> None:
        # Ollama's OpenAI-compatible endpoint lives at /v1
        api_base = base_url.rstrip("/") + "/v1"
        self._client = _make_openai_client(base_url=api_base, api_key="ollama")
        self.model = model
        self.temperature = temperature
        self._system_prompt = build_system_prompt(custom_rules)

    async def triage(self, cluster: IncidentCluster) -> TriageResult:
        user_message = build_user_message(cluster)

        try:
            response = await self._client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                # Note: json_object format not universally supported by Ollama
                # models — we extract JSON from the response text instead.
                messages=[
                    {"role": "system", "content": self._system_prompt},
                    {"role": "user", "content": user_message},
                ],
            )
            raw_text = response.choices[0].message.content or "{}"
            return _parse_triage_result(_extract_json(raw_text), cluster)

        except Exception as exc:  # noqa: BLE001
            logger.error("Ollama triage call failed: %s", exc)
            return _fallback_result(cluster, reason=str(exc))


# ── OpenAI provider ───────────────────────────────────────────────────────────

class OpenAITriageProvider:
    """
    TriageProvider backed by OpenAI chat completions (cloud API).

    Requires ``OPENAI_API_KEY`` to be set.

    Also works with any OpenAI-compatible proxy (set ``OPENAI_BASE_URL``).
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        base_url: str = "",
        custom_rules: Optional[List[str]] = None,
        temperature: float = 0.0,
    ) -> None:
        self._client = _make_openai_client(base_url=base_url, api_key=api_key)
        self.model = model
        self.temperature = temperature
        self._system_prompt = build_system_prompt(custom_rules)

    async def triage(self, cluster: IncidentCluster) -> TriageResult:
        user_message = build_user_message(cluster)

        try:
            response = await self._client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": self._system_prompt},
                    {"role": "user", "content": user_message},
                ],
            )
            raw_json = response.choices[0].message.content or "{}"
            return _parse_triage_result(raw_json, cluster)

        except Exception as exc:  # noqa: BLE001
            logger.error("OpenAI triage call failed: %s", exc)
            return _fallback_result(cluster, reason=str(exc))


# ── TriageAgent orchestrator ──────────────────────────────────────────────────

class TriageAgent:
    """
    Orchestrates triage for an IncidentCluster.

    Wraps a TriageProvider and handles validation / fallback.
    """

    def __init__(self, provider: TriageProvider) -> None:
        self._provider = provider

    async def triage(self, cluster: IncidentCluster) -> TriageResult:
        """Return a validated TriageResult for the given cluster."""
        return await self._provider.triage(cluster)


# ── Helpers ───────────────────────────────────────────────────────────────────

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_BARE_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> str:
    """
    Extract a JSON object from a model response.

    Tries (in order):
      1. Fenced code block  ```json { ... } ```
      2. First bare  { ... }  in the text
      3. The text as-is (let the JSON parser report any error)
    """
    m = _JSON_BLOCK_RE.search(text)
    if m:
        return m.group(1)
    m = _JSON_BARE_RE.search(text)
    if m:
        return m.group(0)
    return text


def _parse_triage_result(raw_json: str, cluster: IncidentCluster) -> TriageResult:
    """Parse and validate the LLM JSON output into a TriageResult."""
    try:
        data = json.loads(raw_json)
        return TriageResult.model_validate(data)
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.warning(
            "Failed to parse triage result (cluster %s): %s\nRaw: %s",
            cluster.incident_id,
            exc,
            raw_json[:500],
        )
        return _fallback_result(cluster, reason=f"Parse error: {exc}")


def _fallback_result(cluster: IncidentCluster, reason: str = "") -> TriageResult:
    """Return a safe fallback when LLM call or parsing fails."""
    return TriageResult(
        severity=Severity.MEDIUM,
        category=Category.UNKNOWN,
        is_actionable=True,  # conservative — surface it rather than silence it
        summary=f"Triage unavailable — manual review required. {cluster.primary_error or ''}".strip(),
        root_cause_hypothesis=f"Could not obtain LLM triage. Reason: {reason}",
        confidence=0.0,
        evidence=[f"Cluster contained {len(cluster.events)} events"],
    )
